# ScreenWeave

ScreenWeave provisions a short-lived AWS EC2 worker that:

1. Crawls a target website with Playwright + Chromium.
2. Captures full-page screenshots (including interactive/tabbed UI states).
3. Builds a stitched MP4 using `ffmpeg`.
4. Uploads artifacts to S3.
5. Self-terminates when finished.

The infrastructure is defined in CloudFormation (`template.yaml`) and deployed with a helper script (`deploy.sh`).

## What gets created

Deploying this project creates:

- **IAM Role** for EC2 with:
  - `AmazonSSMManagedInstanceCore` for Session Manager access.
  - Inline policy permissions for:
    - `s3:PutObject` to `s3://<bucket>/<prefix>/*`
    - `ec2:TerminateInstances` (self-termination)
- **IAM Instance Profile** attached to EC2.
- **Security Group** (egress only).
- **EC2 Instance** that runs all crawl/render/upload logic in `UserData`.

## Artifacts produced

By default, artifacts are uploaded under:

- `s3://<bucket>/website-videos/tarunraja_product_video.mp4`
- `s3://<bucket>/website-videos/screens.zip`
- `s3://<bucket>/website-videos/video-gen.log`

You can override the S3 prefix at deploy time.

## Prerequisites

- AWS CLI configured with credentials and permission to:
  - deploy CloudFormation stacks
  - create/attach IAM roles and instance profiles
  - launch EC2 instances
  - write to your S3 bucket
- An existing S3 bucket for outputs.
- Access to AWS Systems Manager Session Manager (optional, for live log viewing).

## Quick start

```bash
chmod +x deploy.sh
./deploy.sh <s3-bucket> [region] [target-url] [s3-prefix]
```

Examples:

```bash
# Use defaults for region, target, and prefix
./deploy.sh my-output-bucket

# Override everything
./deploy.sh my-output-bucket us-east-1 https://example.com demo-runs
```

## Script arguments (`deploy.sh`)

- `s3-bucket` (**required**) – destination bucket.
- `region` (default: `us-east-1`) – AWS region.
- `target-url` (default: `https://tarunraja.info`) – crawl root URL.
- `s3-prefix` (default: `website-videos`) – output folder prefix.

The stack name is hardcoded to:

- `tarunraja-video-gen`

## Runtime behavior

Inside EC2 `UserData`, the worker does the following:

1. Installs dependencies (`python3-pip`, ImageMagick, static ffmpeg, Chromium deps).
2. Installs Playwright and Chromium.
3. Writes and runs a Python crawler script.
4. Captures screenshots with a 1280x800 viewport.
5. Generates intro/outro title cards with ImageMagick.
6. Builds an MP4 via ffmpeg concat + scale/pad pipeline.
7. Uploads video/screens/log to S3.
8. Calls EC2 terminate on itself.

Crawler defaults in the embedded script:

- Max crawl depth: **2**
- Max internal links per page: **12**

## Monitoring progress

After deploy, the script prints commands to monitor execution.

### Attach via Session Manager

```bash
aws ssm start-session --target <instance-id> --region <region>
sudo tail -f /var/log/video-gen.log
```

### Poll S3 uploads

```bash
watch -n 15 "aws s3 ls s3://<bucket>/<prefix>/"
```

## Cleanup

Delete the stack when done:

```bash
aws cloudformation delete-stack --stack-name tarunraja-video-gen --region <region>
```

## CloudFormation parameters

`template.yaml` supports:

- `S3Bucket` (required)
- `S3Prefix` (default: `website-videos`)
- `TargetURL` (default: `https://tarunraja.info`)
- `InstanceType` (default: `t3.medium`, allowed: `t3.small|t3.medium|t3.large`)
- `AmiId` (default: latest Amazon Linux 2023 via SSM Parameter)

## Notes and limitations

- The generated MP4 output filename is currently fixed as `tarunraja_product_video.mp4`.
- The intro/outro card text is currently tailored to `tarunraja.info` branding in the embedded crawler.
- The security group has no inbound rules; debugging is expected through SSM.

## License

Apache-2.0. See [LICENSE](LICENSE).
