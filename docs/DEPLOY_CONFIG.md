# Deploy configuration (GitHub Actions)

ScreenWeave's `.github/workflows/deploy.yaml` deploy job does not embed AWS account
identifiers. Configure the following in the repository **before the next deploy**
(**Settings → Secrets and variables → Actions**).

Past commits may still contain old values in git history. Rotating or recreating
resources is outside this repository; only new deploys depend on the names below.

## Required repository variables

| Name | Purpose | Example placeholder |
|---|---|---|
| `ASSUME_ROLE_ARN` | IAM role ARN assumed via GitHub OIDC for deploy | `arn:aws:iam::123456789012:role/example-github-deploy-role` |
| `AWS_REGION` | Region for SAM deploy, S3 upload, and smoke test | `us-east-1` |
| `STACK_NAME` | CloudFormation stack name for ScreenWeave | `screenweave` |

## Optional repository variables

| Name | Purpose | Example placeholder |
|---|---|---|
| `AUTH_MODE` | `/mcp` auth: `audit` or `enforce` (default `audit` when unset) | `audit` |
| `CIPHERWEAVE_STACK_NAME` | CloudFormation stack exporting CipherWeave `IssuerJwksUrl` / `IssuerId` | `cipherweave-example` |
| `ALERT_EMAIL` | Email subscribed to ops SNS topic (omit for no subscriber) | `you@example.com` |
| `OBSERVATORY_METRICS_TABLE_NAME` | Shared Observatory DynamoDB table name | `example-shared-OBSERVATORY_METRICS` |
| `CONTEXTWEAVE_RAW_BUCKET_NAME` | ContextWeave raw-ingestion bucket for `export_session` | `example-contextweave-raw` |

## Required repository secret (when Observatory is enabled)

Set this **only** when `OBSERVATORY_METRICS_TABLE_NAME` is non-empty.

| Name | Purpose | Example placeholder |
|---|---|---|
| `OBSERVATORY_METRICS_TABLE_ARN` | Full DynamoDB table ARN for IAM `PutItem` scoping | `arn:aws:dynamodb:us-east-1:123456789012:table/example-shared-OBSERVATORY_METRICS` |

## SAM template parameters (local deploy)

For manual `sam deploy`, pass the same integration settings via
`--parameter-overrides` (placeholders only — never commit real ARNs):

```bash
sam deploy --resolve-s3 --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ObservatoryMetricsTableName=example-shared-OBSERVATORY_METRICS \
    ObservatoryMetricsTableArn=arn:aws:dynamodb:us-east-1:123456789012:table/example-shared-OBSERVATORY_METRICS \
    ContextWeaveRawBucketName=example-contextweave-raw \
    AlertEmail=you@example.com
```

## Deploy job validation

The workflow fails fast if `ASSUME_ROLE_ARN`, `AWS_REGION`, or `STACK_NAME` is missing,
or if Observatory table name is set without the matching ARN secret.

Deploy and smoke-test steps require live AWS credentials; they are not run on pull
requests.
