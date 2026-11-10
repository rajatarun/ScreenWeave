# Extending ScreenWeave

## MCP tools (primary extension point)

New capabilities exposed to agents should be added as MCP tools in `src/lambda/mcpServer/index.mjs`:

1. Add an entry to `TOOL_DEFINITIONS` with JSON Schema `inputSchema`.
2. Implement handler logic (inline or import from a sibling module like `exportSession.mjs`).
3. Wrap the handler with `withObservability(name, fn)` so optional telemetry stays consistent.
4. Register scope in `auth.mjs` → `TOOL_SCOPES` (`read` vs `execute`).
5. Add Node tests under `src/lambda/mcpServer/test/`.

The dispatcher routes `tools/call` → `params.name` → your handler. Errors should surface as JSON-RPC errors with clear messages (see existing `crawl_url` validation).

**Protocol:** Supported MCP protocol versions are listed in `MCP_SUPPORTED_PROTOCOL_VERSIONS`. If clients need a newer revision, add it only when this server's surface truthfully matches that revision.

## Observatory spans

When `OBSERVATORY_METRICS_TABLE` is set, spans must satisfy `contracts/observatory_metrics_item.json`. Use `contracts/conformance.mjs` (`checkItem`) in tests. Do not change invariant checks in one language without updating the other (see file header comments).

## Crawler behavior

`src/crawler/crawl.py` runs on EC2. Changes deploy by uploading to `CrawlerCodeBucket` (`crawler/crawl.py`), not via Lambda code update.

Extension ideas:

- New capture modes (e.g. viewport sizes) → state metadata in `states.json`
- Additional guards in `guard_request` / `is_internal_host`

Keep parity with Lambda URL validation in `crawlTarget.mjs` for defense in depth.

## Visual QA pipeline

`src/lambda/visualQAWorker/`:

- **Tier routing:** `image_classifier.py` — adjust thresholds or add signals
- **Cache:** `cache.py` — perceptual hash cache in DynamoDB
- **Preprocessing:** `preprocessor.py` — image bytes before Bedrock

Worker env vars come from `infra/main-stack.yaml`. Long-running changes need timeout/memory review (900s / 3 GB default).

## Infrastructure

Single template: `infra/main-stack.yaml`. New resources should:

- Use stack parameters with safe defaults
- Grant least privilege on new IAM statements
- Export URLs/names via `Outputs` when user-facing

## Optional cross-weave hooks

| Hook | Env / parameter |
|------|-------------------|
| ContextWeave export | `CONTEXTWEAVE_RAW_BUCKET` |
| Shared metrics | `OBSERVATORY_METRICS_TABLE` |
| Video render worker | `VIDEO_WORKER_FUNCTION` |
| Auth | `AuthMode`, CipherWeave JWKS parameters |

Blank optional env vars should fail with clear configuration errors, not opaque AWS exceptions (`export_session` pattern).

## What not to fork casually

- `auth.mjs` verification rules (keep consistent with your organization's token issuer)
- S3 key layout under `screenweave/<session_id>/` (downstream tools depend on it)
