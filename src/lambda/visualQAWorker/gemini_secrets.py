"""Load Gemini API key from env or AWS Secrets Manager (cached per execution environment)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_SECRET_NAME = "gemini/api_key"
DEFAULT_SECRET_REGION = "us-east-1"

_cache: str | None = None
_load_attempted = False


def _parse_secret_string(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(payload, dict):
            for key in ("api_key", "API_KEY", "gemini_api_key", "GEMINI_API_KEY", "key"):
                val = payload.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        return None
    return text


def get_gemini_api_key() -> str | None:
    """
    Resolve Gemini API key: GEMINI_API_KEY env first, else Secrets Manager once per warm container.
    Never logs secret values.
    """
    global _cache, _load_attempted

    direct = os.environ.get("GEMINI_API_KEY", "").strip()
    if direct:
        return direct

    if _load_attempted:
        return _cache

    _load_attempted = True
    secret_name = os.environ.get("GEMINI_SECRET_NAME", DEFAULT_SECRET_NAME).strip() or DEFAULT_SECRET_NAME
    region = os.environ.get("GEMINI_SECRET_REGION", DEFAULT_SECRET_REGION).strip() or DEFAULT_SECRET_REGION

    try:
        import boto3  # noqa: PLC0415 — Lambda runtime provides boto3

        client = boto3.client("secretsmanager", region_name=region)
        resp = client.get_secret_value(SecretId=secret_name)
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "Gemini secret unavailable (secret=%s region=%s): %s",
            secret_name,
            region,
            type(exc).__name__,
        )
        _cache = None
        return None

    raw = resp.get("SecretString")
    if raw is None and resp.get("SecretBinary"):
        try:
            raw = resp["SecretBinary"].decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            raw = None

    if not raw:
        logger.info("Gemini secret empty (secret=%s)", secret_name)
        _cache = None
        return None

    _cache = _parse_secret_string(raw)
    if not _cache:
        logger.info("Gemini secret could not be parsed (secret=%s)", secret_name)
    return _cache


def reset_gemini_key_cache_for_tests() -> None:
    """Clear in-process cache (unit tests only)."""
    global _cache, _load_attempted
    _cache = None
    _load_attempted = False