# Contributing to ScreenWeave

Thanks for your interest. This project is AWS-centric (SAM/CloudFormation, Lambda, EC2, Bedrock). Most changes touch `infra/main-stack.yaml`, the MCP server (`src/lambda/mcpServer/`), the Playwright crawler (`src/crawler/crawl.py`), or Visual QA Lambdas (`src/lambda/visualQA*`).

## Development setup

### Prerequisites

- Node.js 20+ (24 matches CI)
- Python 3.12
- Optional: AWS SAM CLI and an AWS account for full integration tests

### Clone and install

```bash
git clone https://github.com/rajatarun/ScreenWeave.git
cd ScreenWeave
cd src/lambda/mcpServer && npm ci
```

### Run tests locally

**MCP server (Node):**

```bash
cd src/lambda/mcpServer
npm test
```

**Python (crawler guard, Visual QA modules, handler import smoke):**

```bash
python3 -m pip install pytest boto3 pyyaml -r src/lambda/visualQAWorker/requirements.txt
# crawl.py creates /opt/output at import time — ensure it exists and is writable:
sudo mkdir -p /opt/output && sudo chmod 1777 /opt/output
python3 -m pytest -q tests
```

Or use the helper script:

```bash
./scripts/run-local-tests.sh
```

CI runs the same suites in `.github/workflows/deploy.yaml` (test, audit, secrets jobs).

### Deploy (optional)

See the README quickstart. Deploy requires AWS credentials with permission to create the stack resources.

## Code style

- **JavaScript:** ES modules (`.mjs`), AWS SDK v3, Node built-in test runner for unit tests
- **Python:** Standard library + boto3/Pillow where already used; pytest for tests
- **Infrastructure:** One SAM template at `infra/main-stack.yaml`; prefer parameter defaults over hard-coded account specifics

Match existing logging patterns (`log()` in MCP, structured JSON on the crawler).

## Pull requests

1. Branch from `main` (forks: `yourname/short-description`).
2. Keep changes focused; update docs when behavior or configuration changes.
3. Ensure `npm test` and `pytest` pass.
4. Do not commit secrets, account IDs, or personal emails in docs or code.
5. Open a PR with a clear description, link related issues if any, and note test evidence.

We use [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Be constructive in review.

## Extension points

See [docs/EXTENDING.md](docs/EXTENDING.md) for MCP tools, observatory spans, crawler behavior, and Visual QA tiering.

## Good first tasks

See [docs/GOOD_FIRST_ISSUES.md](docs/GOOD_FIRST_ISSUES.md) for scoped ideas the maintainers can turn into issues.
