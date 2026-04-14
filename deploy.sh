#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# ScreenWeave – Unified Deploy Script
#
# Builds and deploys the complete ScreenWeave stack from a single command:
#   • Packages the McpServer Lambda (src/lambda/mcpServer/)
#   • Uploads mcpServer.zip + crawl.py to S3
#   • Deploys infra/main-stack.yaml via CloudFormation (creates or updates)
#   • Forces a Lambda code refresh so the latest ZIP is always live
#   • Prints the MCP endpoint URL ready to paste into MCP client config
#
# Usage:
#   ./deploy.sh [OPTIONS]
#
# Required options:
#   --artifacts-bucket BUCKET   Existing S3 bucket for Playwright artifacts
#   --code-bucket BUCKET        S3 bucket for Lambda ZIP + crawl.py
#
# Optional options:
#   --env ENV             dev | staging | prod       (default: dev)
#   --region REGION       AWS region                 (default: us-east-1)
#   --prefix PREFIX       S3 artifact key prefix     (default: screenweave)
#   --stack-name NAME     CloudFormation stack name  (default: screenweave-{env})
#   --expires SECONDS     Pre-signed URL TTL         (default: 3600)
#   --instance-type TYPE  EC2 crawler instance type  (default: t3.medium)
#   --skip-build          Skip npm install + zip step (re-use .build/mcpServer.zip)
#
# Prerequisites:
#   AWS CLI v2 with credentials that have CloudFormation, IAM, Lambda,
#   DynamoDB, API Gateway (v2), EC2, and S3 permissions.
#   Node.js 20+ and npm installed locally.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_SRC="${SCRIPT_DIR}/src/lambda/mcpServer"
CRAWLER_SRC="${SCRIPT_DIR}/src/crawler/crawl.py"
INFRA_TEMPLATE="${SCRIPT_DIR}/infra/main-stack.yaml"
BUILD_DIR="${SCRIPT_DIR}/.build"
ZIP_NAME="mcpServer.zip"
ZIP_PATH="${BUILD_DIR}/${ZIP_NAME}"

# ── Defaults ──────────────────────────────────────────────────────────────────
ENV="dev"
REGION="us-east-1"
ARTIFACTS_BUCKET=""
CODE_BUCKET=""
BUCKET_PREFIX="screenweave"
STACK_NAME=""
SIGNED_URL_EXPIRES="3600"
INSTANCE_TYPE="t3.medium"
SKIP_BUILD=false

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)               ENV="$2";               shift 2 ;;
    --region)            REGION="$2";            shift 2 ;;
    --artifacts-bucket)  ARTIFACTS_BUCKET="$2";  shift 2 ;;
    --code-bucket)       CODE_BUCKET="$2";       shift 2 ;;
    --prefix)            BUCKET_PREFIX="$2";     shift 2 ;;
    --stack-name)        STACK_NAME="$2";        shift 2 ;;
    --expires)           SIGNED_URL_EXPIRES="$2"; shift 2 ;;
    --instance-type)     INSTANCE_TYPE="$2";     shift 2 ;;
    --skip-build)        SKIP_BUILD=true;        shift ;;
    --help|-h)
      sed -n '/^# Usage/,/^[^#]/p' "$0" | grep '^#' | sed 's/^# \?//'; exit 0 ;;
    *) echo "ERROR: Unknown option: $1" >&2; exit 1 ;;
  esac
done

STACK_NAME="${STACK_NAME:-screenweave-${ENV}}"
LAMBDA_CODE_KEY="${BUCKET_PREFIX}/lambda/${ZIP_NAME}"
CRAWLER_S3_KEY="${BUCKET_PREFIX}/crawler/crawl.py"

# ── Validation ────────────────────────────────────────────────────────────────
ERRORS=()
[[ -z "$ARTIFACTS_BUCKET" ]] && ERRORS+=("--artifacts-bucket is required")
[[ -z "$CODE_BUCKET" ]]      && ERRORS+=("--code-bucket is required")
if [[ ${#ERRORS[@]} -gt 0 ]]; then
  for msg in "${ERRORS[@]}"; do echo "ERROR: $msg" >&2; done
  exit 1
fi

# ── Banner ────────────────────────────────────────────────────────────────────
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ScreenWeave – Unified Deploy"
echo "  Environment    : ${ENV}"
echo "  Region         : ${REGION}"
echo "  Stack          : ${STACK_NAME}"
echo "  Artifacts      : s3://${ARTIFACTS_BUCKET}/${BUCKET_PREFIX}/"
echo "  Lambda code    : s3://${CODE_BUCKET}/${LAMBDA_CODE_KEY}"
echo "  Crawler script : s3://${CODE_BUCKET}/${CRAWLER_S3_KEY}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── Step 1: Build Lambda package ──────────────────────────────────────────────
if [[ "$SKIP_BUILD" == "true" ]]; then
  echo ""
  echo "[1/4] Skipping build (--skip-build set)."
  [[ ! -f "$ZIP_PATH" ]] && { echo "ERROR: ZIP not found at ${ZIP_PATH}"; exit 1; }
else
  echo ""
  echo "[1/4] Building McpServer Lambda..."
  mkdir -p "${BUILD_DIR}"

  pushd "${MCP_SRC}" > /dev/null
  npm ci --omit=dev --silent
  popd > /dev/null

  rm -f "${ZIP_PATH}"
  pushd "${MCP_SRC}" > /dev/null
  zip -qr "${ZIP_PATH}" . --exclude "*.test.mjs" --exclude "*.test.js" --exclude "*.md"
  popd > /dev/null

  echo "      ${ZIP_PATH} ($(du -sh "${ZIP_PATH}" | cut -f1))"
fi

# ── Step 2: Upload to S3 ──────────────────────────────────────────────────────
echo ""
echo "[2/4] Uploading to S3..."
aws s3 cp "${ZIP_PATH}"     "s3://${CODE_BUCKET}/${LAMBDA_CODE_KEY}" \
  --region "${REGION}" --sse AES256
aws s3 cp "${CRAWLER_SRC}"  "s3://${CODE_BUCKET}/${CRAWLER_S3_KEY}"  \
  --region "${REGION}" --sse AES256
echo "      Uploaded: mcpServer.zip + crawl.py"

# ── Step 3: Deploy / update CloudFormation stack ──────────────────────────────
echo ""
echo "[3/4] Deploying CloudFormation stack: ${STACK_NAME}..."
aws cloudformation deploy \
  --region "${REGION}" \
  --template-file "${INFRA_TEMPLATE}" \
  --stack-name "${STACK_NAME}" \
  --parameter-overrides \
    "ArtifactsBucket=${ARTIFACTS_BUCKET}" \
    "BucketPrefix=${BUCKET_PREFIX}" \
    "LambdaCodeBucket=${CODE_BUCKET}" \
    "LambdaCodeKey=${LAMBDA_CODE_KEY}" \
    "SignedUrlExpiresSeconds=${SIGNED_URL_EXPIRES}" \
    "CrawlerInstanceType=${INSTANCE_TYPE}" \
    "Environment=${ENV}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset

# ── Step 4: Force Lambda code refresh ────────────────────────────────────────
# CloudFormation won't re-deploy Lambda if only the S3 object changed
# (same key, new content). Explicit update-function-code guarantees the
# latest ZIP is always live after every deploy.
echo ""
echo "[4/4] Forcing Lambda code refresh..."
FN_NAME="screenweave-mcp-${ENV}"
aws lambda update-function-code \
  --region "${REGION}" \
  --function-name "${FN_NAME}" \
  --s3-bucket "${CODE_BUCKET}" \
  --s3-key "${LAMBDA_CODE_KEY}" \
  --query "FunctionArn" --output text
aws lambda wait function-updated \
  --region "${REGION}" \
  --function-name "${FN_NAME}"
echo "      Refreshed: ${FN_NAME}"

# ── Print deployment summary ──────────────────────────────────────────────────
query_output() {
  aws cloudformation describe-stacks \
    --region "${REGION}" \
    --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='${1}'].OutputValue" \
    --output text
}

MCP_ENDPOINT="$(query_output McpEndpoint)"
TABLE_NAME="$(query_output SessionsTableName)"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Deployment complete!"
echo ""
echo "  MCP Endpoint   : ${MCP_ENDPOINT}"
echo "  DynamoDB Table : ${TABLE_NAME}"
echo ""
echo "  MCP client config (claude_desktop_config.json / .cursor/mcp.json):"
echo "    {"
echo "      \"mcpServers\": {"
echo "        \"screenweave\": {"
echo "          \"url\": \"${MCP_ENDPOINT}\""
echo "        }"
echo "      }"
echo "    }"
echo ""
echo "  Smoke-test the endpoint:"
echo "    curl -s -X POST \\"
echo "      -H 'content-type: application/json' \\"
echo "      -H 'accept: text/event-stream' \\"
echo "      -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{}}' \\"
echo "      '${MCP_ENDPOINT}'"
echo ""
echo "  Start a crawl:"
echo "    curl -s -X POST \\"
echo "      -H 'content-type: application/json' \\"
echo "      -d '{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"crawl_url\",\"arguments\":{\"url\":\"https://example.com\",\"max_depth\":2}}}' \\"
echo "      '${MCP_ENDPOINT}'"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
