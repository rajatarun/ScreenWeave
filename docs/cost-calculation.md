# ScreenWeave cost calculation (Claude Sonnet 4 + Lambda)

This document shows how to estimate Visual QA cost for ScreenWeave using:

- **Claude Sonnet 4** token pricing.
- **AWS Lambda** request + compute pricing for the Visual QA worker.

> Example below uses a **requested what-if scenario** of `total_states = 73`.

---

## 1) Inputs you need

From this repo:

- Example crawl size: `total_states = 73` (requested example scenario).
- Visual QA worker config: `MemorySize = 3008 MB` (2.9375 GB).
- Worker model ID: `us.anthropic.claude-sonnet-4-20250514-v1:0`.

From pricing pages (check again before production forecasting):

- Claude Sonnet 4 API list price: **$3 / 1M input tokens** and **$15 / 1M output tokens**.
- Lambda on-demand price baseline (x86 in US regions example):
  - **$0.0000166667 per GB-second**
  - **$0.20 per 1M requests**

---

## 2) Formula

Total job cost:

```text
Total = Claude input cost + Claude output cost + Lambda compute cost + Lambda request cost
```

Where:

```text
Claude input cost  = (input_tokens  / 1,000,000) * input_price_per_million
Claude output cost = (output_tokens / 1,000,000) * output_price_per_million

Lambda compute cost = duration_seconds * memory_gb * gb_second_price
Lambda request cost = (invocations / 1,000,000) * request_price_per_million
```

---

## 3) Worked example using 73 states

### Example assumptions for one Visual QA run

Using a 73-state run (requested), apply these starter assumptions:

- States analyzed: **73**.
- Batches (worker default size 10): **8** batches (`ceil(73/10)`).
- Claude usage for the run (scaled for 73 states / multi-turn conversation):
  - Input tokens: **276,000**
  - Output tokens: **24,000**
- Worker runtime (end-to-end): **85 seconds**
- Worker memory: **3008 MB = 2.9375 GB**
- Invocations: **1**

### Step-by-step

1. Claude input cost

```text
(276,000 / 1,000,000) * $3 = $0.8280
```

2. Claude output cost

```text
(24,000 / 1,000,000) * $15 = $0.3600
```

3. Lambda compute cost

```text
85s * 2.9375 GB * $0.0000166667 = $0.0041615
```

4. Lambda request cost

```text
(1 / 1,000,000) * $0.20 = $0.0000002
```

### Estimated total

```text
$0.8280 + $0.3600 + $0.0041615 + $0.0000002
= $1.1921617
≈ $1.1922 per Visual QA run
```

---

## 4) Practical notes

- In this architecture, **model token cost dominates**; Lambda cost is usually tiny by comparison.
- Your real cost is sensitive to:
  - Prompt/system size,
  - Number and size of screenshots,
  - Number of model turns (more batches => more tokens).
- To make this precise in production, record actual token usage from Bedrock responses and actual Lambda duration from CloudWatch.

---

## 5) Quick monthly projection template

If you run `N` jobs/month:

```text
Monthly cost ≈ N * cost_per_job
```

For example, at 5,000 similar jobs/month:

```text
5,000 * $1.1922 ≈ $5,961/month
```

(Free tier, discounts, and regional differences can reduce/increase this.)

---

## Sources

- Anthropic Claude pricing: https://platform.claude.com/docs/en/about-claude/pricing
- AWS Lambda pricing: https://aws.amazon.com/lambda/pricing/
- AWS Bedrock pricing reference: https://aws.amazon.com/bedrock/pricing/
