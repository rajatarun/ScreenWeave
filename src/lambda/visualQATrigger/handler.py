"""
ScreenWeave Visual QA — Trigger Lambda

Exposes POST /visual-qa on the REST API Gateway.
Validates the session_id, fires the VisualQA-Worker Lambda asynchronously,
and immediately returns 202 so the caller is not blocked.

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


def _parse_session_id(event: dict) -> str | None:
    """Extract session_id from POST body JSON or query-string parameters."""
    # 1. Try JSON body first
    body_raw = event.get("body") or ""
    if body_raw:
        try:
            body = json.loads(body_raw)
            if isinstance(body, dict) and body.get("session_id"):
                return body["session_id"]
        except json.JSONDecodeError:
            pass

    # 2. Fall back to query-string
    qs = event.get("queryStringParameters") or {}
    return qs.get("session_id")


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

    bucket = os.environ["ARTIFACTS_BUCKET"]
    prefix = os.environ.get("BUCKET_PREFIX", "screenweave")
    worker_fn = os.environ["WORKER_FUNCTION_NAME"]  # "VisualQA-Worker"

    report_s3_key = f"{prefix}/{session_id}/qa_report.json"

    payload = json.dumps(
        {
            "session_id": session_id,
            "bucket": bucket,
            "prefix": prefix,
        }
    ).encode()

    # InvocationType=Event → async fire-and-forget; API Gateway returns 202 immediately.
    # The Worker runs up to 900 s and writes the report directly to S3.
    _lambda.invoke(
        FunctionName=worker_fn,
        InvocationType="Event",
        Payload=payload,
    )

    logger.info("Fired worker for session_id=%s", session_id)

    return _response(
        202,
        {
            "job_id": session_id,
            "status": "RUNNING",
            "report_s3_key": report_s3_key,
            "message": (
                "QA job started. "
                f"Poll s3://{bucket}/{report_s3_key} for results."
            ),
        },
    )
