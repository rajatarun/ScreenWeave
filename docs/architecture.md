# ScreenWeave — Architecture

## Overview

ScreenWeave is deployed as a single AWS SAM/CloudFormation stack (`infra/main-stack.yaml`). All infrastructure — S3 buckets, DynamoDB table, IAM roles, Lambda functions, EC2 security group, and API Gateway endpoints — is declared in that one template and provisioned together.

There are two independent subsystems:

1. **Crawl Pipeline** — an MCP-compatible HTTP API that triggers an EC2 worker to crawl a site with Playwright and persist artifacts to S3.
2. **Visual QA Pipeline** — a REST API that reads those artifacts, feeds them through Claude 3.5 Sonnet via Bedrock in a multi-turn conversation, and writes a structured QA report back to S3.

---

## System diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│  Client (MCP-compatible agent or curl)                              │
└──────────────┬──────────────────────────────┬───────────────────────┘
               │ MCP JSON-RPC 2.0             │ REST POST /visual-qa
               ▼                              ▼
┌─────────────────────────┐    ┌──────────────────────────────────────┐
│  API Gateway HTTP API   │    │  API Gateway REST API (v1)           │
│  (stage: $default)      │    │  (stage: v1)                         │
└──────────┬──────────────┘    └──────────────┬───────────────────────┘
           │                                  │
           ▼                                  ▼
┌─────────────────────────┐    ┌──────────────────────────────────────┐
│  McpServerFunction      │    │  VisualQATriggerFunction             │
│  Node.js 20 / 29s       │    │  Python 3.12 / 10s                   │
│                         │    │                                      │
│  Tools:                 │    │  • Validates session_id              │
│  • crawl_url            │    │  • Forwards parent_session_id        │
│  • get_session_status   │    │  • Invokes Worker (async)            │
│  • get_screenshots      │    │  • Returns 202 immediately           │
│  • get_metrics          │    └──────────────┬───────────────────────┘
│  • get_full_session     │                   │ InvocationType=Event
└──────────┬──────────────┘                   ▼
           │ RunInstances               ┌──────────────────────────────┐
           │                           │  VisualQAWorkerFunction       │
           ▼                           │  Python 3.12 / 900s / 3 GB   │
┌─────────────────────────┐            │                              │
│  EC2 Crawler Instance   │            │  Step 0: fetch & summarise   │
│  (AL2023, t3.medium)    │            │    parent qa_report.json     │
│                         │            │    (if parent_session_id)    │
│  crawl.py (Playwright)  │            │                              │
│  • Walks site           │            │  Step 1-4: discover states,  │
│  • Captures screenshots │            │    strip fields, pair        │
│  • Writes states.json   │            │    screenshots, batch (×10)  │
│  • Writes transitions   │            │                              │
│  • Uploads to S3        │            │  Step 5: multi-turn Bedrock  │
│  • Updates DynamoDB     │            │    [user+assistant] × batch  │
│  • Self-terminates      │            │    → shared Claude context   │
└──────────┬──────────────┘            │                              │
           │                           │  Step 6: consolidation turn  │
           │ PutObject / UpdateItem    │    → structured JSON report  │
           ▼                           │                              │
┌──────────────────────────────────────┤  Step 7: PutObject report    │
│  Amazon S3 (ArtifactsBucket)        │    to S3                     │
│                                     └──────────────┬───────────────┘
│  screenweave/<session_id>/                         │ GetObject / PutObject
│  ├── states.json                    ◄──────────────┘
│  ├── transitions.json
│  ├── trace.zip
│  ├── screenshots/state_NNNN.png
│  └── qa_report.json
└──────────────────────────────────────┐
           ▲                           │
           │ GetItem / PutItem         │
┌──────────┴──────────────┐            │
│  DynamoDB SessionsTable │            │
│  PK: session_id         │            │
│  status, stats,         │            │
│  artifact_manifest, ttl │            │
└─────────────────────────┘            │
                                       │ InvokeModel
                           ┌───────────┴───────────────┐
                           │  Amazon Bedrock            │
                           │  claude-3-5-sonnet         │
                           │  -20241022-v2:0            │
                           └───────────────────────────┘
```

---

## Components

### API Gateway — MCP HTTP API (`McpHttpApi`)

- Type: `AWS::Serverless::HttpApi` (API Gateway v2), stage `$default`
- Routes: `POST /mcp`, `GET /mcp`
- CORS: `*` origins, `content-type` + `accept` headers
- Throttling: 50 burst / 25 RPS
- Access logs → `McpApiLogGroup` (30-day retention)

### API Gateway — Visual QA REST API (`VisualQAApi`)

- Type: `AWS::Serverless::Api` (API Gateway v1), stage `v1`
- Route: `POST /visual-qa`
- CORS: `*` origins, `content-type` header
- Intentionally no throttle config — callers are expected to be low-frequency

### McpServerFunction

- Runtime: Node.js 20, 512 MB, 29s timeout (API Gateway limit)
- Implements the full MCP JSON-RPC 2.0 protocol inline — no helper Lambdas
- Handles: `initialize`, `ping`, `tools/list`, `tools/call`
- On `crawl_url`: writes `RUNNING` to DynamoDB, calls `ec2:RunInstances` with a UserData script that downloads `crawl.py` from `CrawlerCodeBucket` and runs it
- On `get_*` tools: reads from DynamoDB and/or S3, returns pre-signed URLs
- Reserved concurrency: 50

### VisualQATriggerFunction

- Runtime: Python 3.12, 256 MB, 10s timeout
- Validates `session_id` (regex `^[a-zA-Z0-9_-]{4,64}$`)
- Optionally validates `parent_session_id` (same regex; silently drops invalid values)
- Fires `VisualQAWorkerFunction` with `InvocationType=Event` (async)
- Returns `202` with `report_s3_key` so the caller knows where to poll

### VisualQAWorkerFunction

- Runtime: Python 3.12, 3008 MB, 900s timeout
- X-Ray tracing enabled
- Always writes a result (report or error stub) to `{prefix}/{session_id}/qa_report.json`

#### Step 0 — Parent context (optional)
If `parent_session_id` is present in the event:
1. Fetches `{prefix}/{parent_session_id}/qa_report.json` from S3
2. Validates it is parseable JSON
3. Calls Bedrock for a single-turn summarisation (≤300 words)
4. On any failure (NoSuchKey, non-JSON, Bedrock error) → logs a warning and sets `parent_summary = None`

#### Steps 1–4 — Discovery and batching
1. `GET {prefix}/{session_id}/states.json` — strips each state to `{state_id, url, trigger_label, visible_text_preview, interactive_elements}`
2. Paginates S3 to list `screenshots/state_NNNN.png` keys
3. Pairs each state to its screenshot key by reconstructing the path from `state_id`
4. Chunks pairs into batches of 10

#### Step 5 — Multi-turn Bedrock conversation
The conversation is an append-only `messages` list:

```
[optional] user:      "Prior session summary: ..."
[optional] assistant: "Understood, I'll use this as context."

user:      batch 1 images + metadata + instructions
assistant: plain-text observations for batch 1

user:      batch 2 images + metadata + instructions
assistant: plain-text observations for batch 2

...

user:      "Consolidate all findings into the final JSON report."
assistant: { ... JSON report ... }
```

Claude sees the full prior conversation on every call, giving it shared context for cross-batch consistency checking. Bedrock `ThrottlingException` is retried with 2 s / 4 s / 8 s backoff.

#### Step 6 — Consolidation
The final user turn requests a raw JSON document (no markdown fences). The Worker strips any accidental fencing before parsing. On parse failure an `"ERROR"` stub is written so the S3 key always resolves.

### EC2 Crawler

- AMI: Latest Amazon Linux 2023 (resolved from SSM at deploy time)
- Instance type: configurable (`t3.small` / `t3.medium` / `t3.large`, default `t3.medium`)
- Security group: egress-only, no inbound rules
- UserData installs Playwright + Chromium, downloads `crawl.py` from `CrawlerCodeBucket`, runs the crawl, uploads artifacts, and calls `ec2:TerminateInstances` on itself
- Reachable for debugging via AWS Systems Manager Session Manager (no SSH key needed)

### S3 — ArtifactsBucket

- Auto-named by CloudFormation (globally unique)
- AES-256 server-side encryption
- All public access blocked
- 90-day lifecycle expiry on all objects
- `DeletionPolicy: Retain` (stack deletion does not delete data)

### S3 — CrawlerCodeBucket

- Stores `crawler/crawl.py` — downloaded by EC2 workers at boot
- Versioning enabled so rollbacks don't require a stack redeploy
- `DeletionPolicy: Retain`

### DynamoDB — SessionsTable

- Table name: `<StackName>-sessions`
- Key: `session_id` (string hash key)
- On-demand billing, point-in-time recovery, SSE enabled
- TTL attribute: `ttl` (set by the crawler to auto-expire old records)

---

## IAM roles

| Role | Principal | Key permissions |
|---|---|---|
| `McpServerRole` | Lambda | `dynamodb:{GetItem,PutItem}` on SessionsTable; `s3:{ListBucket,GetObject}` on ArtifactsBucket; `ec2:{RunInstances,CreateTags}`; `iam:PassRole` to CrawlerInstanceRole |
| `CrawlerInstanceRole` | EC2 | `s3:PutObject` on ArtifactsBucket; `s3:GetObject` on CrawlerCodeBucket; `dynamodb:{PutItem,UpdateItem}` on SessionsTable; `ec2:TerminateInstances`; SSM managed instance core |
| `VisualQATriggerRole` | Lambda | `lambda:InvokeFunction` on VisualQAWorkerFunction only |
| `VisualQAWorkerRole` | Lambda | `s3:{ListBucket,GetObject,PutObject}` on ArtifactsBucket; `bedrock:InvokeModel` on `anthropic.claude-3-5-sonnet-20241022-v2:0` |

---

## Data flows

### Crawl session

```
MCP client
  → POST /mcp  tools/call  crawl_url
  → McpServerFunction
      → DynamoDB PutItem  (status=RUNNING)
      → EC2 RunInstances  (UserData injects session_id + config)
  ← {session_id}

EC2 worker (async)
  → S3 GetObject  crawler/crawl.py  (from CrawlerCodeBucket)
  → Playwright crawl
  → S3 PutObject  screenshots/, states.json, transitions.json, trace.zip
  → DynamoDB UpdateItem  (status=COMPLETED, summary_stats, artifact_manifest)
  → EC2 TerminateInstances (self)
```

### Visual QA with parent context

```
Caller
  → POST /v1/visual-qa  {session_id, parent_session_id}
  → VisualQATriggerFunction  (validates IDs, returns 202)
      → Lambda InvokeFunction (Event)  VisualQAWorkerFunction

VisualQAWorkerFunction (async)
  → S3 GetObject  parent_session/qa_report.json
  → Bedrock InvokeModel  (summarise parent report)
  → S3 GetObject  states.json
  → S3 ListObjectsV2  screenshots/
  → [for each batch of 10]
      → S3 GetObject  state_NNNN.png  (×10, parallel download)
      → Bedrock InvokeModel  (batch review, full message history)
  → Bedrock InvokeModel  (consolidation → JSON report)
  → S3 PutObject  qa_report.json

Caller
  → aws s3 cp s3://<bucket>/screenweave/<session_id>/qa_report.json .
```

---

## CloudFormation outputs

| Output key | Description |
|---|---|
| `McpEndpoint` | MCP server URL for client configuration |
| `McpClientConfig` | Ready-to-paste JSON MCP client config |
| `ArtifactsBucketName` | S3 bucket storing all session artifacts |
| `CrawlerCodeBucketName` | S3 bucket for `crawl.py` — upload after first deploy |
| `SessionsTableName` | DynamoDB session index table |
| `VisualQAEndpoint` | Visual QA REST API — `POST {"session_id": "..."}` |
| `VisualQAWorkerFunctionName` | Worker Lambda name (for log monitoring) |

---

## Deployment

The stack is deployed with AWS SAM. No parameters are required — all have defaults.

```bash
sam build --template-file infra/main-stack.yaml
sam deploy \
  --stack-name screenweave \
  --resolve-s3 \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-confirm-changeset \
  --no-fail-on-empty-changeset
```

Optional parameters (can be passed with `--parameter-overrides`):

| Parameter | Default | Options |
|---|---|---|
| `CrawlerInstanceType` | `t3.medium` | `t3.small`, `t3.medium`, `t3.large` |
| `SignedUrlExpiresSeconds` | `3600` | 300 – 43200 |
| `CrawlerAmiId` | Latest AL2023 (from SSM) | Any valid AMI ID |

---

## Monitoring

**MCP Lambda logs**
```bash
aws logs tail /aws/lambda/screenweave-mcp --follow
```

**Visual QA Worker logs**
```bash
aws logs tail /aws/lambda/screenweave-visualqa-worker --follow
```

**Active crawl sessions**
```bash
aws dynamodb scan \
  --table-name screenweave-sessions \
  --filter-expression "#s = :r" \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values '{":r":{"S":"RUNNING"}}'
```

**Poll for QA report**
```bash
aws s3 cp s3://<ArtifactsBucket>/screenweave/<session_id>/qa_report.json - | jq .overall_status
```
