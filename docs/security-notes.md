# Security notes (threat model)

This document summarizes how ScreenWeave is exposed and what operators should harden. It is not a formal audit.

## Assets

| Asset | Storage / location |
|-------|-------------------|
| Crawl screenshots, JSON, traces | S3 `ArtifactsBucket` |
| Session index | DynamoDB `SessionsTable` |
| Crawler script | S3 `CrawlerCodeBucket` |
| QA reports | S3 under `screenweave/<session_id>/qa_report.json` |
| Optional telemetry | DynamoDB Observatory table (when configured) |

## Entry points

1. **`POST /mcp`** — MCP JSON-RPC. Can launch EC2 instances (`crawl_url`), read session artifacts, and optionally export to another bucket (`export_session`).
2. **`POST /visual-qa`** — Starts an async Lambda worker; requires SigV4/IAM on API Gateway as deployed.
3. **EC2 crawler** — No inbound ports; uses Playwright with a request guard that blocks internal/metadata addresses (mirrors Lambda-side URL validation).

## Authentication (MCP)

Access tokens are verified when presented (`src/lambda/mcpServer/auth.mjs`). Deploy-time `AuthMode` controls behavior:

- **`audit` (default):** Requests without a token still run; invalid tokens are rejected.
- **`enforce`:** Valid token and per-tool scopes are required.

OAuth protected-resource metadata is served at `GET /.well-known/oauth-protected-resource`. Token issuance is external to this repository; configure issuer/JWKS via SAM parameters.

Do not expose `/mcp` on the public internet without a deliberate auth and rate-limit strategy.

## Crawl abuse

`crawl_url` spends EC2 time and writes to your S3 bucket. Mitigations in code today:

- URL validation rejecting private/internal targets (`crawlTarget.mjs`, `crawl.py` guard)
- `MAX_CONCURRENT_CRAWLERS` cap on simultaneous instances
- API Gateway throttling on the MCP HTTP API (see `infra/main-stack.yaml`)

Operators should still use WAF, IP allow lists, or private API Gateway integrations where appropriate.

## Data handling

- Artifacts expire via S3 lifecycle (90 days in the default template).
- Session rows support DynamoDB TTL.
- Pre-signed URLs are time-limited (`SignedUrlExpiresSeconds`).

## Dependency and supply chain

CI runs `npm audit`, `pip-audit`, and `gitleaks` on pull requests and `main` (see `.github/workflows/deploy.yaml`). Keep deploy roles scoped to what the SAM template requires.

## Out of scope for this repo

- DPoP replay ledger (not implemented server-side; short proof lifetime limits exposure)
- Multi-tenant isolation beyond single-stack IAM and bucket policies

If you find a gap, report it per [SECURITY.md](../SECURITY.md).
