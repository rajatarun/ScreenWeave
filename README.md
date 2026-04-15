# ScreenWeave

ScreenWeave is an AWS-native website crawling and visual QA platform. It uses Playwright to walk a site, capture every visual state (including interactive UI transitions), and persist structured artifacts to S3. A separate Visual QA pipeline feeds those artifacts through Claude 3.5 Sonnet via Amazon Bedrock to produce a structured report of anomalies, regressions, and cross-page inconsistencies.

Two independent entry points are exposed:

| Entry point | Protocol | Purpose |
|---|---|---|
| `POST /mcp` | MCP JSON-RPC 2.0 over HTTP | Trigger crawls, poll status, retrieve screenshots and metrics from any MCP-compatible client |
| `POST /visual-qa` | REST | Start an asynchronous Visual QA job against a completed crawl session |

---

## Quick start

### 1. Deploy

```bash
git clone https://github.com/rajatarun/ScreenWeave
cd ScreenWeave
sam build --template-file infra/main-stack.yaml
sam deploy --resolve-s3 --capabilities CAPABILITY_NAMED_IAM
```

After deploy, note the two stack outputs:

```
McpEndpoint      → https://<id>.execute-api.<region>.amazonaws.com/mcp
VisualQAEndpoint → https://<id>.execute-api.<region>.amazonaws.com/v1/visual-qa
```

### 2. Connect an MCP client

Paste the `McpClientConfig` stack output directly into your MCP client configuration:

```json
{
  "mcpServers": {
    "screenweave": {
      "url": "https://<McpHttpApi>.execute-api.<region>.amazonaws.com/mcp"
    }
  }
}
```

No AWS credentials are required on the client side.

### 3. Upload the crawler script

`crawl.py` runs on EC2, not Lambda. Upload it after the first deploy:

```bash
CRAWLER_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name screenweave \
  --query "Stacks[0].Outputs[?OutputKey=='CrawlerCodeBucketName'].OutputValue" \
  --output text)

aws s3 cp src/crawler/crawl.py s3://${CRAWLER_BUCKET}/crawler/crawl.py --sse AES256
```

This step is automated in CI (`.github/workflows/deploy.yaml`).

---

## MCP tools

All tools are invoked via `POST /mcp` using standard MCP JSON-RPC 2.0.

### `crawl_url`

Launch a crawl session against a target URL. Returns a `session_id` immediately while an EC2 worker runs in the background.

```json
{ "url": "https://example.com", "max_depth": 2, "max_links": 12 }
```

### `get_session_status`

Poll the status of a crawl session (`RUNNING`, `COMPLETED`, or `FAILED`).

```json
{ "session_id": "sess-abc123" }
```

### `get_screenshots`

Retrieve pre-signed S3 URLs for all screenshots captured in a session.

```json
{ "session_id": "sess-abc123" }
```

### `get_metrics`

Return computed crawl metrics: total states, unique URLs, transition graph summary, duration.

```json
{ "session_id": "sess-abc123" }
```

### `get_full_session`

Return all artifacts for a session in one call: screenshots, `states.json`, `transitions.json`, `trace.zip`.

```json
{ "session_id": "sess-abc123" }
```

---

## Visual QA

The Visual QA pipeline analyses a completed crawl session with Claude 3.5 Sonnet via Bedrock. Screenshots are processed in batches of 10; Claude maintains shared conversation context across all batches so it can detect cross-page regressions and inconsistencies.

### Start a QA job

```bash
curl -X POST https://<VisualQAEndpoint> \
  -H "Content-Type: application/json" \
  -d '{"session_id": "sess-abc123"}'
```

Response (`202 Accepted`):

```json
{
  "job_id": "sess-abc123",
  "status": "RUNNING",
  "report_s3_key": "screenweave/sess-abc123/qa_report.json",
  "message": "QA job started. Poll the report key for results."
}
```

### Use prior session context

Pass `parent_session_id` to inject a summary of a previous session's QA report as context before the current analysis begins. Useful for regression detection across deploys.

```bash
curl -X POST https://<VisualQAEndpoint> \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "sess-new",
    "parent_session_id": "sess-previous"
  }'
```

If the parent report is not found in S3, the step is silently skipped and analysis proceeds normally.

### Fetch the report

The report is written to S3 when the job completes:

```bash
aws s3 cp s3://<ArtifactsBucket>/screenweave/<session_id>/qa_report.json .
```

Report shape:

```json
{
  "report_version": "1.0",
  "overall_status": "PASS",
  "session_id": "sess-abc123",
  "total_states_analyzed": 24,
  "total_batches": 3,
  "findings": [
    {
      "state_id": "state_0001",
      "url": "https://example.com/",
      "passed": true,
      "observations": "...",
      "issues": []
    }
  ],
  "cross_batch_observations": "...",
  "all_issues": [],
  "generated_at": "2026-04-15T10:30:00Z"
}
```

---

## S3 artifact layout

```
s3://<ArtifactsBucket>/
└── screenweave/
    └── <session_id>/
        ├── states.json          # Structured metadata for every crawled state
        ├── transitions.json     # Directed graph of page transitions
        ├── trace.zip            # Playwright trace (open with: playwright show-trace)
        ├── screenshots/
        │   ├── state_0001.png
        │   ├── state_0002.png
        │   └── ...
        └── qa_report.json       # Written by Visual QA Worker on completion
```

---

## CI/CD

Pushes to `main` that touch `src/`, `infra/main-stack.yaml`, or the workflow file trigger an automatic deploy via GitHub Actions (`.github/workflows/deploy.yaml`).

The workflow:
1. Authenticates to AWS using OIDC (no long-lived keys — set `ASSUME_ROLE_ARN` in repository variables)
2. Runs `sam build && sam deploy`
3. Uploads `crawl.py` to `CrawlerCodeBucket`
4. Smoke-tests the MCP endpoint and prints the URL to the job summary

---

## Repository layout

```
infra/
  main-stack.yaml          SAM/CloudFormation template — all infrastructure
src/
  crawler/
    crawl.py               Playwright crawler (runs on EC2)
  lambda/
    mcpServer/
      index.mjs            MCP JSON-RPC server (Node.js 20, HTTP API)
    visualQATrigger/
      handler.py           REST API entry-point — validates & fires Worker (Python 3.12)
    visualQAWorker/
      handler.py           Async Bedrock orchestrator — multi-turn Claude QA (Python 3.12)
.github/
  workflows/
    deploy.yaml            CI/CD pipeline
examples/
  response.json            Example get_full_session response
docs/
  architecture.md          Detailed architecture reference
```

---

## Prerequisites

- AWS CLI configured with permissions to deploy CloudFormation, create IAM roles, and launch EC2
- AWS SAM CLI (`brew install aws-sam-cli` or see the [installation guide](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html))
- Amazon Bedrock model access enabled for `anthropic.claude-3-5-sonnet-20241022-v2:0` in your region

---

## License

Apache-2.0. See [LICENSE](LICENSE).
