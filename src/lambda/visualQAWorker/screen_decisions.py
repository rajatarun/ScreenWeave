"""
ScreenWeave — Gemini Decisions API screen checks (opt-in).

Answers configurable yes/no, choice, and score questions over a screenshot or
short video clip in one Decisions call, sending pixels directly (no OCR).

Opt-in: SCREENWEAVE_DECISIONS=1 (default off). When off, callers use their
existing heuristics unchanged. When on, the Decisions path runs first; on API
error, timeout, missing key, malformed response, or low confidence the module
falls back and records which path was used.

Auth: GEMINI_API_KEY (never logged). Model: gemini-decision-1, API v1beta only.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

logger = logging.getLogger(__name__)

FLAG_ENV = "SCREENWEAVE_DECISIONS"
DEFAULT_MIN_CONFIDENCE = float(os.environ.get("SCREENWEAVE_DECISIONS_MIN_CONFIDENCE", "0.6"))
DEFAULT_TIMEOUT_SEC = float(os.environ.get("SCREENWEAVE_DECISIONS_TIMEOUT_SEC", "15"))
MODEL_ID = "gemini-decision-1"
DEFAULT_API_VERSION = "v1beta"

# Preset used by preprocessor format selection (UI vs photographic).
UI_CONTENT_QUESTIONS: dict[str, dict[str, Any]] = {
    "is_ui_screenshot": {
        "type": "noul",
        "instructions": (
            "Does this image show a software user-interface screenshot "
            "(website or application UI with controls, text, and layout) "
            "rather than a photograph of the physical world?"
        ),
        "criteria": {
            "true_description": "Screen capture of an application or website interface.",
            "false_description": (
                "Photograph of real-world scenery, people, or objects; "
                "or a non-UI illustration with no app chrome."
            ),
        },
    },
}


def decisions_enabled(env: Mapping[str, str] | None = None) -> bool:
    """True only when SCREENWEAVE_DECISIONS is explicitly on."""
    value = (env if env is not None else os.environ).get(FLAG_ENV, "")
    return value.strip().lower() in ("1", "true", "on", "yes")


class DecisionsClient(Protocol):
    """Minimal surface of google.genai.Client.decisions for testing."""

    class _Decisions:
        def create(
            self,
            *,
            model: str,
            input: Any,
            questions: dict[str, Any],
        ) -> Any: ...

    decisions: _Decisions


@dataclass(frozen=True)
class NormalizedAnswer:
    """Portable answer shape regardless of question type."""

    question_key: str
    answer_type: str
    confidence: float
    noul: float | None = None
    choice: str | None = None
    probabilities: dict[str, float] | None = None
    score: float | None = None
    legend: dict[str, str] | None = None


@dataclass
class ScreenCheckResult:
    """Outcome of a screen check attempt."""

    answers: dict[str, NormalizedAnswer] = field(default_factory=dict)
    path: str = "heuristic"  # decisions | heuristic | heuristic_fallback
    fallback_reason: str | None = None
    usage: dict[str, Any] | None = None
    duration_ms: int | None = None


def _mime_from_bytes(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        return "video/mp4"
    return "application/octet-stream"


def build_media_input(
    data: bytes,
    *,
    mime_type: str | None = None,
    context_text: str | None = None,
    resolution: str = "low",
) -> list[dict[str, Any]]:
    """
    Build Decisions API content blocks for image or video bytes plus optional text.
    """
    mime = mime_type or _mime_from_bytes(data)
    if mime == "application/octet-stream":
        raise ValueError("Could not infer MIME type; pass mime_type explicitly")

    blocks: list[dict[str, Any]] = []
    if context_text and context_text.strip():
        blocks.append({"type": "text", "text": context_text.strip()})

    if mime.startswith("video/"):
        block: dict[str, Any] = {
            "type": "video",
            "data": base64.b64encode(data).decode("ascii"),
            "mime_type": mime,
            "resolution": resolution,
        }
    else:
        block = {
            "type": "image",
            "data": base64.b64encode(data).decode("ascii"),
            "mime_type": mime,
            "resolution": resolution,
        }
    blocks.append(block)
    return blocks


def _normalize_answer(key: str, raw: Any) -> NormalizedAnswer | None:
    try:
        answer_type = getattr(raw, "type", None) or (raw.get("type") if isinstance(raw, dict) else None)
        conf = float(getattr(raw, "confidence", None) or (raw.get("confidence") if isinstance(raw, dict) else 0))
    except (TypeError, ValueError):
        return None

    if answer_type == "noul":
        try:
            noul = float(getattr(raw, "noul", None) if not isinstance(raw, dict) else raw["noul"])
        except (TypeError, ValueError, KeyError):
            return None
        return NormalizedAnswer(key, "noul", conf, noul=noul)

    if answer_type == "choice":
        try:
            choice = getattr(raw, "choice", None) if not isinstance(raw, dict) else raw["choice"]
            probs = getattr(raw, "probabilities", None) if not isinstance(raw, dict) else raw.get("probabilities")
        except (TypeError, ValueError):
            return None
        if not choice:
            return None
        prob_map = dict(probs) if probs else None
        return NormalizedAnswer(key, "choice", conf, choice=str(choice), probabilities=prob_map)

    if answer_type == "score":
        try:
            score = float(getattr(raw, "score", None) if not isinstance(raw, dict) else raw["score"])
            probs = getattr(raw, "probabilities", None) if not isinstance(raw, dict) else raw.get("probabilities")
            legend = getattr(raw, "legend", None) if not isinstance(raw, dict) else raw.get("legend")
        except (TypeError, ValueError, KeyError):
            return None
        prob_map = dict(probs) if probs else None
        legend_map = dict(legend) if legend else None
        return NormalizedAnswer(key, "score", conf, score=score, probabilities=prob_map, legend=legend_map)

    return None


def _parse_response(result: Any, question_keys: set[str]) -> dict[str, NormalizedAnswer]:
    answers_raw = getattr(result, "answers", None)
    if answers_raw is None and isinstance(result, dict):
        answers_raw = result.get("answers")
    if not isinstance(answers_raw, dict):
        raise ValueError("malformed_response: missing answers map")

    parsed: dict[str, NormalizedAnswer] = {}
    for key in question_keys:
        if key not in answers_raw:
            raise ValueError(f"malformed_response: missing answer for {key}")
        norm = _normalize_answer(key, answers_raw[key])
        if norm is None:
            raise ValueError(f"malformed_response: could not parse answer for {key}")
        parsed[key] = norm
    return parsed


def _usage_dict(result: Any) -> dict[str, Any] | None:
    usage = getattr(result, "usage", None)
    if usage is None and isinstance(result, dict):
        usage = result.get("usage")
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage
    out: dict[str, Any] = {}
    for attr in ("total_input_tokens", "total_output_tokens", "total_tokens"):
        val = getattr(usage, attr, None)
        if val is not None:
            out[attr] = val
    return out or None


def _low_confidence(answers: dict[str, NormalizedAnswer], min_confidence: float) -> bool:
    return any(a.confidence < min_confidence for a in answers.values())


def create_genai_client() -> DecisionsClient | None:
    """Build a real Gemini client when GEMINI_API_KEY is set."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from google import genai  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("google-genai not installed; Decisions path unavailable")
        return None
    return genai.Client(api_key=api_key, http_options={"api_version": DEFAULT_API_VERSION})


def run_screen_check(
    data: bytes,
    questions: dict[str, Any],
    *,
    mime_type: str | None = None,
    context_text: str | None = None,
    client: DecisionsClient | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    resolution: str = "low",
    enabled: bool | None = None,
) -> ScreenCheckResult:
    """
    Run configured Decisions questions over image/video bytes.

    When disabled (default), returns immediately with path=heuristic and empty answers.
    """
    if enabled is None:
        enabled = decisions_enabled()
    if not enabled:
        return ScreenCheckResult(path="heuristic", fallback_reason="flag_off")

    if not questions:
        return ScreenCheckResult(path="heuristic_fallback", fallback_reason="empty_questions")

    effective_client = client if client is not None else create_genai_client()
    if effective_client is None:
        return ScreenCheckResult(path="heuristic_fallback", fallback_reason="missing_client")

    try:
        input_blocks = build_media_input(
            data, mime_type=mime_type, context_text=context_text, resolution=resolution
        )
    except ValueError as exc:
        return ScreenCheckResult(path="heuristic_fallback", fallback_reason=str(exc))

    started = time.monotonic()

    def _call() -> Any:
        return effective_client.decisions.create(
            model=MODEL_ID,
            input=input_blocks,
            questions=questions,
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_call)
            result = future.result(timeout=timeout_sec)
    except FuturesTimeoutError:
        return ScreenCheckResult(
            path="heuristic_fallback",
            fallback_reason="timeout",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001 — API errors become fallback
        logger.info("Decisions API error (%s); using fallback", type(exc).__name__)
        return ScreenCheckResult(
            path="heuristic_fallback",
            fallback_reason=f"api_error:{type(exc).__name__}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        answers = _parse_response(result, set(questions))
    except ValueError as exc:
        return ScreenCheckResult(
            path="heuristic_fallback",
            fallback_reason=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    if _low_confidence(answers, min_confidence):
        return ScreenCheckResult(
            answers=answers,
            path="heuristic_fallback",
            fallback_reason="low_confidence",
            usage=_usage_dict(result),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "screen_decisions path=decisions duration_ms=%d keys=%s",
        duration_ms,
        ",".join(sorted(answers)),
    )
    return ScreenCheckResult(
        answers=answers,
        path="decisions",
        usage=_usage_dict(result),
        duration_ms=duration_ms,
    )


def resolve_is_ui_content(
    image_bytes: bytes,
    heuristic_fn: Callable[[], bool],
    *,
    client: DecisionsClient | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> tuple[bool, str]:
    """
    Decide whether screenshot bytes look like UI content.

    Fast path: Gemini Decisions when enabled and successful.
    Fallback: heuristic_fn (image_classifier.is_ui_content).
    """
    check = run_screen_check(
        image_bytes,
        UI_CONTENT_QUESTIONS,
        client=client,
        min_confidence=min_confidence,
    )
    if check.path == "decisions":
        ans = check.answers.get("is_ui_screenshot")
        if ans and ans.noul is not None:
            return ans.noul >= 0.5, "decisions"
    return heuristic_fn(), "heuristic_fallback" if decisions_enabled() else "heuristic"


def image_bytes_from_pil(img: Any, fmt: str = "PNG") -> bytes:
    """Encode a Pillow image to bytes for Decisions input."""
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()