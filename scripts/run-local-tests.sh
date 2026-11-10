#!/usr/bin/env bash
# Offline test harness — no AWS credentials required.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "== MCP server (node --test) =="
cd "$ROOT/src/lambda/mcpServer"
npm ci --silent
npm test

echo "== Python tests =="
python3 -m pip install -q pytest boto3 pyyaml -r "$ROOT/src/lambda/visualQAWorker/requirements.txt"
OUT="${SCREENWEAVE_OUT_DIR:-/tmp/screenweave-crawl-test}"
mkdir -p "$OUT"
# crawl.py still defaults to /opt/output unless patched; ensure it exists for import-time makedirs:
if [[ ! -w /opt/output ]] 2>/dev/null; then
  if mkdir -p /opt/output 2>/dev/null; then :; else
  sudo mkdir -p /opt/output 2>/dev/null && sudo chmod 1777 /opt/output 2>/dev/null || true
  fi
fi
python3 -m pytest -q "$ROOT/tests"

echo "== Observatory contract sample (synthetic) =="
node "$ROOT/scripts/check-observatory-contract.mjs"

echo "All local checks passed."
