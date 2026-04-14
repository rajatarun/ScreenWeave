#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# ScreenWeave – API Stack Deployment Script
#
# Packages the Lambda function, uploads it to S3, then deploys (or updates)
# the CloudFormation API stack (DynamoDB + Lambda + API Gateway).
#
# Usage:
#   ./deploy-api.sh [OPTIONS]
#
# Required options:
#   --artifacts-bucket BUCKET   S3 bucket that holds Playwright artifacts
#   --code-bucket BUCKET        S3 bucket for the Lambda deployment ZIP
#
# Optional options:
#   --env ENV             dev | staging | prod         (default: dev)
#   --region REGION       AWS region                   (default: us-east-1)
#   --prefix PREFIX       Artifact key prefix          (default: screenweave)
#   --stack-name NAME     CloudFormation stack name    (default: screenweave-api-{env})
#   --expires SECONDS     Signed URL TTL               (default: 3600)
#   --skip-build          Skip npm install + zip step (re-use existing .build/getSession.zip)
#
# Prerequisites:
#   - AWS CLI v2 configured with credentials that have CloudFormation, IAM,
#     Lambda, DynamoDB, API Gateway, and S3 permissions
#   - Node.js 20+ and npm installed locally
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GETSESSION_SRC="${SCRIPT_DIR}/src/lambda/getSession"
STARTCRAWL_SRC="${SCRIPT_DIR}/src/lambda/startCrawl"
CRAWLER_SRC="${SCRIPT_DIR}/src/crawler/crawl.py"
INFRA_TEMPLATE="${SCRIPT_DIR}/infra/api-stack.yaml"
BUILD_DIR="${SCRIPT_DIR}/.build"
ZIP_NAME="getSession.zip"
ZIP_PATH="${BUILD_DIR}/${ZIP_NAME}"
STARTCRAWL_ZIP_NAME="startCrawl.zip"
STARTCRAWL_ZIP_PATH="${BUILD_DIR}/${STARTCRAWL_ZIP_NAME}"
STARTCRAWL_CODE_KEY="screenweave/lambda/${STARTCRAWL_ZIP_NAME}"
CRAWLER_S3_KEY="screenweave/crawler/crawl.py"

# ── Defaults ─────────────────────────────────────────────────────────────────
ENV="dev"
REGION="us-east-1"
ARTIFACTS_BUCKET=""
CODE_BUCKET=""
BUCKET_PREFIX="screenweave"
STACK_NAME=""
SIGNED_URL_EXPIRES="3600"
SKIP_BUILD=false
LAMBDA_CODE_KEY="screenweave/lambda/${ZIP_NAME}"

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
    --skip-build)        SKIP_BUILD=true;        shift ;;
    --help|-h)
      sed -n '/^# Usage/,/^[^#]/p' "$0" | grep '^#' | sed 's/^# \?//'
      exit 0 ;;
    *) echo "ERROR: Unknown option: $1" >&2; exit 1 ;;
  esac
done

STACK_NAME="${STACK_NAME:-screenweave-api-${ENV}}"

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
echo "  ScreenWeave API Stack – Deploy"
echo "  Environment  : ${ENV}"
echo "  Region       : ${REGION}"
echo "  Stack        : ${STACK_NAME}"
echo "  Artifacts    : s3://${ARTIFACTS_BUCKET}/${BUCKET_PREFIX}/"
echo "  Lambda ZIP   : s3://${CODE_BUCKET}/${LAMBDA_CODE_KEY}"
echo "  Signed URL TTL: ${SIGNED_URL_EXPIRES}s"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── Step 1: Build Lambda package ─────────────────────────────────────────────
if [[ "$SKIP_BUILD" == "true" ]]; then
  echo ""
  echo "[1/4] Skipping build (--skip-build set)."
  [[ ! -f "$ZIP_PATH" ]]           && { echo "ERROR: ZIP not found at ${ZIP_PATH}"; exit 1; }
  [[ ! -f "$STARTCRAWL_ZIP_PATH" ]] && { echo "ERROR: ZIP not found at ${STARTCRAWL_ZIP_PATH}"; exit 1; }
else
  echo ""
  echo "[1/4] Building Lambda packages..."
  mkdir -p "${BUILD_DIR}"

  # getSession Lambda
  pushd "${GETSESSION_SRC}" > /dev/null
  npm ci --omit=dev --silent
  popd > /dev/null
  rm -f "${ZIP_PATH}"
  pushd "${GETSESSION_SRC}" > /dev/null
  zip -qr "${ZIP_PATH}" . --exclude "*.test.js" --exclude "*.md"
  popd > /dev/null
  echo "      getSession:  ${ZIP_PATH} ($(du -sh "${ZIP_PATH}" | cut -f1))"

  # startCrawl Lambda
  pushd "${STARTCRAWL_SRC}" > /dev/null
  npm ci --omit=dev --silent
  popd > /dev/null
  rm -f "${STARTCRAWL_ZIP_PATH}"
  pushd "${STARTCRAWL_SRC}" > /dev/null
  zip -qr "${STARTCRAWL_ZIP_PATH}" . --exclude "*.test.js" --exclude "*.md"
  popd > /dev/null
  echo "      startCrawl:  ${STARTCRAWL_ZIP_PATH} ($(du -sh "${STARTCRAWL_ZIP_PATH}" | cut -f1))"
fi

# ── Step 2: Upload ZIPs + crawler script to S3 ───────────────────────────────
echo ""
echo "[2/4] Uploading to S3..."
aws s3 cp "${ZIP_PATH}"           "s3://${CODE_BUCKET}/${LAMBDA_CODE_KEY}"    --region "${REGION}" --sse AES256
aws s3 cp "${STARTCRAWL_ZIP_PATH}" "s3://${CODE_BUCKET}/${STARTCRAWL_CODE_KEY}" --region "${REGION}" --sse AES256
# crawl.py is downloaded by EC2 crawler workers at runtime
aws s3 cp "${CRAWLER_SRC}"        "s3://${CODE_BUCKET}/${CRAWLER_S3_KEY}"     --region "${REGION}" --sse AES256
echo "      Uploaded: getSession, startCrawl ZIPs + crawl.py"

# ── Step 3: Deploy / update CloudFormation stack ─────────────────────────────
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
    "StartCrawlCodeKey=${STARTCRAWL_CODE_KEY}" \
    "SignedUrlExpiresSeconds=${SIGNED_URL_EXPIRES}" \
    "Environment=${ENV}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset

# ── Step 4: Force Lambda code update ─────────────────────────────────────────
# CloudFormation won't detect a code change if only the S3 object changed
# without modifying the template. Explicit update-function-code ensures the
# latest ZIP is always deployed.
echo ""
echo "[4/4] Forcing Lambda code refresh..."
for FN_NAME in "screenweave-get-session-${ENV}" "screenweave-start-crawl-${ENV}"; do
  FN_KEY="${LAMBDA_CODE_KEY}"
  [[ "$FN_NAME" == *"start-crawl"* ]] && FN_KEY="${STARTCRAWL_CODE_KEY}"
  aws lambda update-function-code \
    --region "${REGION}" \
    --function-name "${FN_NAME}" \
    --s3-bucket "${CODE_BUCKET}" \
    --s3-key "${FN_KEY}" \
    --query "FunctionArn" --output text
  aws lambda wait function-updated --region "${REGION}" --function-name "${FN_NAME}"
  echo "      Refreshed: ${FN_NAME}"
done

# ── Print deployment summary ─────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Deployment complete!"
echo ""

# Fetch stack outputs
query_output() {
  aws cloudformation describe-stacks \
    --region "${REGION}" \
    --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='${1}'].OutputValue" \
    --output text
}

API_ENDPOINT="$(query_output ApiEndpoint)"
API_KEY_ID="$(query_output DefaultApiKeyId)"
TABLE_NAME="$(query_output SessionsTableName)"

# Attempt to retrieve the raw API key value (requires apigateway:GetApiKey)
API_KEY_VALUE="$(aws apigateway get-api-key \
  --region "${REGION}" \
  --api-key "${API_KEY_ID}" \
  --include-value \
  --query "value" \
  --output text 2>/dev/null || echo "(retrieve from Console: API Gateway > API Keys)")"

echo "  API Endpoint   : ${API_ENDPOINT}"
echo "  DynamoDB Table : ${TABLE_NAME}"
echo "  API Key ID     : ${API_KEY_ID}"
echo "  API Key Value  : ${API_KEY_VALUE}"
echo ""
echo "  Trigger a crawl:"
echo "    curl -s -X POST \\"
echo "      -H 'x-api-key: ${API_KEY_VALUE}' \\"
echo "      -H 'content-type: application/json' \\"
echo "      -d '{\"url\":\"https://example.com\",\"max_depth\":2}' \\"
echo "      '${API_ENDPOINT}/session' | jq ."
echo ""
echo "  Poll until COMPLETED, then get results:"
echo "    curl -s -H 'x-api-key: ${API_KEY_VALUE}' \\"
echo "      '${API_ENDPOINT}/session/{session_id}' | jq ."
echo ""
echo "  MCP server config (claude_desktop_config.json / .cursor/mcp.json):"
echo "    {"
echo "      \"mcpServers\": {"
echo "        \"screenweave\": {"
echo "          \"command\": \"node\","
echo "          \"args\": [\"$(pwd)/src/mcp-server/index.js\"],"
echo "          \"env\": {"
echo "            \"SCREENWEAVE_API_URL\": \"${API_ENDPOINT}\","
echo "            \"SCREENWEAVE_API_KEY\": \"${API_KEY_VALUE}\""
echo "          }"
echo "        }"
echo "      }"
echo "    }"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
