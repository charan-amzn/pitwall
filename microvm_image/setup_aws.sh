#!/usr/bin/env bash
# Idempotent: create the AWS resources Pitwall's MicroVM needs.
#
#   1. An S3 bucket to stage the build artifact (zip).
#   2. An IAM "build role" that Lambda assumes during image creation
#      (download the zip from S3, write build logs to CloudWatch).
#   3. An IAM "exec role" the MicroVM assumes at run time so the Claude Code
#      CLI *inside* the VM can InvokeModel against Bedrock via the standard
#      AWS credential chain — no keys ever land in the VM.
#
# Safe to re-run: existing resources are detected and reused, never recreated.
# Prints the env vars to drop into your .env when done.
#
# Requires: aws CLI with credentials, AWS_REGION (or --region default below).
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${PITWALL_BUILD_BUCKET:-pitwall-microvm-${ACCOUNT_ID}-${REGION}}"
ROLE_NAME="${PITWALL_BUILD_ROLE_NAME:-PitwallMicrovmBuildRole}"
EXEC_ROLE_NAME="${PITWALL_EXEC_ROLE_NAME:-PitwallMicrovmExecRole}"
export AWS_PAGER=""

echo "Account: $ACCOUNT_ID   Region: $REGION"
echo "Bucket:      $BUCKET"
echo "Build role:  $ROLE_NAME"
echo "Exec role:   $EXEC_ROLE_NAME"
echo

# --- 1. S3 bucket ----------------------------------------------------------
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "==> S3 bucket already exists: $BUCKET"
else
  echo "==> Creating S3 bucket: $BUCKET"
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=$REGION"
  fi
  aws s3api put-public-access-block --bucket "$BUCKET" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
fi

# --- 2. IAM build role -----------------------------------------------------
BUILD_TRUST='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "lambda.amazonaws.com" },
    "Action": ["sts:AssumeRole", "sts:TagSession"]
  }]
}'

BUILD_PERMS="$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/*" },
    { "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:*:*:*" }
  ]
}
JSON
)"

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "==> IAM build role already exists: $ROLE_NAME (updating trust + policy)"
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "$BUILD_TRUST"
else
  echo "==> Creating IAM build role: $ROLE_NAME"
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$BUILD_TRUST" \
    --description "Pitwall MicroVM image build role" >/dev/null
fi

aws iam put-role-policy --role-name "$ROLE_NAME" \
  --policy-name PitwallMicrovmBuildPolicy --policy-document "$BUILD_PERMS"

ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

# --- 3. IAM exec role (VM -> Bedrock) --------------------------------------
# Lambda MicroVMs assumes this on behalf of the running microVM so processes
# inside (specifically the Claude Code CLI) can call bedrock:InvokeModel with
# temporary credentials fetched from the container credentials endpoint.
EXEC_TRUST='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "lambda.amazonaws.com" },
    "Action": ["sts:AssumeRole", "sts:TagSession"]
  }]
}'

EXEC_PERMS="$(cat <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockInvoke",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/anthropic.*",
        "arn:aws:bedrock:*:*:inference-profile/*"
      ]
    }
  ]
}
JSON
)"

if aws iam get-role --role-name "$EXEC_ROLE_NAME" >/dev/null 2>&1; then
  echo "==> IAM exec role already exists: $EXEC_ROLE_NAME (updating trust + policy)"
  aws iam update-assume-role-policy --role-name "$EXEC_ROLE_NAME" --policy-document "$EXEC_TRUST"
else
  echo "==> Creating IAM exec role: $EXEC_ROLE_NAME"
  aws iam create-role --role-name "$EXEC_ROLE_NAME" \
    --assume-role-policy-document "$EXEC_TRUST" \
    --description "Pitwall MicroVM runtime role - grants Bedrock invoke to Claude Code inside the VM" >/dev/null
fi

aws iam put-role-policy --role-name "$EXEC_ROLE_NAME" \
  --policy-name PitwallMicrovmExecPolicy --policy-document "$EXEC_PERMS"

EXEC_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${EXEC_ROLE_NAME}"

# IAM role propagation can lag a few seconds; build_image.sh handles retries on
# the first create-microvm-image call.
if [ "${PITWALL_SETUP_QUIET:-}" != "1" ]; then
  cat <<EOF

==> Done. Resources ready (bucket + build role + exec role).

You normally don't need to set anything by hand — ./microvm_image/build_image.sh
ensures these exist and discovers them automatically. For reference:
  PITWALL_BUILD_BUCKET=$BUCKET
  PITWALL_BUILD_ROLE_ARN=$ROLE_ARN
  PITWALL_MICROVM_EXEC_ROLE_ARN=$EXEC_ROLE_ARN
  AWS_REGION=$REGION

Next:   ./microvm_image/build_image.sh
EOF
fi
