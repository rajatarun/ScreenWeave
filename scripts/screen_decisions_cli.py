#!/usr/bin/env python3
"""
CLI for ScreenWeave Gemini Decisions screen checks.

Examples:
  export SCREENWEAVE_DECISIONS=1 GEMINI_API_KEY=...
  python scripts/screen_decisions_cli.py --image shot.png --preset ui-content
  python scripts/screen_decisions_cli.py --image shot.png --questions questions.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKER = ROOT / "src" / "lambda" / "visualQAWorker"
sys.path.insert(0, str(WORKER))

import screen_decisions as sd  # noqa: E402

PRESETS = {
    "ui-content": sd.UI_CONTENT_QUESTIONS,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Gemini Decisions screen checks")
    parser.add_argument("--image", type=pathlib.Path, help="Screenshot image file")
    parser.add_argument("--video", type=pathlib.Path, help="Short screen clip (video)")
    parser.add_argument("--text", default="", help="Optional context text")
    parser.add_argument("--preset", choices=sorted(PRESETS), help="Built-in question set")
    parser.add_argument("--questions", type=pathlib.Path, help="JSON file with questions map")
    parser.add_argument("--min-confidence", type=float, default=sd.DEFAULT_MIN_CONFIDENCE)
    args = parser.parse_args()

    if bool(args.image) == bool(args.video):
        parser.error("Provide exactly one of --image or --video")

    if args.questions:
        questions = json.loads(args.questions.read_text())
    elif args.preset:
        questions = PRESETS[args.preset]
    else:
        parser.error("Provide --preset or --questions")

    data = (args.image or args.video).read_bytes()
    mime = None
    if args.video:
        mime = "video/mp4"

    if not sd.decisions_enabled():
        print(
            json.dumps(
                {
                    "error": "SCREENWEAVE_DECISIONS is not enabled",
                    "hint": "export SCREENWEAVE_DECISIONS=1",
                },
                indent=2,
            )
        )
        return 2

    result = sd.run_screen_check(
        data,
        questions,
        mime_type=mime,
        context_text=args.text or None,
        min_confidence=args.min_confidence,
    )
    payload = {
        "path": result.path,
        "fallback_reason": result.fallback_reason,
        "duration_ms": result.duration_ms,
        "usage": result.usage,
        "answers": {
            k: {
                "type": v.answer_type,
                "confidence": v.confidence,
                "noul": v.noul,
                "choice": v.choice,
                "score": v.score,
                "probabilities": v.probabilities,
            }
            for k, v in result.answers.items()
        },
    }
    print(json.dumps(payload, indent=2))
    return 0 if result.path == "decisions" else 1


if __name__ == "__main__":
    raise SystemExit(main())