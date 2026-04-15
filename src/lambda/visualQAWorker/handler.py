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
import io
import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from PIL import Image

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ── Constants ─────────────────────────────────────────────────────────────────

BATCH_SIZE = 10

BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-20250514-v1:0"

# Fields retained from each state object after pre-processing.
# links_found and the screenshot relative path are intentionally excluded
# to minimise token weight.
KEEP_FIELDS = frozenset(
    {"state_id", "url", "trigger_label", "visible_text_preview", "interactive_elements"}
)

MAX_IMG_BYTES = 5 * 1024 * 1024  # 5 MB — skip larger screenshots with a warning

# Target short edge (drives scale for normal screenshots).
MAX_IMG_DIMENSION = 512
# Hard cap on the long edge — Bedrock rejects > 2000 px in multi-image requests.
MAX_LONG_EDGE = 1568

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


def _resize_image(data: bytes) -> bytes:
    """
    Resize image preserving aspect ratio using two constraints:
      1. Short edge → MAX_IMG_DIMENSION (512 px) for normal screenshots
      2. Long edge  → MAX_LONG_EDGE (1568 px) hard cap for tall full-page captures

    Takes the more restrictive scale so neither limit is exceeded.
    No-op if the image already satisfies both constraints.

    Examples:
      1280x720   → scale=0.71 (short-edge drives) → 910x512
      1280x16937 → scale=0.09 (long-edge drives)  → 118x1568
    """
    img = Image.open(io.BytesIO(data))
    w, h = img.size
    scale = min(
        MAX_IMG_DIMENSION / min(w, h),  # short-edge target
        MAX_LONG_EDGE / max(w, h),      # long-edge hard cap
    )
    if scale >= 1.0:
        return data
    new_size = (int(w * scale), int(h * scale))
    logger.info("Resizing screenshot %dx%d → %dx%d", w, h, new_size[0], new_size[1])
    img = img.resize(new_size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _download_b64(bucket: str, key: str) -> str | None:
    """
    Download a PNG from S3, resize if any dimension exceeds MAX_IMG_DIMENSION,
    and return the base64 representation.
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
        data = _resize_image(data)
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


def _invoke_bedrock(messages: list[dict], max_tokens: int = 4096) -> str:
    """
    Call Claude via Bedrock with the full conversation history.
    Retries on ThrottlingException with exponential backoff.
    Returns the assistant's text response.
    """
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
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
    # Consolidation can produce large JSON for sessions with many states —
    # use a generous token budget so the response is never truncated mid-JSON.
    report_text = _invoke_bedrock(messages, max_tokens=16384)
    logger.info("Consolidation response received (%d chars)", len(report_text))

    # Parse Claude's JSON output, stripping markdown fences if present.
    def _strip_fences(text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            # Remove the opening fence line (```json or just ```)
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            # Remove the closing fence if present (tolerates truncated responses)
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3].rstrip()
        return text.strip()

    try:
        report = json.loads(report_text)
    except json.JSONDecodeError:
        try:
            report = json.loads(_strip_fences(report_text))
        except json.JSONDecodeError as exc:
            logger.error("Failed to parse Claude's JSON response: %s", exc)
            report = {
                "report_version": "1.0",
                "overall_status": "ERROR",
                "error": "Claude returned non-JSON response",
                "raw_response": report_text[:2000],
            }

    return report


def _generate_html_report(
    report: dict,
    pairs: list[tuple[dict, str | None]],
    bucket: str,
) -> str:
    """
    Build a rich self-contained HTML QA report with embedded screenshots.

    Each state gets a row in the findings table showing:
      status badge | state ID | URL | screenshot thumbnail | observations | issues
    """
    findings_map = {f["state_id"]: f for f in report.get("findings", [])}

    total        = len(pairs)
    passed_count = sum(1 for f in report.get("findings", []) if f.get("passed"))
    failed_count = total - passed_count
    overall      = report.get("overall_status", "UNKNOWN")
    session_id   = report.get("session_id", "")
    generated_at = report.get("generated_at", "")
    cross_batch  = report.get("cross_batch_observations", "")
    all_issues   = report.get("all_issues", [])

    status_colour = "#22c55e" if overall == "PASS" else "#ef4444"

    # ── Screenshot thumbnails (re-use existing resize pipeline) ──────────────
    thumbnails: dict[str, str] = {}
    for state, key in pairs:
        sid = state.get("state_id", "")
        if key:
            b64 = _download_b64(bucket, key)
            if b64:
                thumbnails[sid] = b64

    # ── Table rows ────────────────────────────────────────────────────────────
    rows: list[str] = []
    for state, _ in pairs:
        sid     = state.get("state_id", "")
        url     = state.get("url", "")
        finding = findings_map.get(sid, {})
        passed  = finding.get("passed", True)
        obs     = finding.get("observations", "—")
        issues  = finding.get("issues", [])

        badge = (
            '<span class="badge pass">PASS</span>'
            if passed else
            '<span class="badge fail">FAIL</span>'
        )
        img_tag = (
            f'<img src="data:image/png;base64,{thumbnails[sid]}" class="thumb" />'
            if sid in thumbnails else
            '<span class="no-img">—</span>'
        )
        issues_html = (
            "<ul>" + "".join(f"<li>{i}</li>" for i in issues) + "</ul>"
            if issues else "—"
        )
        row_cls = "pass-row" if passed else "fail-row"
        short_url = url.replace("https://", "").replace("http://", "")
        rows.append(f"""
      <tr class="{row_cls}">
        <td>{badge}</td>
        <td class="mono">{sid}</td>
        <td><a href="{url}" target="_blank" title="{url}">{short_url}</a></td>
        <td class="img-cell">{img_tag}</td>
        <td>{obs}</td>
        <td>{issues_html}</td>
      </tr>""")

    rows_html = "\n".join(rows)

    # ── All-issues summary ────────────────────────────────────────────────────
    if all_issues:
        issues_rows = "".join(
            f'<tr><td class="mono">{i.get("state_id","")}</td>'
            f'<td>{i.get("description","")}</td></tr>'
            for i in all_issues
        )
        issues_section = f"""
    <section>
      <h2>All Issues ({len(all_issues)})</h2>
      <table class="issues-table">
        <thead><tr><th>State</th><th>Description</th></tr></thead>
        <tbody>{issues_rows}</tbody>
      </table>
    </section>"""
    else:
        issues_section = ""

    cross_section = (
        f'<section><h2>Cross-batch Observations</h2><p>{cross_batch}</p></section>'
        if cross_batch else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>ScreenWeave QA — {session_id}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #e2e8f0; }}
    a {{ color: #60a5fa; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}

    header {{
      background: #1e293b;
      border-bottom: 3px solid {status_colour};
      padding: 24px 32px;
      display: flex;
      align-items: center;
      gap: 24px;
    }}
    .overall-badge {{
      font-size: 1.4rem;
      font-weight: 700;
      color: {status_colour};
      background: {status_colour}22;
      border: 2px solid {status_colour};
      border-radius: 8px;
      padding: 6px 18px;
      white-space: nowrap;
    }}
    .header-meta h1 {{ font-size: 1.1rem; color: #94a3b8; font-weight: 500; }}
    .header-meta p  {{ font-size: 0.82rem; color: #64748b; margin-top: 2px; }}

    .stats {{
      display: flex;
      gap: 16px;
      padding: 20px 32px;
      background: #1e293b;
      border-bottom: 1px solid #334155;
    }}
    .stat {{
      background: #0f172a;
      border: 1px solid #334155;
      border-radius: 8px;
      padding: 12px 20px;
      text-align: center;
      min-width: 100px;
    }}
    .stat .value {{ font-size: 1.8rem; font-weight: 700; }}
    .stat .label {{ font-size: 0.75rem; color: #64748b; text-transform: uppercase; letter-spacing: .05em; }}
    .stat.green .value {{ color: #22c55e; }}
    .stat.red   .value {{ color: #ef4444; }}
    .stat.blue  .value {{ color: #60a5fa; }}

    section {{ padding: 24px 32px; }}
    section h2 {{ font-size: 1rem; font-weight: 600; color: #94a3b8;
                  text-transform: uppercase; letter-spacing: .05em; margin-bottom: 14px; }}
    section p  {{ color: #cbd5e1; line-height: 1.6; }}

    table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
    th {{
      background: #1e293b;
      color: #94a3b8;
      text-align: left;
      padding: 10px 12px;
      font-weight: 600;
      text-transform: uppercase;
      font-size: 0.75rem;
      letter-spacing: .04em;
      position: sticky;
      top: 0;
      z-index: 1;
    }}
    td {{ padding: 10px 12px; border-bottom: 1px solid #1e293b; vertical-align: top; }}
    tr.pass-row {{ background: #0f2d1a; }}
    tr.fail-row {{ background: #2d0f0f; }}
    tr:hover td  {{ filter: brightness(1.15); }}

    .badge {{
      display: inline-block;
      font-size: 0.7rem;
      font-weight: 700;
      padding: 3px 8px;
      border-radius: 4px;
      text-transform: uppercase;
      letter-spacing: .05em;
    }}
    .badge.pass {{ background: #14532d; color: #22c55e; }}
    .badge.fail {{ background: #450a0a; color: #ef4444; }}

    .mono {{ font-family: ui-monospace, monospace; font-size: 0.8rem; color: #94a3b8; }}
    .img-cell {{ width: 200px; }}
    .thumb {{
      max-width: 180px;
      max-height: 140px;
      border-radius: 4px;
      border: 1px solid #334155;
      display: block;
    }}
    .no-img {{ color: #475569; font-size: 0.8rem; }}

    ul {{ padding-left: 16px; }}
    li {{ color: #fca5a5; margin-bottom: 4px; line-height: 1.4; }}

    .issues-table {{ margin-top: 0; }}
    .issues-table th, .issues-table td {{ padding: 8px 12px; }}
    .issues-table tr {{ background: #1e293b; }}
    .issues-table tr:hover td {{ filter: brightness(1.2); }}

    footer {{ padding: 16px 32px; color: #334155; font-size: 0.75rem; border-top: 1px solid #1e293b; }}
  </style>
</head>
<body>

<header>
  <div class="overall-badge">{overall}</div>
  <div class="header-meta">
    <h1>ScreenWeave Visual QA Report</h1>
    <p>Session: {session_id} &nbsp;·&nbsp; Generated: {generated_at}</p>
  </div>
</header>

<div class="stats">
  <div class="stat blue">
    <div class="value">{total}</div>
    <div class="label">States</div>
  </div>
  <div class="stat green">
    <div class="value">{passed_count}</div>
    <div class="label">Passed</div>
  </div>
  <div class="stat red">
    <div class="value">{failed_count}</div>
    <div class="label">Failed</div>
  </div>
  <div class="stat blue">
    <div class="value">{report.get("total_batches", "—")}</div>
    <div class="label">Batches</div>
  </div>
</div>

{cross_section}

<section>
  <h2>Findings ({total} states)</h2>
  <table>
    <thead>
      <tr>
        <th>Status</th>
        <th>State</th>
        <th>URL</th>
        <th>Screenshot</th>
        <th>Observations</th>
        <th>Issues</th>
      </tr>
    </thead>
    <tbody>
{rows_html}
    </tbody>
  </table>
</section>

{issues_section}

<footer>ScreenWeave · {session_id} · {generated_at}</footer>
</body>
</html>"""


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

        # 5. Write JSON report to S3
        _write_report(bucket, report_key, report)

        # 6. Generate and write HTML report
        html_key = report_key.replace(".json", ".html")
        logger.info("Generating HTML report → s3://%s/%s", bucket, html_key)
        html = _generate_html_report(report, pairs, bucket)
        _s3.put_object(
            Bucket=bucket,
            Key=html_key,
            Body=html.encode("utf-8"),
            ContentType="text/html; charset=utf-8",
        )
        logger.info("HTML report written (%d bytes)", len(html))

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
