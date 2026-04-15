"""
ScreenWeave Visual QA — Trigger Lambda

Exposes POST /visual-qa on the REST API Gateway.
Validates the session_id, fires the VisualQA-Worker Lambda asynchronously,
and immediately returns 202 so the caller is not blocked.

Optional: pass parent_session_id in the request body. If provided, the Worker
will fetch that session's QA report, summarize it with Claude, and inject it
as prior context before analysing the current session. If the parent report is
not found in S3 the Worker silently skips the step and proceeds normally.

The Worker writes its report to:
  s3://{ARTIFACTS_BUCKET}/{BUCKET_PREFIX}/{session_id}/qa_report.json
"""

import json
import logging
import os
import re

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Same pattern the existing McpServerFunction uses for session IDs
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{4,64}$")

_lambda = boto3.client("lambda")


def _parse_body(event: dict) -> dict:
    """Return parsed JSON body dict, or empty dict on failure."""
    body_raw = event.get("body") or ""
    if body_raw:
        try:
            parsed = json.loads(body_raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


def _parse_session_id(event: dict) -> str | None:
    """Extract session_id from POST body JSON or query-string parameters."""
    body = _parse_body(event)
    if body.get("session_id"):
        return body["session_id"]

    qs = event.get("queryStringParameters") or {}
    return qs.get("session_id")


def _parse_parent_session_id(event: dict) -> str | None:
    """Extract optional parent_session_id from POST body only."""
    return _parse_body(event).get("parent_session_id") or None


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event: dict, context) -> dict:
    logger.info("VisualQA-Trigger invoked")

    session_id = _parse_session_id(event)

    if not session_id:
        return _response(400, {"error": "session_id is required (body JSON or query-string)"})

    if not _SESSION_ID_RE.match(session_id):
        return _response(
            400,
            {
                "error": (
                    "session_id must be 4-64 characters: "
                    "letters, digits, hyphens, and underscores only"
                )
            },
        )

    # Optional: caller may supply a parent session whose QA report will be
    # summarised and used as prior context in the Worker. Invalid IDs are
    # silently dropped so a bad parent never blocks the current analysis.
    parent_session_id = _parse_parent_session_id(event)
    if parent_session_id and not _SESSION_ID_RE.match(parent_session_id):
        logger.warning("Ignoring invalid parent_session_id: %r", parent_session_id)
        parent_session_id = None

    bucket = os.environ["ARTIFACTS_BUCKET"]
    prefix = os.environ.get("BUCKET_PREFIX", "screenweave")
    worker_fn = os.environ["WORKER_FUNCTION_NAME"]  # "VisualQA-Worker"

    report_s3_key = f"{prefix}/{session_id}/qa_report.json"

    worker_payload: dict = {
        "session_id": session_id,
        "bucket": bucket,
        "prefix": prefix,
    }
    if parent_session_id:
        worker_payload["parent_session_id"] = parent_session_id

    # InvocationType=Event → async fire-and-forget; API Gateway returns 202 immediately.
    # The Worker runs up to 900 s and writes the report directly to S3.
    _lambda.invoke(
        FunctionName=worker_fn,
        InvocationType="Event",
        Payload=json.dumps(worker_payload).encode(),
    )

    logger.info(
        "Fired worker for session_id=%s parent_session_id=%s",
        session_id,
        parent_session_id or "none",
    )

    response_body: dict = {
        "job_id": session_id,
        "status": "RUNNING",
        "report_s3_key": report_s3_key,
        "message": f"QA job started. Poll s3://{bucket}/{report_s3_key} for results.",
    }
    if parent_session_id:
        response_body["parent_session_id"] = parent_session_id

    return _response(202, response_body)
