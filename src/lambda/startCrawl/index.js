'use strict';

/**
 * ScreenWeave – startCrawl Lambda
 *
 * POST /session
 * Body: { url: string, max_depth?: number, max_links?: number }
 *
 * 1. Validates the URL
 * 2. Generates a session_id (UUID v4)
 * 3. Writes session record to DynamoDB with status RUNNING
 * 4. Launches an EC2 instance whose UserData runs the Playwright crawler
 * 5. Returns { session_id, status: "RUNNING" }
 *
 * The crawler self-terminates after uploading artifacts and updating DynamoDB
 * to COMPLETED (or FAILED).
 *
 * Required environment variables:
 *   SESSIONS_TABLE        DynamoDB table name
 *   ARTIFACTS_BUCKET      S3 bucket for artifacts
 *   BUCKET_PREFIX         S3 key prefix (default: screenweave)
 *   CRAWLER_AMI_ID        AMI for the crawler EC2 instance (AL2023)
 *   CRAWLER_SG_ID         Security group ID (egress-only)
 *   CRAWLER_INSTANCE_PROFILE_ARN  IAM instance profile ARN
 *   CRAWLER_INSTANCE_TYPE EC2 instance type (default: t3.medium)
 *   AWS_REGION / AWS_DEFAULT_REGION
 */

const { DynamoDBClient, PutItemCommand } = require('@aws-sdk/client-dynamodb');
const { EC2Client, RunInstancesCommand } = require('@aws-sdk/client-ec2');
const { marshall } = require('@aws-sdk/util-dynamodb');
const { randomUUID } = require('crypto');

const dynamo = new DynamoDBClient({});
const ec2    = new EC2Client({});

const TABLE_NAME      = process.env.SESSIONS_TABLE;
const BUCKET_NAME     = process.env.ARTIFACTS_BUCKET;
const BUCKET_PREFIX   = process.env.BUCKET_PREFIX || 'screenweave';
const AMI_ID          = process.env.CRAWLER_AMI_ID;
const SG_ID           = process.env.CRAWLER_SG_ID;
const INSTANCE_PROFILE_ARN = process.env.CRAWLER_INSTANCE_PROFILE_ARN;
const INSTANCE_TYPE   = process.env.CRAWLER_INSTANCE_TYPE || 't3.medium';
const REGION          = process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || 'us-east-1';

// Simple URL validation: must be http(s) with a hostname
const URL_REGEX = /^https?:\/\/[a-zA-Z0-9._-]+(?::\d+)?(?:\/[^\s]*)?$/;
const MAX_DEPTH_LIMIT  = 3;
const MAX_LINKS_LIMIT  = 30;

// ── Handler ───────────────────────────────────────────────────────────────────
exports.handler = async (event) => {
  try {
    // ── Parse body ───────────────────────────────────────────────────────────
    let body;
    try {
      body = typeof event.body === 'string' ? JSON.parse(event.body) : (event.body || {});
    } catch {
      return response(400, { error: 'Request body must be valid JSON' });
    }

    const { url, max_depth = 2, max_links = 12 } = body;

    // ── Input validation ─────────────────────────────────────────────────────
    if (!url || typeof url !== 'string') {
      return response(400, { error: 'url is required (string)' });
    }
    if (!URL_REGEX.test(url.trim())) {
      return response(400, { error: 'url must be a valid http(s) URL' });
    }
    if (typeof max_depth !== 'number' || max_depth < 0 || max_depth > MAX_DEPTH_LIMIT) {
      return response(400, { error: `max_depth must be 0–${MAX_DEPTH_LIMIT}` });
    }
    if (typeof max_links !== 'number' || max_links < 1 || max_links > MAX_LINKS_LIMIT) {
      return response(400, { error: `max_links must be 1–${MAX_LINKS_LIMIT}` });
    }

    const targetUrl  = url.trim();
    const sessionId  = randomUUID();
    const now        = new Date().toISOString();
    const ttl        = Math.floor(Date.now() / 1000) + 7 * 24 * 60 * 60; // 7-day TTL

    // ── Write session to DynamoDB (RUNNING) ──────────────────────────────────
    await dynamo.send(new PutItemCommand({
      TableName: TABLE_NAME,
      Item: marshall({
        session_id:  `SESSION#${sessionId}`,
        status:      'RUNNING',
        target_url:  targetUrl,
        max_depth,
        max_links,
        created_at:  now,
        updated_at:  now,
        ttl,
      }),
      // Prevent overwriting an existing session (safety check)
      ConditionExpression: 'attribute_not_exists(session_id)',
    }));

    // ── Launch EC2 crawler instance ──────────────────────────────────────────
    const userDataScript = buildUserData({
      sessionId,
      targetUrl,
      s3Bucket: BUCKET_NAME,
      s3Prefix: BUCKET_PREFIX,
      region:   REGION,
      table:    TABLE_NAME,
      maxDepth: max_depth,
      maxLinks: max_links,
    });

    const userDataBase64 = Buffer.from(userDataScript).toString('base64');

    const ec2Result = await ec2.send(new RunInstancesCommand({
      ImageId:      AMI_ID,
      InstanceType: INSTANCE_TYPE,
      MinCount: 1,
      MaxCount: 1,
      IamInstanceProfile: { Arn: INSTANCE_PROFILE_ARN },
      SecurityGroupIds: [SG_ID],
      UserData: userDataBase64,
      TagSpecifications: [
        {
          ResourceType: 'instance',
          Tags: [
            { Key: 'Name',       Value: `screenweave-crawler-${sessionId.slice(0, 8)}` },
            { Key: 'SessionId',  Value: sessionId },
            { Key: 'Project',    Value: 'ScreenWeave' },
            { Key: 'ManagedBy',  Value: 'startCrawlLambda' },
          ],
        },
      ],
      // Ensure instance self-terminates even if UserData fails
      InstanceInitiatedShutdownBehavior: 'terminate',
    }));

    const instanceId = ec2Result.Instances[0].InstanceId;

    console.log(JSON.stringify({
      level: 'INFO',
      message: 'Crawl started',
      session_id: sessionId,
      instance_id: instanceId,
      target_url: targetUrl,
    }));

    return response(202, {
      session_id:  sessionId,
      status:      'RUNNING',
      target_url:  targetUrl,
      instance_id: instanceId,
      message:     'Crawl started. Poll GET /session/{session_id} to check status.',
    });

  } catch (err) {
    console.error(JSON.stringify({ level: 'ERROR', message: err.message, stack: err.stack }));
    return response(500, { error: 'Internal server error' });
  }
};

// ── UserData builder ──────────────────────────────────────────────────────────
/**
 * Builds the EC2 UserData bash script for a single crawl run.
 * The script installs Playwright, runs crawl.py (downloaded from S3),
 * uploads artifacts, updates DynamoDB, and self-terminates.
 */
function buildUserData({ sessionId, targetUrl, s3Bucket, s3Prefix, region, table, maxDepth, maxLinks }) {
  // The crawl.py source is expected to be pre-uploaded to S3 by deploy-api.sh.
  // Key: {s3Prefix}/crawler/crawl.py
  const crawlerS3Key = `${s3Prefix}/crawler/crawl.py`;

  return `#!/bin/bash
set -euxo pipefail
exec > /var/log/screenweave-crawler.log 2>&1

SESSION_ID="${sessionId}"
TARGET_URL="${targetUrl}"
S3_BUCKET="${s3Bucket}"
S3_PREFIX="${s3Prefix}"
DYNAMO_TABLE="${table}"
REGION="${region}"
MAX_DEPTH="${maxDepth}"
MAX_LINKS="${maxLinks}"
OUT_DIR="/opt/output"

# IMDSv2 instance ID
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \\
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \\
  http://169.254.169.254/latest/meta-data/instance-id)

echo "SESSION_ID : $SESSION_ID"
echo "TARGET_URL : $TARGET_URL"
echo "INSTANCE   : $INSTANCE_ID"

mkdir -p "$OUT_DIR/screenshots"

# ── 1. Install system packages ─────────────────────────────────────────────────
dnf update -y
dnf install -y python3-pip ImageMagick unzip tar xz

dnf install -y atk cups-libs gtk3 libXcomposite libXcursor libXdamage \\
  libXext libXi libXrandr libXScrnSaver libXtst pango alsa-lib \\
  at-spi2-atk at-spi2-core libdrm mesa-libgbm nss nspr \\
  libxkbcommon libgbm xdg-utils

# ── 2. Install Playwright + boto3 ──────────────────────────────────────────────
pip3 install playwright boto3
python3 -m playwright install chromium

# ── 3. Download crawler script from S3 ────────────────────────────────────────
aws s3 cp "s3://$S3_BUCKET/${crawlerS3Key}" /opt/crawl.py --region "$REGION"
echo "Crawler downloaded: $(wc -l /opt/crawl.py) lines"

# ── 4. Run crawler (passes session config via env vars) ────────────────────────
export SCREENWEAVE_SESSION_ID="$SESSION_ID"
export SCREENWEAVE_MAX_DEPTH="$MAX_DEPTH"
export SCREENWEAVE_MAX_LINKS="$MAX_LINKS"
export SCREENWEAVE_DYNAMO_TABLE="$DYNAMO_TABLE"
export SCREENWEAVE_S3_BUCKET="$S3_BUCKET"
export SCREENWEAVE_S3_PREFIX="$S3_PREFIX"
export SCREENWEAVE_REGION="$REGION"

python3 /opt/crawl.py "$TARGET_URL" || {
  # Mark session FAILED in DynamoDB on crawler error
  aws dynamodb update-item \\
    --table-name "$DYNAMO_TABLE" \\
    --key '{"session_id":{"S":"SESSION#'"$SESSION_ID"'"}}' \\
    --update-expression "SET #s = :s, updated_at = :u" \\
    --expression-attribute-names '{"#s":"status"}' \\
    --expression-attribute-values '{":s":{"S":"FAILED"},":u":{"S":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"}}' \\
    --region "$REGION"
  echo "Crawler FAILED – DynamoDB updated"
  aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --region "$REGION"
  exit 1
}

# ── 5. Upload artifacts to S3 ──────────────────────────────────────────────────
SESSION_S3="s3://$S3_BUCKET/$S3_PREFIX/$SESSION_ID"
echo "Uploading to: $SESSION_S3"

aws s3 cp "$OUT_DIR/states.json"      "$SESSION_S3/states.json"      --region "$REGION"
aws s3 cp "$OUT_DIR/transitions.json" "$SESSION_S3/transitions.json" --region "$REGION"
aws s3 cp "$OUT_DIR/trace.zip"        "$SESSION_S3/trace.zip"        --region "$REGION"
aws s3 sync "$OUT_DIR/screenshots/"   "$SESSION_S3/screenshots/"     --region "$REGION"
aws s3 cp /var/log/screenweave-crawler.log "$SESSION_S3/crawler.log" --region "$REGION"

echo "Upload complete"
echo "Screenshots: $(ls $OUT_DIR/screenshots/*.png 2>/dev/null | wc -l)"

# ── 6. Self-terminate ──────────────────────────────────────────────────────────
aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --region "$REGION"
`;
}

// ── Response builder ──────────────────────────────────────────────────────────
function response(statusCode, body) {
  return {
    statusCode,
    headers: {
      'Content-Type': 'application/json',
      'Cache-Control': 'no-store',
    },
    body: JSON.stringify(body),
  };
}
