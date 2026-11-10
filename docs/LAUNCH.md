# Launch draft (not published)

Use this as a starting point for a blog post, README announcement, or community post. Adjust tone and links before publishing.

---

## Draft post

**Title idea:** ScreenWeave — crawl the web visually, then QA it with MCP and Bedrock

**Problem**

Agents and test pipelines often see HTML, not what users see. Screenshots catch layout breaks, lazy-loaded content, and multi-step UI flows that DOM-only crawlers miss.

**What ScreenWeave does**

- Exposes an **MCP over HTTP** server: start a Playwright crawl, poll status, fetch screenshots and structured `states.json` / `transitions.json`.
- Runs crawls on **ephemeral EC2** workers; artifacts land in **S3** with a session index in **DynamoDB**.
- Optional **Visual QA** uses **Amazon Bedrock** (Haiku/Sonnet routing + local heuristics) to produce a JSON QA report, including cross-page observations.

**Who it's for**

- Teams building **MCP-connected agents** that need grounded visual context
- Platform engineers wiring **AWS-native** crawl + QA into existing S3/Dynamo workflows
- Integrators feeding crawl output into their own indexing pipelines via `export_session`

**Quick try**

Deploy with SAM (see README), paste the MCP URL into your client, call `crawl_url`, then `get_full_session` when status is `COMPLETED`.

**Honest limits**

- **Requires AWS** (not local-first today)
- Crawls cost **EC2 + S3**; Visual QA costs **Bedrock** and long Lambda runs
- Default MCP **`audit` mode** allows anonymous calls — use **`enforce`** and tokens before exposing broadly
- Video generation and session export need **extra configuration**
- DPoP-bound tokens need client-side proof support; server-side replay ledger is **not** implemented

**License:** Apache-2.0

---

## Communities where this may fit (draft list)

- MCP ecosystem directories and Discord/forums focused on Model Context Protocol
- AWS builder communities (re:Invent builder sessions, Bedrock workshops)
- Playwright user groups and visual testing discussions
- Open-source agent orchestration projects (LangGraph, custom agent frameworks) seeking browser grounding tools
- DevOps / platform engineering circles doing post-deploy smoke checks

Do not spam; tailor each post to community rules.
