"""
ScreenWeave Visual QA — Worker Lambda

Invoked asynchronously by the Trigger Lambda.

Flow:
  0. Parent context      → if parent_session_id supplied, fetch its qa_report.json,
                           summarise it with Claude, inject summary as prior context
                           (silently skipped if the report is absent or on any error)
  1. Discover artifacts  → list S3 keys under {prefix}/{session_id}/
  2. Pre-process         → fetch states.json, strip to minimal fields
  3. Pair screenshots    → match each state to its S3 screenshot key
  4. Batch              → split into groups of BATCH_SIZE (default 10)
  5. Multi-turn Bedrock  → for each batch, add a [user, assistant] turn;
                           Claude carries shared context across all batches
  6. Consolidate        → final turn asks Claude for the structured JSON report
  7. Write report       → PUT qa_report.json to S3

Report is written to: s3://{bucket}/{prefix}/{session_id}/qa_report.json
"""

import base64
import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ── Constants ─────────────────────────────────────────────────────────────────

BATCH_SIZE = 10

BEDROCK_MODEL = "anthropic.claude-3-5-sonnet-20241022-v2:0"

# Fields retained from each state object after pre-processing.
# links_found and the screenshot relative path are intentionally excluded
# to minimise token weight.
KEEP_FIELDS = frozenset(
    {"state_id", "url", "trigger_label", "visible_text_preview", "interactive_elements"}
)

MAX_IMG_BYTES = 5 * 1024 * 1024  # 5 MB — skip larger screenshots with a warning

# Bedrock ThrottlingException backoff schedule (seconds)
_BACKOFF = (2, 4, 8)

# ── AWS clients (module-level for Lambda container reuse) ─────────────────────

_s3 = boto3.client("s3")
_bedrock = boto3.client(
    "bedrock-runtime",
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)


# ── S3 helpers ────────────────────────────────────────────────────────────────


def _discover_states(
    bucket: str, prefix: str, session_id: str
) -> tuple[list[dict], list[str]]:
    """
    Fetch and pre-process states.json; list screenshot S3 keys.

    Returns:
        stripped_states  – list of state dicts with only KEEP_FIELDS
        screenshot_keys  – sorted list of S3 keys for screenshots
    """
    session_prefix = f"{prefix}/{session_id}"
    states_key = f"{session_prefix}/states.json"

    # Fetch states.json
    try:
        obj = _s3.get_object(Bucket=bucket, Key=states_key)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("NoSuchKey", "404"):
            raise FileNotFoundError(
                f"states.json not found at s3://{bucket}/{states_key}"
            ) from exc
        raise

    raw = json.loads(obj["Body"].read().decode("utf-8"))
    states: list[dict] = raw.get("states", [])

    stripped = [
        {k: s[k] for k in KEEP_FIELDS if k in s}
        for s in states
    ]

    # List screenshot keys
    paginator = _s3.get_paginator("list_objects_v2")
    screenshot_keys: list[str] = []
    for page in paginator.paginate(
        Bucket=bucket, Prefix=f"{session_prefix}/screenshots/"
    ):
        for obj_meta in page.get("Contents", []):
            key = obj_meta["Key"]
            if key.endswith(".png"):
                screenshot_keys.append(key)

    # Sort numerically by the integer embedded in the filename (state_0001.png → 1)
    def _state_num(key: str) -> int:
        basename = key.rsplit("/", 1)[-1]  # "state_0001.png"
        try:
            return int(basename.replace("state_", "").replace(".png", ""))
        except ValueError:
            return 0

    screenshot_keys.sort(key=_state_num)

    logger.info(
        "Discovered %d states and %d screenshots for session %s",
        len(stripped),
        len(screenshot_keys),
        session_id,
    )
    return stripped, screenshot_keys


def _pair_screenshots(
    states: list[dict],
    bucket: str,
    prefix: str,
    session_id: str,
) -> list[tuple[dict, str | None]]:
    """
    Match each state to its S3 screenshot key.

    Uses the state_id to reconstruct the expected key rather than positional
    zip so gaps in numbering are handled correctly.

    Returns list of (state, s3_key_or_None).
    """
    session_prefix = f"{prefix}/{session_id}"
    pairs = []
    for state in states:
        sid = state.get("state_id", "")
        # Canonical key: {prefix}/{session_id}/screenshots/{state_id}.png
        expected_key = f"{session_prefix}/screenshots/{sid}.png"
        pairs.append((state, expected_key))
    return pairs


def _make_batches(
    pairs: list[tuple[dict, str | None]], batch_size: int = BATCH_SIZE
) -> list[list[tuple[dict, str | None]]]:
    """Chunk pairs into groups of batch_size."""
    return [pairs[i : i + batch_size] for i in range(0, len(pairs), batch_size)]


def _download_b64(bucket: str, key: str) -> str | None:
    """
    Download a PNG from S3 and return its base64 representation.
    Returns None on NoSuchKey or if the object exceeds MAX_IMG_BYTES.
    """
    try:
        obj = _s3.get_object(Bucket=bucket, Key=key)
        content_length = obj.get("ContentLength", 0)
        if content_length > MAX_IMG_BYTES:
            logger.warning(
                "Screenshot %s is %d bytes (> %d MB limit), skipping",
                key,
                content_length,
                MAX_IMG_BYTES // (1024 * 1024),
            )
            return None
        data = obj["Body"].read()
        return base64.b64encode(data).decode("ascii")
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("NoSuchKey", "404"):
            logger.warning("Screenshot not found: %s", key)
            return None
        raise


# ── Parent session context ────────────────────────────────────────────────────


def _fetch_parent_summary(bucket: str, prefix: str, parent_session_id: str) -> str | None:
    """
    Fetch the QA report for parent_session_id, summarise it with Claude,
    and return a concise summary string for use as prior context.

    Returns None — and logs a warning — if:
      • The report key does not exist in S3
      • Any S3 or Bedrock error occurs
    Callers should proceed normally (BAU) on None.
    """
    report_key = f"{prefix}/{parent_session_id}/qa_report.json"
    logger.info("Fetching parent session report: s3://%s/%s", bucket, report_key)

    # 1. Fetch the parent report from S3
    try:
        obj = _s3.get_object(Bucket=bucket, Key=report_key)
        report_text = obj["Body"].read().decode("utf-8")
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("NoSuchKey", "404"):
            logger.info(
                "Parent report not found for session %s — skipping context injection",
                parent_session_id,
            )
        else:
            logger.warning(
                "Could not fetch parent report %s (%s) — skipping context injection",
                report_key,
                exc,
            )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Unexpected error fetching parent report: %s — skipping context injection", exc
        )
        return None

    # Guard: empty or non-JSON report (e.g. a previous error stub)
    try:
        json.loads(report_text)
    except json.JSONDecodeError:
        logger.warning("Parent report is not valid JSON — skipping context injection")
        return None

    # 2. Summarise with Claude (single-turn, no images needed)
    logger.info("Summarising parent report with Claude (%d chars)", len(report_text))
    try:
        summary = _invoke_bedrock(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Summarise the following Visual QA report concisely. "
                                "Focus on overall status, the most significant issues found, "
                                "and any cross-batch observations. "
                                "Keep the summary under 300 words.\n\n"
                                f"REPORT:\n{report_text}"
                            ),
                        }
                    ],
                }
            ]
        )
        logger.info(
            "Parent context summary ready (%d chars) for session %s",
            len(summary),
            parent_session_id,
        )
        return summary
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not summarise parent report: %s — skipping context injection", exc
        )
        return None


# ── Bedrock prompt builders ───────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a senior visual QA engineer reviewing a web crawl session. "
    "You will receive screenshots and metadata across multiple batches. "
    "Carry your findings forward — each batch builds on the prior context.\n\n"
    "For each batch, examine every state and flag anything that looks wrong, "
    "inconsistent, or unexpected — visually or structurally. "
    "Do not limit yourself to a fixed checklist; surface whatever issues "
    "the evidence shows. After each batch respond with a plain-text summary "
    "of what you observed. In the final turn you will consolidate into a report."
)


def _build_batch_user_turn(
    batch: list[tuple[dict, str | None]],
    images_b64: list[str | None],
    batch_index: int,
    total_batches: int,
) -> list[dict]:
    """
    Build the content array for a single user turn covering one batch.

    Structure: [image_block, ...] followed by one text_block containing
    the screenshot index map, stripped metadata JSON, and QA instructions.
    """
    content: list[dict] = []

    # Image blocks (skip None slots)
    img_counter = 0
    for img_b64 in images_b64:
        if img_b64 is not None:
            img_counter += 1
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": img_b64,
                    },
                }
            )

    # Screenshot index map (helps Claude correlate images to state IDs)
    index_lines: list[str] = []
    img_seq = 0
    for i, ((state, _), img_b64) in enumerate(zip(batch, images_b64)):
        if img_b64 is not None:
            img_seq += 1
            label = (
                f"Image {img_seq}: {state.get('state_id')} | "
                f"{state.get('url', '')} | "
                f"trigger: {state.get('trigger_label') or 'n/a'}"
            )
        else:
            label = (
                f"MISSING: {state.get('state_id')} | "
                f"{state.get('url', '')} | "
                f"trigger: {state.get('trigger_label') or 'n/a'}"
            )
        index_lines.append(label)

    states_only = [state for state, _ in batch]
    metadata_json = json.dumps(states_only, indent=2)

    text = (
        f"--- BATCH {batch_index + 1} of {total_batches} ---\n\n"
        "SCREENSHOT INDEX MAP:\n"
        + "\n".join(index_lines)
        + "\n\n"
        "STATE METADATA:\n"
        + metadata_json
        + "\n\n"
        "Review the screenshots and metadata. Describe what you observe and flag any "
        "anomalies, regressions, or inconsistencies compared to previous batches.\n"
        "Respond in plain text — the JSON report comes in the final consolidation turn."
    )

    content.append({"type": "text", "text": text})
    return content


def _build_consolidation_turn(total_states: int, total_batches: int) -> list[dict]:
    """Final user turn: ask Claude to emit the structured JSON report."""
    prompt = (
        f"You have now reviewed all {total_states} states across {total_batches} batches.\n"
        "Produce the final QA report as valid JSON only — no markdown fences, no preamble:\n"
        "{\n"
        '  "report_version": "1.0",\n'
        '  "overall_status": "PASS or FAIL",\n'
        f'  "total_states_analyzed": {total_states},\n'
        f'  "total_batches": {total_batches},\n'
        '  "findings": [\n'
        '    {\n'
        '      "state_id": "...",\n'
        '      "url": "...",\n'
        '      "passed": true,\n'
        '      "observations": "What you saw",\n'
        '      "issues": []\n'
        '    }\n'
        '  ],\n'
        '  "cross_batch_observations": '
        '"Any patterns or regressions spanning multiple batches",\n'
        '  "all_issues": [\n'
        '    {"state_id": "...", "description": "..."}\n'
        '  ]\n'
        "}\n\n"
        "Report only what the evidence shows."
    )
    return [{"type": "text", "text": prompt}]


# ── Bedrock invocation ────────────────────────────────────────────────────────


def _invoke_bedrock(messages: list[dict]) -> str:
    """
    Call Claude via Bedrock with the full conversation history.
    Retries on ThrottlingException with exponential backoff.
    Returns the assistant's text response.
    """
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 4096,
            "system": _SYSTEM_PROMPT,
            "messages": messages,
        }
    )

    last_exc: Exception | None = None
    for attempt, delay in enumerate([0] + list(_BACKOFF)):
        if delay:
            logger.info("Bedrock throttle retry %d, sleeping %ds", attempt, delay)
            time.sleep(delay)
        try:
            resp = _bedrock.invoke_model(
                modelId=BEDROCK_MODEL,
                contentType="application/json",
                accept="application/json",
                body=body,
            )
            result = json.loads(resp["body"].read())
            return result["content"][0]["text"]
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("ThrottlingException", "ServiceUnavailableException"):
                last_exc = exc
                continue
            raise

    raise RuntimeError(
        f"Bedrock still throttling after {len(_BACKOFF)} retries"
    ) from last_exc


# ── Core orchestration ────────────────────────────────────────────────────────


def _run_visual_qa(
    batches: list[list[tuple[dict, str | None]]],
    bucket: str,
    parent_summary: str | None = None,
) -> dict:
    """
    Drive the multi-turn Bedrock conversation across all batches.

    If parent_summary is provided it is injected as the first [user, assistant]
    exchange so Claude has prior-session context before seeing any screenshots.

    Returns the parsed QA report dict from Claude's final consolidation turn.
    """
    messages: list[dict] = []
    total_batches = len(batches)
    total_states = sum(len(b) for b in batches)

    # ── Inject parent context ─────────────────────────────────────────────────
    if parent_summary:
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Before we begin, here is a summary of a prior related QA session. "
                            "Keep these findings in mind — they may help you spot regressions "
                            "or confirm improvements in the current session.\n\n"
                            f"PRIOR SESSION SUMMARY:\n{parent_summary}"
                        ),
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": (
                    "Understood. I've noted the prior session findings and will use them "
                    "as reference context when reviewing the current session — particularly "
                    "for regression detection and cross-session consistency."
                ),
            }
        )
        logger.info("Parent context injected into conversation (%d chars)", len(parent_summary))

    for batch_idx, batch in enumerate(batches):
        logger.info(
            "Processing batch %d/%d (%d states)",
            batch_idx + 1,
            total_batches,
            len(batch),
        )

        # Download screenshots for this batch
        images_b64 = [
            _download_b64(bucket, key) if key else None
            for _, key in batch
        ]
        loaded = sum(1 for img in images_b64 if img is not None)
        logger.info("  %d/%d screenshots loaded", loaded, len(batch))

        # Build user turn and append
        user_content = _build_batch_user_turn(
            batch, images_b64, batch_idx, total_batches
        )
        messages.append({"role": "user", "content": user_content})

        # Get Claude's batch summary (plain text — shared context for next batch)
        assistant_text = _invoke_bedrock(messages)
        logger.info("  Batch %d summary received (%d chars)", batch_idx + 1, len(assistant_text))
        messages.append({"role": "assistant", "content": assistant_text})

    # Final consolidation turn — request structured JSON report
    messages.append(
        {
            "role": "user",
            "content": _build_consolidation_turn(total_states, total_batches),
        }
    )
    report_text = _invoke_bedrock(messages)
    logger.info("Consolidation response received (%d chars)", len(report_text))

    # Parse Claude's JSON output
    try:
        report = json.loads(report_text)
    except json.JSONDecodeError:
        # Claude occasionally wraps JSON in a code fence; attempt to strip it
        stripped = report_text.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("```", 2)[1]
            if stripped.startswith("json"):
                stripped = stripped[4:]
            stripped = stripped.rsplit("```", 1)[0].strip()
        try:
            report = json.loads(stripped)
        except json.JSONDecodeError as exc:
            logger.error("Failed to parse Claude's JSON response: %s", exc)
            report = {
                "report_version": "1.0",
                "overall_status": "ERROR",
                "error": "Claude returned non-JSON response",
                "raw_response": report_text[:2000],
            }

    return report


def _write_report(bucket: str, key: str, report: dict) -> None:
    """PUT the QA report JSON to S3."""
    body = json.dumps(report, indent=2).encode("utf-8")
    _s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
    )
    logger.info("Report written to s3://%s/%s", bucket, key)


# ── Lambda entry point ────────────────────────────────────────────────────────


def handler(event: dict, context) -> None:
    """
    Lambda entry point.

    Expected event shape:
      {
        "session_id":        str,
        "bucket":            str,
        "prefix":            str,            (default: "screenweave")
        "parent_session_id": str | absent    (optional)
      }

    If parent_session_id is present, the Worker fetches that session's
    qa_report.json, summarises it with Claude, and uses the summary as
    prior context before starting the current session analysis. If the
    report is not found in S3 — or any error occurs — the step is silently
    skipped and analysis proceeds normally.

    The function always writes either the QA report or an error report to S3
    so the caller can always poll for a result.
    """
    session_id: str = event.get("session_id", "")
    bucket: str = event.get("bucket") or os.environ.get("ARTIFACTS_BUCKET", "")
    prefix: str = event.get("prefix") or os.environ.get("BUCKET_PREFIX", "screenweave")
    parent_session_id: str | None = event.get("parent_session_id") or None

    report_key = f"{prefix}/{session_id}/qa_report.json"

    logger.info(
        "VisualQA-Worker started | session=%s parent=%s bucket=%s prefix=%s",
        session_id,
        parent_session_id or "none",
        bucket,
        prefix,
    )

    started_at = datetime.now(timezone.utc).isoformat()

    try:
        # 0. Fetch and summarise parent session report (optional, best-effort)
        parent_summary: str | None = None
        if parent_session_id:
            parent_summary = _fetch_parent_summary(bucket, prefix, parent_session_id)

        # 1. Discover + pre-process
        stripped_states, _screenshot_keys = _discover_states(bucket, prefix, session_id)

        if not stripped_states:
            _write_report(
                bucket,
                report_key,
                {
                    "report_version": "1.0",
                    "session_id": session_id,
                    "overall_status": "ERROR",
                    "error": "No states found in states.json",
                    "generated_at": started_at,
                },
            )
            return

        # 2. Pair each state with its expected screenshot key
        pairs = _pair_screenshots(stripped_states, bucket, prefix, session_id)

        # 3. Batch
        batches = _make_batches(pairs, BATCH_SIZE)
        logger.info(
            "%d states → %d batches of up to %d",
            len(pairs),
            len(batches),
            BATCH_SIZE,
        )

        # 4. Run multi-turn Visual QA (parent_summary may be None — that's fine)
        report = _run_visual_qa(batches, bucket, parent_summary=parent_summary)

        # Stamp metadata onto the report
        report["session_id"] = session_id
        report["generated_at"] = datetime.now(timezone.utc).isoformat()
        report["started_at"] = started_at
        if parent_session_id:
            report["parent_session_id"] = parent_session_id
            report["parent_context_used"] = parent_summary is not None

        # 5. Write report to S3
        _write_report(bucket, report_key, report)

    except FileNotFoundError as exc:
        logger.error("Artifact missing: %s", exc)
        _write_report(
            bucket,
            report_key,
            {
                "report_version": "1.0",
                "session_id": session_id,
                "overall_status": "ERROR",
                "error": str(exc),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "started_at": started_at,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in VisualQA-Worker: %s", exc)
        _write_report(
            bucket,
            report_key,
            {
                "report_version": "1.0",
                "session_id": session_id,
                "overall_status": "ERROR",
                "error": str(exc),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "started_at": started_at,
            },
        )
