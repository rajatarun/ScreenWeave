# Design notes

Detailed references live alongside this folder:

| Topic | Document |
|-------|----------|
| System architecture | [../architecture.md](../architecture.md) |
| Swarm → video workflow | [../swarm-to-video.md](../swarm-to-video.md) |
| Visual QA cost / tiering | [../cost-calculation.md](../cost-calculation.md) |
| Security overview | [../security-notes.md](../security-notes.md) |
| MCP observatory contract | [../../contracts/README.md](../../contracts/README.md) |

## Core design principles (Visual QA)

From `image_classifier.py` and worker orchestration:

1. **Stratified routing** — Local complexity scoring routes screens to heuristic-only, Haiku, or Sonnet tiers.
2. **Progressive enhancement** — Cheapest tier that suffices is used first.
3. **Perceptual-hash cache** — Repeated UI states reuse cached interpretations (72h TTL table).
4. **Multi-turn Bedrock context** — Batches share conversation history for cross-screen consistency checks.

## Crawl design principles

1. **State graph** — Each interactive change produces a new `state_id` and transition edge.
2. **Defense in depth** — Block internal/metadata URLs in Lambda validation and again in Playwright routing.
3. **Ephemeral workers** — Crawler EC2 self-terminates; no SSH required (SSM optional).

## MCP design principles

1. **Inline tool logic** — No Lambda-to-Lambda hop for crawl control plane.
2. **Optional telemetry** — Observatory wrapper is transparent when unset.
3. **Protocol negotiation** — Honor client-requested MCP revision when supported.

For integration sketches, see [../INTEGRATIONS.md](../INTEGRATIONS.md).
