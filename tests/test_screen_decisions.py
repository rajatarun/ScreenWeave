"""Unit tests for Gemini Decisions screen-check module (mocked client, no network)."""
from __future__ import annotations

import base64
import io
import json
import os
import pathlib
import sys
import time

import pytest
from PIL import Image, ImageDraw

WORKER = pathlib.Path(__file__).resolve().parents[1] / "src" / "lambda" / "visualQAWorker"
sys.path.insert(0, str(WORKER))

import screen_decisions as sd  # noqa: E402


def _png_bytes() -> bytes:
    img = Image.new("RGB", (400, 300), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 40, 200, 120], outline="black", width=2)
    draw.text((50, 70), "Login", fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class _Noul:
    type = "noul"

    def __init__(self, noul: float, confidence: float):
        self.noul = noul
        self.confidence = confidence


class _Choice:
    type = "choice"

    def __init__(self, choice: str, confidence: float, probabilities: dict):
        self.choice = choice
        self.confidence = confidence
        self.probabilities = probabilities


class _Usage:
    total_input_tokens = 42
    total_output_tokens = 0
    total_tokens = 42


class FakeDecisions:
    def __init__(self, *, answers=None, error: Exception | None = None, delay: float = 0):
        self._answers = answers or {}
        self._error = error
        self._delay = delay
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self._delay:
            time.sleep(self._delay)
        if self._error:
            raise self._error
        return type("Result", (), {"answers": self._answers, "usage": _Usage()})()


class FakeClient:
    def __init__(self, decisions: FakeDecisions):
        self.decisions = decisions


@pytest.fixture(autouse=True)
def _clear_decisions_flag(monkeypatch):
    monkeypatch.delenv(sd.FLAG_ENV, raising=False)
    # Keep GEMINI_API_KEY only for the opted-in live smoke test (skipif at collect time).
    if os.environ.get("WEAVE_DECISIONS_LIVE") != "1":
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def test_flag_off_returns_heuristic_path():
    out = sd.run_screen_check(_png_bytes(), sd.UI_CONTENT_QUESTIONS, enabled=False)
    assert out.path == "heuristic"
    assert out.fallback_reason == "flag_off"
    assert not out.answers


def test_missing_client_falls_back(monkeypatch):
    monkeypatch.setenv(sd.FLAG_ENV, "1")
    out = sd.run_screen_check(_png_bytes(), sd.UI_CONTENT_QUESTIONS, client=None, enabled=True)
    assert out.path == "heuristic_fallback"
    assert out.fallback_reason == "missing_client"


def test_successful_noul_answer():
    client = FakeClient(
        FakeDecisions(answers={"is_ui_screenshot": _Noul(0.91, 0.91)})
    )
    out = sd.run_screen_check(
        _png_bytes(),
        sd.UI_CONTENT_QUESTIONS,
        client=client,
        enabled=True,
    )
    assert out.path == "decisions"
    assert out.answers["is_ui_screenshot"].noul == 0.91


def test_low_confidence_falls_back():
    client = FakeClient(
        FakeDecisions(answers={"is_ui_screenshot": _Noul(0.99, 0.4)})
    )
    out = sd.run_screen_check(
        _png_bytes(),
        sd.UI_CONTENT_QUESTIONS,
        client=client,
        enabled=True,
        min_confidence=0.6,
    )
    assert out.path == "heuristic_fallback"
    assert out.fallback_reason == "low_confidence"


def test_api_error_falls_back():
    client = FakeClient(FakeDecisions(error=RuntimeError("boom")))
    out = sd.run_screen_check(
        _png_bytes(),
        sd.UI_CONTENT_QUESTIONS,
        client=client,
        enabled=True,
    )
    assert out.path == "heuristic_fallback"
    assert out.fallback_reason == "api_error:RuntimeError"


def test_timeout_falls_back():
    client = FakeClient(FakeDecisions(answers={"is_ui_screenshot": _Noul(0.9, 0.9)}, delay=0.2))
    out = sd.run_screen_check(
        _png_bytes(),
        sd.UI_CONTENT_QUESTIONS,
        client=client,
        enabled=True,
        timeout_sec=0.05,
    )
    assert out.path == "heuristic_fallback"
    assert out.fallback_reason == "timeout"


def test_malformed_response_falls_back():
    client = FakeClient(FakeDecisions(answers={}))
    out = sd.run_screen_check(
        _png_bytes(),
        sd.UI_CONTENT_QUESTIONS,
        client=client,
        enabled=True,
    )
    assert out.path == "heuristic_fallback"
    assert out.fallback_reason and "malformed" in out.fallback_reason


def test_image_payload_construction():
    blocks = sd.build_media_input(_png_bytes(), context_text="Session abc")
    assert len(blocks) == 2
    assert blocks[0]["type"] == "text"
    assert blocks[1]["type"] == "image"
    assert blocks[1]["mime_type"] == "image/png"
    assert blocks[1]["resolution"] == "low"
    raw = base64.b64decode(blocks[1]["data"])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"


def test_choice_and_score_parsing():
    client = FakeClient(
        FakeDecisions(
            answers={
                "screen": _Choice("login", 0.88, {"login": 0.88, "home": 0.12}),
            }
        )
    )
    out = sd.run_screen_check(
        _png_bytes(),
        {
            "screen": {
                "type": "choice",
                "instructions": "Which screen is shown?",
                "criteria": {"login": "", "home": ""},
            }
        },
        client=client,
        enabled=True,
    )
    assert out.path == "decisions"
    assert out.answers["screen"].choice == "login"


def test_resolve_is_ui_content_uses_heuristic_when_flag_off():
    is_ui, path = sd.resolve_is_ui_content(_png_bytes(), lambda: True)
    assert is_ui is True
    assert path == "heuristic"


@pytest.mark.skipif(
    not (os.environ.get("GEMINI_API_KEY") and os.environ.get("WEAVE_DECISIONS_LIVE") == "1"),
    reason="Set GEMINI_API_KEY and WEAVE_DECISIONS_LIVE=1 for live Decisions smoke test",
)
def test_live_decisions_smoke():
    """Paid live call — skipped unless explicitly opted in."""
    out = sd.run_screen_check(
        _png_bytes(),
        sd.UI_CONTENT_QUESTIONS,
        enabled=True,
        min_confidence=0.5,
    )
    assert out.path in ("decisions", "heuristic_fallback")


def test_decisions_enabled_values():
    assert sd.decisions_enabled({sd.FLAG_ENV: "1"})
    assert sd.decisions_enabled({sd.FLAG_ENV: "on"})
    assert not sd.decisions_enabled({})