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
  4. Pre-download        → download + preprocess all screenshots; build image_store
  5. Smart batch         → group by URL for temporal locality, adaptive batch size
  6. Multi-turn Bedrock  → per-batch tier routing (heuristic / Haiku / Sonnet);
                           cache lookup before each batch, store after consolidation
  7. Consolidate        → final turn asks Claude for the structured JSON report
  8. Write report       → PUT qa_report.json (+ qa_report.html) to S3

Cost-optimisation principles implemented (see individual modules):
  P1 – Stratified model routing      (image_classifier.py + _run_visual_qa)
  P2 – Batch processing w/ locality  (_smart_batch)
  P3 – Perceptual hash caching       (cache.py + _run_visual_qa)
  P4 – Progressive enhancement       (image_classifier.py tiers)
  P5 – Infrastructure optimisations  (preprocessor.py + _download_processed)

Report is written to: s3://{bucket}/{prefix}/{session_id}/qa_report.json
"""

import base64
import io
import json
import logging
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from PIL import Image

import cache as _cache
import image_classifier as _classifier
import preprocessor as _preprocessor

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ── Constants ─────────────────────────────────────────────────────────────────

# Model IDs — Principle 1: Stratified Model Routing
BEDROCK_MODEL_SONNET = os.environ.get(
    "SONNET_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0"
)
BEDROCK_MODEL_HAIKU = os.environ.get(
    "HAIKU_MODEL_ID", "us.anthropic.claude-3-5-haiku-20241022-v1:0"
)
BEDROCK_MODEL = BEDROCK_MODEL_SONNET  # backward-compat alias

# Feature flags (can be toggled via env vars without redeployment)
ROUTING_ENABLED = os.environ.get("ROUTING_ENABLED", "true").lower() == "true"
CACHE_ENABLED   = os.environ.get("CACHE_ENABLED",   "true").lower() == "true"
CACHE_TABLE     = os.environ.get("CACHE_TABLE", "")

# Pre-flight token budget per image — skip images that would cost too much
MAX_TOKEN_COST_PER_IMAGE = int(os.environ.get("MAX_TOKENS_PER_IMAGE", "5000"))

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

_s3      = boto3.client("s3")
_dynamo  = boto3.client("dynamodb")
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


def _smart_batch(
    pairs: list[tuple[dict, str | None]],
) -> list[list[tuple[dict, str | None]]]:
    """
    Principle 2: Batch processing with temporal locality.

    Groups states by URL so Claude sees all screenshots of the same page
    in a single batch (contextual similarity).  Within each URL group the
    batch size is adaptive:
      ≥ 8 states from the same page  → size 15 (scroll states, simpler)
      3–7 states                     → size 10 (standard)
      1–2 states                     → size 5  (isolated, may be complex)

    States are already in temporal order from the crawler, so URL grouping
    preserves chronological sequence within each page.
    """
    # Group by URL while preserving insertion order
    groups: dict[str, list] = defaultdict(list)
    for pair in pairs:
        state, key = pair
        groups[state.get("url", "")].append(pair)

    batches: list[list] = []
    for url, url_pairs in groups.items():
        n = len(url_pairs)
        size = 15 if n >= 8 else (10 if n >= 3 else 5)
        for i in range(0, n, size):
            batches.append(url_pairs[i : i + size])

    logger.info(
        "Smart batch: %d states across %d URLs → %d batches",
        len(pairs), len(groups), len(batches),
    )
    return batches


def _download_processed(
    bucket: str, key: str
) -> tuple[bytes | None, str, str | None]:
    """
    Principle 5: Download, preprocess (resize + format selection), and fingerprint.

    Returns (processed_bytes_or_None, media_type, phash_or_None).

    Processing steps:
      1. Download raw bytes from S3; skip if > MAX_IMG_BYTES
      2. preprocessor.preprocess() → resize to ≤1024px wide, PNG (UI) or JPEG 80% (photo)
      3. Token pre-flight: skip if estimated tokens > MAX_TOKEN_COST_PER_IMAGE
      4. cache.compute_phash() → 64-bit dHash for cache lookup
    """
    try:
        obj = _s3.get_object(Bucket=bucket, Key=key)
        content_length = obj.get("ContentLength", 0)
        if content_length > MAX_IMG_BYTES:
            logger.warning(
                "Screenshot %s is %d bytes (> %d MB limit), skipping",
                key, content_length, MAX_IMG_BYTES // (1024 * 1024),
            )
            return None, "image/png", None
        raw = obj["Body"].read()
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("NoSuchKey", "404"):
            logger.warning("Screenshot not found: %s", key)
            return None, "image/png", None
        raise

    # Principle 5a: format selection + dimension capping
    processed, media_type = _preprocessor.preprocess(raw)

    # Principle 5b: token pre-flight cost check
    token_est = _preprocessor.estimate_tokens(processed)
    if token_est > MAX_TOKEN_COST_PER_IMAGE:
        logger.warning(
            "Screenshot %s: ~%d tokens exceeds limit of %d, skipping",
            key, token_est, MAX_TOKEN_COST_PER_IMAGE,
        )
        return None, media_type, None

    # Principle 3: perceptual fingerprint for cache lookup
    phash = _cache.compute_phash(processed) if CACHE_ENABLED else None
    return processed, media_type, phash


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
    images: list[tuple[str | None, str]],
    batch_index: int,
    total_batches: int,
    cached_interpretations: dict[str, str] | None = None,
) -> list[dict]:
    """
    Build the content array for a single user turn covering one batch.

    Principle 3: cached_interpretations maps state_id → prior analysis text.
    For cached states, a text block replaces the image block (no image sent).
    For uncached states, an image block is emitted with the correct media_type
    (image/png or image/jpeg — Principle 5 format selection).

    images is a list of (b64_or_None, media_type) parallel to batch.
    """
    cached_interpretations = cached_interpretations or {}
    content: list[dict] = []

    # Emit cached-text blocks first, then image blocks
    img_seq = 0
    for (state, _), (b64, media_type) in zip(batch, images):
        sid = state.get("state_id", "")
        if sid in cached_interpretations:
            content.append({
                "type": "text",
                "text": f"[CACHED ANALYSIS] {sid}: {cached_interpretations[sid]}",
            })
        elif b64 is not None:
            img_seq += 1
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64,
                },
            })

    # Screenshot index map (helps Claude correlate images to state IDs)
    index_lines: list[str] = []
    img_seq = 0
    for (state, _), (b64, _) in zip(batch, images):
        sid = state.get("state_id", "")
        url = state.get("url", "")
        trigger = state.get("trigger_label") or "n/a"
        if sid in cached_interpretations:
            label = f"CACHED: {sid} | {url} | trigger: {trigger}"
        elif b64 is not None:
            img_seq += 1
            label = f"Image {img_seq}: {sid} | {url} | trigger: {trigger}"
        else:
            label = f"MISSING: {sid} | {url} | trigger: {trigger}"
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
        "Review the screenshots and cached analyses. Describe what you observe and flag any "
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


def _invoke_bedrock(
    messages: list[dict],
    max_tokens: int = 4096,
    model_id: str | None = None,
) -> str:
    """
    Call Claude via Bedrock with the full conversation history.

    Principle 1: model_id selects the routing tier at call time.
      None / omitted → BEDROCK_MODEL_SONNET (default, highest quality)
      BEDROCK_MODEL_HAIKU → cheaper, ~2–3× faster for routine batches

    Retries on ThrottlingException with exponential backoff.
    Returns the assistant's text response.
    """
    effective_model = model_id or BEDROCK_MODEL_SONNET
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
            logger.info(
                "Bedrock throttle retry %d (%s), sleeping %ds",
                attempt, effective_model.split(".")[-1], delay,
            )
            time.sleep(delay)
        try:
            resp = _bedrock.invoke_model(
                modelId=effective_model,
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
    image_store: dict[str, tuple[bytes | None, str, str | None]],
    parent_summary: str | None = None,
) -> dict:
    """
    Drive the multi-turn Bedrock conversation across all batches.

    image_store maps state_id → (processed_bytes_or_None, media_type, phash_or_None)
    and is built by the caller via _download_processed() so images are fetched once.

    Integrates all five cost-efficiency principles:
      P1 – Model routing: selects Haiku or Sonnet based on batch complexity
      P2 – Smart batching: already applied by caller (_smart_batch)
      P3 – Cache lookup before each batch; store per-state after consolidation
      P4 – Tier routing: heuristic (no API) / Haiku / Sonnet
      P5 – Preprocessed images (format + size) already in image_store

    Returns the parsed QA report dict (with routing_stats attached).
    """
    messages: list[dict] = []
    total_batches = len(batches)
    total_states  = sum(len(b) for b in batches)
    stats = {"heuristic": 0, "haiku": 0, "sonnet": 0, "cache_hits": 0}

    # ── Inject parent context ─────────────────────────────────────────────────
    if parent_summary:
        messages.append({
            "role": "user",
            "content": [{
                "type": "text",
                "text": (
                    "Before we begin, here is a summary of a prior related QA session. "
                    "Keep these findings in mind — they may help you spot regressions "
                    "or confirm improvements in the current session.\n\n"
                    f"PRIOR SESSION SUMMARY:\n{parent_summary}"
                ),
            }],
        })
        messages.append({
            "role": "assistant",
            "content": (
                "Understood. I've noted the prior session findings and will use them "
                "as reference context when reviewing the current session — particularly "
                "for regression detection and cross-session consistency."
            ),
        })
        logger.info("Parent context injected (%d chars)", len(parent_summary))

    # ── Process batches ───────────────────────────────────────────────────────
    for batch_idx, batch in enumerate(batches):
        logger.info("Processing batch %d/%d (%d states)", batch_idx + 1, total_batches, len(batch))

        # Principle 3: cache lookup for every state in this batch
        cached: dict[str, str] = {}
        for state, _ in batch:
            sid   = state.get("state_id", "")
            entry = image_store.get(sid)
            phash = entry[2] if entry else None
            if phash and CACHE_ENABLED and CACHE_TABLE:
                hit = _cache.lookup(phash, _dynamo, CACHE_TABLE)
                if hit:
                    cached[sid] = hit
                    stats["cache_hits"] += 1

        # Compute complexity only for states not already cached
        uncached_complexities: list[float] = []
        for state, _ in batch:
            sid   = state.get("state_id", "")
            if sid in cached:
                continue
            entry = image_store.get(sid)
            if entry and entry[0]:
                c = _classifier.compute_complexity_score(entry[0], state)
            else:
                c = 0.3  # default when no image available
            uncached_complexities.append(c)

        logger.info(
            "  cached=%d uncached=%d",
            len(cached), len(uncached_complexities),
        )

        # ── Full cache hit: synthetic turn, no Bedrock call ───────────────────
        if not uncached_complexities:
            cached_lines = "\n\n".join(
                f"[CACHED] {state.get('state_id','')}: {cached.get(state.get('state_id',''), '')}"
                for state, _ in batch
            )
            messages.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        f"--- BATCH {batch_idx + 1} of {total_batches} "
                        f"(ALL {len(batch)} STATES FROM CACHE) ---\n\n"
                        + cached_lines
                    ),
                }],
            })
            messages.append({
                "role": "assistant",
                "content": (
                    f"Noted. All {len(batch)} states in batch {batch_idx + 1} "
                    "retrieved from cache — carrying forward."
                ),
            })
            logger.info("  Batch %d: full cache hit, no Bedrock call", batch_idx + 1)
            continue

        # Principle 1 + 4: determine tier from max complexity of uncached states
        max_complexity = max(uncached_complexities)
        tier = _classifier.select_tier(max_complexity) if ROUTING_ENABLED else "sonnet"
        logger.info("  max_complexity=%.3f → tier=%s", max_complexity, tier)

        # ── Tier 1: Heuristic — local assessment, no Bedrock ─────────────────
        if tier == "heuristic":
            heuristic_parts: list[str] = []
            for state, _ in batch:
                sid = state.get("state_id", "")
                if sid not in cached:
                    assessment = _classifier.heuristic_assessment(state)
                    cached[sid] = assessment
                    heuristic_parts.append(assessment)
                    stats["heuristic"] += 1

            messages.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        f"--- BATCH {batch_idx + 1} of {total_batches} (HEURISTIC TIER) ---\n\n"
                        + "\n\n".join(heuristic_parts)
                    ),
                }],
            })
            messages.append({
                "role": "assistant",
                "content": (
                    f"Noted. {len(heuristic_parts)} states assessed at heuristic tier "
                    "— all appear structurally standard for their page type."
                ),
            })
            logger.info("  Batch %d: heuristic tier, %d states", batch_idx + 1, len(heuristic_parts))
            continue

        # ── Tier 2 / 3: Haiku or Sonnet — call Bedrock ───────────────────────
        model_id = BEDROCK_MODEL_HAIKU if tier == "haiku" else BEDROCK_MODEL_SONNET

        # Build (b64_or_None, media_type) for each state (cached → None)
        images: list[tuple[str | None, str]] = []
        for state, _ in batch:
            sid   = state.get("state_id", "")
            entry = image_store.get(sid)
            if sid in cached or entry is None or entry[0] is None:
                images.append((None, "image/png"))
            else:
                b64 = base64.b64encode(entry[0]).decode("ascii")
                images.append((b64, entry[1]))

        loaded = sum(1 for b64, _ in images if b64 is not None)
        logger.info("  %d/%d screenshots loaded for Bedrock", loaded, len(batch))

        user_content = _build_batch_user_turn(
            batch, images, batch_idx, total_batches, cached
        )
        messages.append({"role": "user", "content": user_content})

        assistant_text = _invoke_bedrock(messages, model_id=model_id)
        logger.info(
            "  Batch %d (%s) done (%d chars)", batch_idx + 1, tier, len(assistant_text)
        )
        messages.append({"role": "assistant", "content": assistant_text})

        non_cached = len(batch) - len(cached)
        if tier == "haiku":
            stats["haiku"] += non_cached
        else:
            stats["sonnet"] += non_cached

    # ── Final consolidation — always Sonnet for best report quality ───────────
    messages.append({
        "role": "user",
        "content": _build_consolidation_turn(total_states, total_batches),
    })
    report_text = _invoke_bedrock(
        messages, max_tokens=16384, model_id=BEDROCK_MODEL_SONNET
    )
    logger.info("Consolidation response received (%d chars)", len(report_text))

    def _strip_fences(text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
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

    # Principle 3: store per-state observations in cache after consolidation
    if CACHE_ENABLED and CACHE_TABLE:
        findings_map = {f.get("state_id"): f for f in report.get("findings", [])}
        for batch in batches:
            for state, _ in batch:
                sid   = state.get("state_id", "")
                entry = image_store.get(sid)
                phash = entry[2] if entry else None
                if not phash:
                    continue
                finding = findings_map.get(sid)
                if finding and finding.get("observations"):
                    _cache.store(phash, finding["observations"], _dynamo, CACHE_TABLE)

    report["routing_stats"] = stats
    logger.info(
        "Routing summary: heuristic=%d haiku=%d sonnet=%d cache_hits=%d",
        stats["heuristic"], stats["haiku"], stats["sonnet"], stats["cache_hits"],
    )
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

    # Routing stats for the cost-optimisation section
    routing   = report.get("routing_stats", {})
    cache_hits      = routing.get("cache_hits", 0)
    haiku_count     = routing.get("haiku", 0)
    sonnet_count    = routing.get("sonnet", 0)
    heuristic_count = routing.get("heuristic", 0)

    # ── Screenshot thumbnails ─────────────────────────────────────────────────
    # Use _download_processed so thumbnails benefit from format/size optimisation.
    thumbnails: dict[str, tuple[str, str]] = {}   # state_id → (b64, media_type)
    for state, key in pairs:
        sid = state.get("state_id", "")
        if key:
            proc, mt, _ = _download_processed(bucket, key)
            if proc:
                thumbnails[sid] = (base64.b64encode(proc).decode("ascii"), mt)

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
        if sid in thumbnails:
            thumb_b64, thumb_mt = thumbnails[sid]
            img_tag = f'<img src="data:{thumb_mt};base64,{thumb_b64}" class="thumb" />'
        else:
            img_tag = '<span class="no-img">—</span>'
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
  <div class="stat green">
    <div class="value">{cache_hits}</div>
    <div class="label">Cached</div>
  </div>
  <div class="stat blue">
    <div class="value">{heuristic_count}</div>
    <div class="label">Heuristic</div>
  </div>
  <div class="stat blue">
    <div class="value">{haiku_count}</div>
    <div class="label">Haiku</div>
  </div>
  <div class="stat blue">
    <div class="value">{sonnet_count}</div>
    <div class="label">Sonnet</div>
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

        # 3. Pre-download + preprocess all screenshots (Principle 5)
        # image_store: state_id → (processed_bytes_or_None, media_type, phash_or_None)
        image_store: dict[str, tuple[bytes | None, str, str | None]] = {}
        for state, key in pairs:
            sid = state.get("state_id", "")
            if key:
                proc, mt, phash = _download_processed(bucket, key)
                image_store[sid] = (proc, mt, phash)
            else:
                image_store[sid] = (None, "image/png", None)
        logger.info("Pre-downloaded %d screenshots", len(image_store))

        # 4. Smart batch (Principle 2: temporal locality + URL grouping)
        batches = _smart_batch(pairs)

        # 5. Run multi-turn Visual QA (parent_summary may be None — that's fine)
        report = _run_visual_qa(batches, image_store, parent_summary=parent_summary)

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
