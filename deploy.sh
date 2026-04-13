#!/usr/bin/env bash
# Usage: ./deploy.sh <s3-bucket> [region] [target-url] [s3-prefix]
set -euo pipefail

BUCKET="${1:?Usage: ./deploy.sh <s3-bucket> [region] [target-url] [s3-prefix]}"
REGION="${2:-us-east-1}"
TARGET="${3:-https://tarunraja.info}"
PREFIX="${4:-website-videos}"
STACK="tarunraja-video-gen"

echo "🚀  Stack  : $STACK"
echo "    Target : $TARGET"
echo "    Output : s3://$BUCKET/$PREFIX/"
echo "    Region : $REGION"
echo ""

aws cloudformation deploy \
  --template-file template.yaml \
  --stack-name "$STACK" \
  --region "$REGION" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      S3Bucket="$BUCKET" \
      S3Prefix="$PREFIX" \
      TargetURL="$TARGET" \
  --no-fail-on-empty-changeset

echo ""
echo "✅  Launched. EC2 is now:"
echo "    1. Installing Playwright + Chromium + ffmpeg"
echo "    2. Crawling $TARGET (depth 2, 12 links/page)"
echo "    3. Stitching MP4"
echo "    4. Uploading → s3://$BUCKET/$PREFIX/tarunraja_product_video.mp4"
echo "    5. Self-terminating"
echo ""
echo "⏱   ETA: ~5-8 minutes"
echo ""

INSTANCE=$(aws cloudformation describe-stack-resource \
  --stack-name "$STACK" \
  --logical-resource-id VideoGenInstance \
  --region "$REGION" \
  --query "StackResourceDetail.PhysicalResourceId" \
  --output text 2>/dev/null || echo "<instance-id>")

echo "📡  Watch logs:"
echo "    aws ssm start-session --target $INSTANCE --region $REGION"
echo "    sudo tail -f /var/log/video-gen.log"
echo ""
echo "📺  Poll S3:"
echo "    watch -n 15 \"aws s3 ls s3://$BUCKET/$PREFIX/\""
echo ""
echo "🗑   Cleanup:"
echo "    aws cloudformation delete-stack --stack-name $STACK --region $REGION"
