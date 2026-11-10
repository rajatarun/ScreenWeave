# Integration opportunities

ScreenWeave is already an **MCP server over HTTP** and a **REST Visual QA API**. Below are ecosystems where adapters or configuration would help developers adopt it without bespoke glue code.

Legend: **Implemented** = ships in this repo today; **Planned** = sensible fit, not built.

| Integration | Fit | Adapter shape | Effort | Status |
|-------------|-----|---------------|--------|--------|
| MCP clients (Claude Desktop, IDE MCP, custom agents) | **High** | Paste `McpClientConfig` stack output; call `tools/call` | Low (docs + URL) | **Implemented** |
| AWS Bedrock AgentCore Gateway | **High** | Gateway target → MCP endpoint; protocol negotiation in `index.mjs` | Low–Med | **Implemented** (HTTP MCP) |
| `@weaveaijs/mcp-observatory` / shared DynamoDB metrics | **Med** | Set `OBSERVATORY_METRICS_TABLE` + IAM; spans via `observatory.mjs` | Low | **Implemented** |
| Raw-ingestion export | **Med** | `export_session` → `CONTEXTWEAVE_RAW_BUCKET` | Low | **Implemented** |
| OpenTelemetry (traces/metrics) | **Med** | Export invocation spans from Observatory or Lambda Powertools; correlate `session_id` | Med | Planned |
| LangChain / LangGraph tools | **High** | Thin wrapper: `StructuredTool` per MCP tool or single `ScreenWeaveToolkit` using HTTP JSON-RPC | Med | Planned |
| Anthropic / OpenAI SDK (non-Bedrock) | **Low–Med** | Visual QA today is Bedrock-only; optional worker backend for reports | High | Planned |
| CI visual regression (Playwright, Percy-like) | **Med** | `crawl_url` + poll + download screenshots via pre-signed URLs; diff in CI | Med | Planned |
| GitHub Actions composite action | **Med** | Action: deploy optional, call MCP, upload artifacts to workflow | Med | Planned |
| Slack / chatops | **Low** | Bot posts QA `overall_status` from S3 report | Low–Med | Planned |
| Local Playwright-only mode (no AWS) | **Med** | Run `crawl.py` with env vars + upload to local disk | High | Planned |

## MCP clients (high fit)

**What exists:** `POST /mcp` implements `initialize`, `tools/list`, `tools/call`, and `ping`. Clients need only the HTTPS URL (no AWS keys on the client).

**Adapter:** None beyond client config. Optional bearer token when `AuthMode=enforce`.

**Example:** See [examples/mcp-jsonrpc.md](../examples/mcp-jsonrpc.md).

## LangChain / LangGraph (high fit, planned)

**Adapter sketch:**

```python
# Planned pattern — not shipped in this repo
from langchain_core.tools import tool
import httpx

@tool
def screenweave_crawl(url: str, max_depth: int = 2) -> str:
    """Start a ScreenWeave crawl; returns session_id."""
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "crawl_url", "arguments": {"url": url, "max_depth": max_depth}},
    }
    r = httpx.post(MCP_URL, json=payload, headers={"Authorization": f"Bearer {TOKEN}"})
    return r.text
```

**Effort:** ~1–2 days for a minimal toolkit + session polling helper.

## OpenTelemetry (medium fit, planned)

**What exists:** Optional DynamoDB observatory records per tool call (tool name, args hash, duration, outcome).

**Adapter:** Bridge `InvocationWrapper` spans to OTLP (Node `@opentelemetry/sdk-node` in the MCP Lambda layer or sidecar). Visual QA worker already has X-Ray enabled in the template.

**Effort:** Medium; needs decisions on sampling and PII in args hashes.

## Bedrock / AgentCore (high fit)

**What exists:** Visual QA uses `bedrock:InvokeModel`; MCP server documents AgentCore Gateway compatibility for protocol versions.

**Adapter:** AgentCore Gateway MCP target configuration pointing at `McpEndpoint`.

## Raw-ingestion export (medium fit)

**What exists:** `export_session` copies text/metadata to a configured raw bucket with a generated `summary.md`.

**Adapter:** Set `ContextWeaveRawBucketName` at deploy time; point your indexing or retrieval pipeline at the `raw/screenweave/` prefix.

## Local-first note

ScreenWeave **requires AWS** for the stock deployment (EC2 crawler, S3, DynamoDB, Lambda). There is no fully local-first mode today. For development without deploy, use unit tests and offline modules under `tests/` (see [scripts/run-local-tests.sh](../scripts/run-local-tests.sh)).
