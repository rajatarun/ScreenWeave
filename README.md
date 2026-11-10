# ScreenWeave

**Turn any website into structured visual artifacts—screenshots, state graphs, and optional AI QA reports—and drive it from any MCP client over HTTP.**

ScreenWeave is for teams building **MCP-connected agents** or **AWS-native test pipelines** that need what users actually see, not just HTML. It crawls with Playwright on ephemeral EC2, stores artifacts in S3, and exposes crawl control and retrieval via MCP JSON-RPC. An optional Visual QA path analyzes completed sessions with Amazon Bedrock.

> **Demo GIF:** _Placeholder — add `docs/assets/demo.gif` (see [docs/assets/demo.gif-placeholder.txt](docs/assets/demo.gif-placeholder.txt))._

---

## Who it's for

| Audience | Use case |
|----------|----------|
| Agent builders | Ground tools with screenshots + `states.json` / `transitions.json` |
| Platform engineers | SAM-deployed crawl + QA in your AWS account |
| Weave integrators | `export_session` into ContextWeave; optional Observatory metrics |

**Requires AWS** for the stock deployment (EC2, S3, DynamoDB, Lambda, Bedrock for Visual QA). For offline development, run unit tests only (see [5-minute quickstart](#5-minute-quickstart)).

---

## 5-minute quickstart

### Path A — No AWS (verify the codebase)

```bash
git clone https://github.com/rajatarun/ScreenWeave.git
cd ScreenWeave
./scripts/run-local-tests.sh
```

This runs the MCP server's 110+ unit tests, Python tests, and a synthetic Observatory contract check. No cloud account needed.

### Path B — Deploy to your AWS account

**Prerequisites:** AWS CLI, [SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html), Bedrock access for Visual QA model IDs in your region.

```bash
git clone https://github.com/rajatarun/ScreenWeave.git
cd ScreenWeave
sam build --template-file infra/main-stack.yaml
sam deploy --stack-name screenweave --resolve-s3 --capabilities CAPABILITY_NAMED_IAM
```

Upload the crawler (not packaged as Lambda):

```bash
CRAWLER_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name screenweave \
  --query "Stacks[0].Outputs[?OutputKey=='CrawlerCodeBucketName'].OutputValue" \
  --output text)

aws s3 cp src/crawler/crawl.py "s3://${CRAWLER_BUCKET}/crawler/crawl.py" --sse AES256
```

Use stack outputs `McpEndpoint` and `McpClientConfig` for your MCP client.

---

## Core workflow — three copy-paste examples

Full curl variants: [examples/mcp-jsonrpc.md](examples/mcp-jsonrpc.md).

### 1. List MCP tools

```bash
curl -sS -X POST "$MCP_ENDPOINT" -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

### 2. Start a crawl

```bash
curl -sS -X POST "$MCP_ENDPOINT" -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"crawl_url","arguments":{"url":"https://example.com","max_depth":2,"max_links":12}}}'
```

### 3. Get screenshots after completion

```bash
curl -sS -X POST "$MCP_ENDPOINT" -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_screenshots","arguments":{"session_id":"YOUR_SESSION_ID"}}}'
```

Poll with `get_session_status` until `status` is `COMPLETED`.

---

## What you get

Two entry points:

| Entry point | Protocol | Purpose |
|---|---|---|
| `POST /mcp` | MCP JSON-RPC 2.0 over HTTP | Crawl, poll, screenshots, metrics, export |
| `POST /visual-qa` | REST (SigV4) | Async Bedrock QA report to S3 |

Artifacts layout:

```
s3://<ArtifactsBucket>/screenweave/<session_id>/
├── states.json
├── transitions.json
├── trace.zip
├── screenshots/state_NNNN.png
└── qa_report.json          # after Visual QA
```

Deep dive: [docs/architecture.md](docs/architecture.md).

---

## MCP tools (summary)

| Tool | Scope (when enforced) | Role |
|------|---------------------|------|
| `crawl_url` | execute | Start EC2 Playwright crawl |
| `get_session_status` | read | Poll RUNNING / COMPLETED / FAILED |
| `get_screenshots` | read | Pre-signed screenshot URLs |
| `get_metrics` | read | State counts, graph summary |
| `get_full_session` | read | Bundle of all artifacts |
| `export_session` | execute | Copy text/metadata to ContextWeave raw prefix (optional) |
| `generate_apple_video` | execute | Video job from URL or session (see [docs/swarm-to-video.md](docs/swarm-to-video.md)) |

Optional integrations: [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md). Adding tools: [docs/EXTENDING.md](docs/EXTENDING.md).

---

## Visual QA (REST)

Analyzes a **completed** crawl with tiered Bedrock routing (local heuristics → Haiku → Sonnet). See [docs/cost-calculation.md](docs/cost-calculation.md).

```bash
curl -X POST "https://<VisualQAEndpoint>" \
  --aws-sigv4 "aws:amz:us-east-1:execute-api" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "YOUR_SESSION_ID"}'
```

Fetch `qa_report.json` from the artifacts bucket when the job finishes.

---

## Authentication (MCP)

`/mcp` supports optional access tokens verified in-process (`src/lambda/mcpServer/auth.mjs`). Deploy parameter `AuthMode`:

- **`audit` (default):** Anonymous requests run (logged); invalid tokens are rejected.
- **`enforce`:** Valid token and per-tool scopes required.

Before exposing the endpoint publicly, use `enforce` and restrict network access. Details are intentionally high-level here; operators configure issuer/JWKS via SAM parameters. See [SECURITY.md](SECURITY.md) and [docs/security-notes.md](docs/security-notes.md).

---

## Configuration (optional)

Beyond defaults in `infra/main-stack.yaml`, optional integration and auth settings apply only when set:

| Env var | SAM parameter | Default | Description |
|---|---|---|---|
| `OBSERVATORY_METRICS_TABLE` | `ObservatoryMetricsTableName` | *(blank)* | Shared MCP invocation telemetry (`@weaveaijs/mcp-observatory`) |
| `CONTEXTWEAVE_RAW_BUCKET` | `ContextWeaveRawBucketName` | *(blank)* | Enables `export_session` to a ContextWeave raw-ingestion bucket |
| `AUTH_MODE` | `AuthMode` | `audit` | `audit` or `enforce` for `/mcp` |
| CipherWeave JWKS | `CipherWeaveJwks*` parameters | *(blank)* | Token verification keys when using an external issuer |

Set `ObservatoryMetricsTableArn` alongside `ObservatoryMetricsTableName` so IAM grants `dynamodb:PutItem` on that table only when the name is non-blank.

**Local `sam deploy`** (placeholders only — use your account's names and ARNs):

```bash
sam deploy --resolve-s3 --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ObservatoryMetricsTableName=<shared-observatory-metrics-table> \
    ObservatoryMetricsTableArn=arn:aws:dynamodb:<region>:123456789012:table/<shared-observatory-metrics-table> \
    ContextWeaveRawBucketName=<contextweave-raw-ingestion-bucket>
```

For **GitHub Actions**, set repository variables and secrets per [docs/DEPLOY_CONFIG.md](docs/DEPLOY_CONFIG.md) instead of embedding account-specific values in the repo. The deploy job validates required settings before `sam deploy`.

---

## CI/CD

Pushes to `main` that touch `src/`, `infra/main-stack.yaml`, or the workflow file trigger an automatic deploy via GitHub Actions (`.github/workflows/deploy.yaml`).

The workflow:

1. Authenticates to AWS using OIDC (no long-lived keys — see [docs/DEPLOY_CONFIG.md](docs/DEPLOY_CONFIG.md))
2. Runs tests, dependency audit, and secret scan on pull requests; deploy only on `main`
3. Runs `sam build && sam deploy` with repository variables/secrets
4. Uploads `crawl.py` to `CrawlerCodeBucket`
5. Smoke-tests the MCP endpoint and prints the URL to the job summary

---

## Testing

**Offline (no AWS):**

```bash
./scripts/run-local-tests.sh
```

**Individual suites:**

```bash
cd src/lambda/mcpServer && npm ci && npm test
python3 -m pytest -q tests   # see CONTRIBUTING.md for /opt/output
```

CI runs the same suites in `.github/workflows/deploy.yaml` (test, dependency audit, gitleaks).

---

## Repository layout

```
infra/
  main-stack.yaml          SAM/CloudFormation template
src/
  crawler/crawl.py         Playwright worker (EC2)
  lambda/mcpServer/        MCP server + node --test suite
  lambda/visualQA*/        Visual QA trigger + worker
scripts/
  run-local-tests.sh       Offline test harness
examples/
  mcp-jsonrpc.md           curl examples
  response.json            Sample artifact shape
docs/
  architecture.md          System reference
  DEPLOY_CONFIG.md         CI variables and deploy validation
  INTEGRATIONS.md          Integration opportunities
.github/workflows/
  deploy.yaml              CI/CD pipeline
```

---

## Contributing & security

- [CONTRIBUTING.md](CONTRIBUTING.md) — setup, tests, PR process  
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)  
- [SECURITY.md](SECURITY.md) — report vulnerabilities privately  
- [docs/GOOD_FIRST_ISSUES.md](docs/GOOD_FIRST_ISSUES.md) — starter tasks  

---

## License

Apache-2.0. See [LICENSE](LICENSE).
