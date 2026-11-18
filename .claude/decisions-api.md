# Gemini Decisions API — ScreenWeave integration

## Purpose

Optional fast path for **screen-state checks** on crawled screenshots (and short clips) using Google's **Decisions API** (`gemini-decision-1`, Developer API **v1beta**). One call can answer many independent questions (yes/no, choice, score) over the same pixels—no OCR or separate vision-parse step.

## Opt-in

| Variable | Default | Meaning |
|----------|---------|---------|
| `SCREENWEAVE_DECISIONS` | off | `1` / `on` enables the Decisions path |
| `GEMINI_API_KEY` | unset | Required when Decisions is on |
| `SCREENWEAVE_DECISIONS_MIN_CONFIDENCE` | `0.6` | Below this, fall back to heuristics |
| `SCREENWEAVE_DECISIONS_TIMEOUT_SEC` | `15` | Per-call timeout |
| `WEAVE_DECISIONS_LIVE` | off | With `GEMINI_API_KEY`, enables the skipped live pytest |

When `SCREENWEAVE_DECISIONS` is unset, behaviour is unchanged from pre-integration builds.

## Where it runs today

1. **`preprocessor.py`** — UI vs photographic format selection. When enabled, `resolve_is_ui_content()` asks Decisions whether the capture looks like a UI screenshot; on any failure or low confidence it uses the existing Pillow colour-dominance heuristic (`image_classifier.is_ui_content`).

## Module surface

- **File:** `src/lambda/visualQAWorker/screen_decisions.py`
- **`run_screen_check(bytes, questions, ...)`** — general API; returns `ScreenCheckResult` with `path` (`decisions` | `heuristic` | `heuristic_fallback`) and normalized answers.
- **`build_media_input`** — image/video + optional text blocks for the API.
- **`resolve_is_ui_content`** — preset wrapper for preprocessor routing.
- **CLI:** `scripts/screen_decisions_cli.py` (`--preset ui-content` or `--questions file.json`).

## Fallback rules

Always record `path` / `fallback_reason`. Fall back to existing logic on:

- Flag off, missing API key, missing `google-genai`
- Timeout, API error, malformed response
- Any answer below `SCREENWEAVE_DECISIONS_MIN_CONFIDENCE`

Telemetry: structured **logging** only in the worker (`path`, `duration_ms`). MCP Observatory spans are unchanged (Python worker has no DynamoDB telemetry writer).

## API shape (reference)

```python
from google import genai

client = genai.Client(http_options={"api_version": "v1beta"})
result = client.decisions.create(
    model="gemini-decision-1",
    input=[
        {"type": "text", "text": "optional context"},
        {"type": "image", "data": "<base64>", "mime_type": "image/png", "resolution": "low"},
    ],
    questions={
        "is_error_dialog": {
            "type": "noul",
            "instructions": "Is a modal error dialog visible?",
        },
    },
)
```

Docs: https://aistudio.google.com/docs/decisions-api

## Testing

- `tests/test_screen_decisions.py` — mocked `DecisionsClient`, no network.
- Live smoke: `WEAVE_DECISIONS_LIVE=1` + `GEMINI_API_KEY` (skipped in CI).

## Cost note

Decisions bills **input tokens only** (output tokens are 0). Use `resolution: low` on images/video unless fine detail is required.

## Public repo

No internal endpoints or credentials in docs or commits. Never commit `GEMINI_API_KEY`.