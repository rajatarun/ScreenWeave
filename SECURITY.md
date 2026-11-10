# Security Policy

## Supported versions

Security fixes are applied on the default branch (`main`). Deployed stacks should track `main` or a recent release tag once tags exist.

| Component | Notes |
|-----------|--------|
| MCP HTTP API (`POST /mcp`) | Primary external surface |
| Visual QA REST API (`POST /visual-qa`) | IAM-authenticated; long-running Bedrock workload |
| EC2 crawler (`crawl.py`) | Ephemeral; egress-only security group |

There are no long-term support branches today. If you maintain a fork or pinned release, merge security fixes from `main`.

## Reporting a vulnerability

**Do not open a public GitHub issue for security problems.**

Report vulnerabilities privately to the repository maintainers through GitHub **Private vulnerability reporting** (if enabled for this repo) or by contacting the owner via the email or channel listed on their GitHub profile.

Include:

- Description of the issue and likely impact
- Steps to reproduce (proof-of-concept if available)
- Affected paths (e.g. `src/lambda/mcpServer/`, `src/crawler/crawl.py`)
- Your environment (AWS region, `AuthMode`, approximate deploy date)

We aim to acknowledge reports within a reasonable time and will coordinate disclosure after a fix is available.

## Scope and expectations

ScreenWeave is designed to run in **your** AWS account. You are responsible for:

- IAM permissions and network boundaries for the stack
- Restricting who can call `/mcp` (use `AuthMode=enforce` and valid access tokens when ready)
- Crawl targets: only point the crawler at sites you are allowed to test
- Bedrock usage and cost controls for Visual QA

The MCP endpoint can start EC2 crawlers when `crawl_url` is invoked. Treat the endpoint URL like infrastructure credentials in `audit` mode (anonymous calls are allowed by default).

See [docs/security-notes.md](docs/security-notes.md) for a concise threat model.
