# Swarm to video — cross-repo design

> **Status: proposal.** Nothing in this document is built. It records the three
> interfaces the `resume → deployweave → teamweave → screenweave` flow needs,
> and the places where the flow as originally sketched does not match what the
> repositories actually are. Open questions are marked; they are decisions, not
> oversights.

## The flow

```
resume            user asks for a "Product Marketing & Quality Team" for a URL
  │
  ▼  POST /teams/provision
deployweave       token wallet + team_provisioner  →  agent registry
  │
  ▼
teamweave         supervisor walks the team.json pipeline
  ├─ QA Auditor      ──► screenweave  crawl_url → /visual-qa
  └─ Motion Marketer ──► screenweave  generate_apple_video   (NEW)
  │
  ▼
resume            QA report + MP4, both as S3 URLs
```

## What the original sketch got wrong

Four corrections, because they change the work rather than the wording:

**ScreenWeave is already an MCP server.** `POST /mcp` speaks JSON-RPC 2.0 and
already exposes `crawl_url`, `get_session_status`, `get_screenshots`,
`get_metrics`, `get_full_session` and `export_session`. There is no tool
interface to design — there is a seventh tool to add to `TOOL_DEFINITIONS` in
`src/lambda/mcpServer/index.mjs`. TeamWeave's side is a client in
`tool_registry.py`, not a new protocol.

**There is no `src/tools/*.ts`.** The MCP server is `src/lambda/mcpServer/*.mjs`;
the QA workers and crawler are Python (`src/lambda/visualQAWorker/`,
`src/crawler/crawl.py`). A "dual-engine refactor" of files that do not exist
would be a rewrite of a working service.

**TeamWeave and DeployWeave are Python.** A typed TypeScript supervisor would be
a parallel implementation that cannot reach the config loader, the schema
validator, the Observatory gate or the DPO collector. TypeScript belongs to the
MCP server and to Remotion, and nowhere else in this flow.

**DeployWeave already provisions teams.** `team_provisioner` is one of four MCP
tools and it writes to a DynamoDB agent registry. It also carries a per-tenant
token wallet (reserve → invoke → commit, with threshold alerts and orphan
reconciliation) that nothing else in the platform replicates. `POST /teams/provision`
should be a REST facade over that tool, not a second provisioning path.

## Contract 1 — `POST /teams/provision` (deployweave)

A REST facade over the existing `team_provisioner` MCP tool. Asynchronous, like
every other provisioning write on the platform: 202 and a `run_id`.

```jsonc
// request
{
  "tenant_id": "string",            // required — scopes the wallet and artifacts
  "team_name": "string",            // required — lowercase, [a-z0-9_]
  "target_url": "https://…",        // required — what the squad operates on
  "agents": [                       // required, >= 1
    { "role_id": "PBM-001", "name": "qa-auditor" }
  ],
  "budget": { "max_tokens": 500000 } // optional — defaults to the tenant plan
}

// 202
{ "run_id": "uuid", "team_id": "string", "status": "PROVISIONING" }
```

`schema_ref` is deliberately absent: every role already declares one, and
TeamWeave's `POST /agents` now derives it from the role rather than asking a
caller to repeat a decision the role has made.

> **Open question.** Whether provisioning creates Bedrock Agents Classic or
> AgentCore runtimes is the migration decision, not this contract's. The
> response deliberately returns an opaque `team_id` so callers never learn
> which substrate backs it.

## Contract 2 — `generate_apple_video` (screenweave MCP tool)

A seventh entry in `TOOL_DEFINITIONS`, dispatched by the existing switch. It
follows the shape the other long-running tool already established: return a
session id immediately, poll for completion.

```jsonc
{
  "name": "generate_apple_video",
  "inputSchema": {
    "type": "object",
    "properties": {
      "url":        { "type": "string", "description": "Page to showcase (http/https)" },
      "session_id": { "type": "string", "description": "Reuse an existing crawl instead of re-crawling" },
      "duration_s": { "type": "integer", "minimum": 5, "maximum": 30, "default": 20 },
      "scale":      { "type": "integer", "enum": [1, 2, 3], "default": 3 }
    },
    "required": []                  // one of url or session_id; enforced in the handler
  }
}
```

Reusing a completed crawl matters: the QA auditor has usually already crawled
the site, and capturing it a second time doubles the cost and risks the two
agents describing different versions of the page.

> **Open question.** Hero-feature detection is described as a Bedrock call over
> captured frames. The Visual QA worker already runs Claude over screenshots —
> whether this is a new prompt in that worker or a separate one is a cost and
> latency decision worth measuring before choosing.

## Contract 3 — job completion event

One envelope for both engines, so TeamWeave has a single thing to parse and the
Observatory has a single thing to record.

```jsonc
{
  "event": "screenweave.job.completed",
  "job_id": "uuid",
  "session_id": "uuid",
  "mode": "qa_audit" | "generate_apple_video",
  "status": "COMPLETED" | "FAILED",
  "artifacts": [
    { "kind": "qa_report", "s3_uri": "s3://…/qa_report.json", "content_type": "application/json" },
    { "kind": "showcase_video", "s3_uri": "s3://…/showcase.mp4", "content_type": "video/mp4",
      "duration_s": 20, "width": 3840, "height": 2160 }
  ],
  "error": null,
  "started_at": "2026-09-20T03:00:00Z",
  "finished_at": "2026-09-20T03:04:12Z"
}
```

`artifacts` is a list rather than named fields so a future engine adds a `kind`
instead of a schema version. `status: "FAILED"` still carries any artifacts
produced before the failure — a partial QA report is more useful than nothing,
and the platform already has the habit of returning a failure in the body
rather than as a transport error.

## Tenancy

The sketch proposed a bucket per team, `screenweave-artifacts-${teamId}`. Prefer
**one bucket, per-tenant prefixes, IAM conditions on `s3:prefix`**: an account
is capped at 1,000 buckets, lifecycle and replication rules multiply per bucket,
and per-team buckets make cross-team QA comparison awkward. Hard bucket
separation is worth it only against a compliance requirement that actually
demands it.

## Build order

1. `generate_apple_video` returning a stub artifact — proves the MCP tool,
   the completion event and TeamWeave's client end to end before any rendering
   exists.
2. Playwright capture at `scale: 3`, reusing `crawl.py`'s browser setup.
3. Hero-feature detection.
4. Remotion Lambda render.
5. `POST /teams/provision` facade.

Step 1 first because it makes steps 2–4 independently testable: every later
piece replaces a stub behind a contract that already works.
