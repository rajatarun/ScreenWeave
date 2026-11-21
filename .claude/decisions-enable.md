# Enabling Gemini Decisions (UI-content check) in production

## What it does

When on, the Visual QA worker's preprocessor asks Gemini Decisions whether a screenshot looks like UI content before choosing PNG vs JPEG. On any API error, timeout, low confidence, or missing key, behaviour matches the existing Pillow heuristic.

Default state: **off** (`SCREENWEAVE_DECISIONS=0`). No `WEAVE_DECISIONS_LIVE` in production.

## Prerequisites

1. **Secret** in AWS Secrets Manager (region **us-east-1**):
   - Name: `gemini/api_key`
   - Value: plain API key string, or JSON with an `api_key` field
2. **Deploy** this stack revision (IAM grants `secretsmanager:GetSecretValue` on `arn:aws:secretsmanager:us-east-1:<account>:secret:gemini/api_key-*` only).

## Turn on

1. In GitHub: **Settings → Secrets and variables → Actions → Variables**
2. Add or set repository variable:
   - **Name:** `SCREENWEAVE_DECISIONS`
   - **Value:** `1`
3. Merge to `main` (or run **Deploy ScreenWeave** workflow dispatch) so deploy passes `SCREENWEAVE_DECISIONS=1` to the SAM parameter on **VisualQA-Worker**.

Worker environment (from template):

| Variable | Value |
|----------|--------|
| `SCREENWEAVE_DECISIONS` | `1` (from repo variable via SAM) |
| `GEMINI_SECRET_NAME` | `gemini/api_key` |
| `GEMINI_SECRET_REGION` | `us-east-1` |

Images are sent to Decisions with `resolution: low`.

## Verify

- CloudWatch: `/aws/lambda/<stack>-visualqa-worker` — look for `screen_decisions path=decisions` or `heuristic_fallback` with reasons.
- With flag `0`, logs show `UI content detection path=heuristic`.

## Rollback

1. Set repository variable `SCREENWEAVE_DECISIONS` to `0` (or delete the variable — deploy defaults to `0`).
2. Redeploy from `main`.
3. No secret rotation required; the worker stops calling Gemini when the flag is `0`.

Optional local CLI: `export SCREENWEAVE_DECISIONS=1 GEMINI_API_KEY=...` and `scripts/screen_decisions_cli.py` (never set `WEAVE_DECISIONS_LIVE` in prod).