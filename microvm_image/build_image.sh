#!/usr/bin/env bash
# Build and register the Pitwall MicroVM image, then poll until it's ready.
#
# Self-healing: this one command ensures the S3 bucket and IAM build role exist
# (creating them via setup_aws.sh if missing), builds the dataset, and creates
# the image. You don't need to run setup_aws.sh first or set any ARNs by hand.
#
# Optional overrides (all auto-derived otherwise):
#   AWS_REGION              target region (default us-west-2)
#   PITWALL_BUILD_BUCKET    S3 bucket (default pitwall-microvm-<account>-<region>)
#   PITWALL_BUILD_ROLE_NAME IAM role name (default PitwallMicrovmBuildRole)
#   PITWALL_BASE_IMAGE_ARN  base image ARN (auto-discovered for the region)
#   PITWALL_IMAGE_NAME      image name (default pitwall-lab)
#
# Lambda runs the Dockerfile on its side, so you do NOT need local Docker.
set -euo pipefail
cd "$(dirname "$0")"
export AWS_PAGER=""

REGION="${AWS_REGION:-us-west-2}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${PITWALL_BUILD_BUCKET:-pitwall-microvm-${ACCOUNT_ID}-${REGION}}"
ROLE_NAME="${PITWALL_BUILD_ROLE_NAME:-PitwallMicrovmBuildRole}"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
EXEC_ROLE_NAME="${PITWALL_EXEC_ROLE_NAME:-PitwallMicrovmExecRole}"
EXEC_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${EXEC_ROLE_NAME}"
IMAGE_NAME="${PITWALL_IMAGE_NAME:-pitwall-lab}"
ZIP="pitwall-microvm.zip"

# --- self-heal: ensure the bucket + build role + exec role exist -----------
# The exec role is what lets Claude Code *inside* the VM call Bedrock without
# static credentials — checked here so a freshly-cloned repo still runs.
need_setup=0
aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null || need_setup=1
aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1 || need_setup=1
aws iam get-role --role-name "$EXEC_ROLE_NAME" >/dev/null 2>&1 || need_setup=1
if [ "$need_setup" = "1" ]; then
  echo "==> Ensuring AWS resources exist (bucket + build role + exec role)…"
  PITWALL_SETUP_QUIET=1 AWS_REGION="$REGION" PITWALL_BUILD_BUCKET="$BUCKET" \
    PITWALL_BUILD_ROLE_NAME="$ROLE_NAME" \
    PITWALL_EXEC_ROLE_NAME="$EXEC_ROLE_NAME" \
    bash setup_aws.sh
  echo "    waiting 10s for IAM role propagation…"
  sleep 10
else
  echo "==> Resources present (bucket: $BUCKET, build role: $ROLE_NAME, exec role: $EXEC_ROLE_NAME)"
fi

# Auto-discover the managed base image ARN for this region if not provided.
if [ -z "${PITWALL_BASE_IMAGE_ARN:-}" ]; then
  echo "==> Discovering managed base image for $REGION"
  PITWALL_BASE_IMAGE_ARN="$(aws lambda-microvms list-managed-microvm-images \
    --region "$REGION" --query 'items[0].imageArn' --output text)"
fi
echo "Base image: $PITWALL_BASE_IMAGE_ARN"

# Build the dataset unless it's already present (set PITWALL_REBUILD_DATA=1 to
# force a refetch). The fetch runs here on the build host, not in the VM.
if [ -f data/results.csv ] && [ -z "${PITWALL_REBUILD_DATA:-}" ]; then
  echo "==> Using existing dataset in ./data ($(head -1 data/_SOURCE.txt 2>/dev/null))"
else
  echo "==> Building dataset to ./data (source: ${PITWALL_DATA_SOURCE:-openf1})"
  "${PITWALL_PYTHON:-python3}" make_dataset.py
fi

echo "==> Packaging $ZIP (server.py, Dockerfile, data/)"
rm -f "$ZIP"
zip -r -q "$ZIP" server.py Dockerfile data

echo "==> Uploading to s3://$BUCKET/$ZIP"
aws s3 cp "$ZIP" "s3://$BUCKET/$ZIP" --region "$REGION"

# If an image with this name already exists, update it (a new version);
# otherwise create it. Either way we end up with a CREATED/UPDATED image.
EXISTING_ARN="$(aws lambda-microvms list-microvm-images --region "$REGION" \
  --query "items[?name=='$IMAGE_NAME'].imageArn | [0]" --output text 2>/dev/null || echo None)"

if [ "$EXISTING_ARN" != "None" ] && [ -n "$EXISTING_ARN" ]; then
  echo "==> Updating existing MicroVM image '$IMAGE_NAME' (new version)"
  aws lambda-microvms update-microvm-image \
    --image-identifier "$EXISTING_ARN" \
    --code-artifact "uri=s3://$BUCKET/$ZIP" \
    --base-image-arn "$PITWALL_BASE_IMAGE_ARN" \
    --build-role-arn "$ROLE_ARN" \
    --region "$REGION" >/dev/null
  IMAGE_ARN="$EXISTING_ARN"
else
  echo "==> Creating MicroVM image '$IMAGE_NAME'"
  aws lambda-microvms create-microvm-image \
    --name "$IMAGE_NAME" \
    --code-artifact "uri=s3://$BUCKET/$ZIP" \
    --base-image-arn "$PITWALL_BASE_IMAGE_ARN" \
    --build-role-arn "$ROLE_ARN" \
    --region "$REGION" >/dev/null
  # get-microvm-image requires the full ARN, not the short name — resolve it.
  IMAGE_ARN="$(aws lambda-microvms list-microvm-images --region "$REGION" \
    --query "items[?name=='$IMAGE_NAME'].imageArn | [0]" --output text)"
fi

echo "==> Waiting for build to finish (this can take several minutes)…"
for _ in $(seq 1 120); do
  STATE="$(aws lambda-microvms get-microvm-image \
    --image-identifier "$IMAGE_ARN" --region "$REGION" \
    --query 'state' --output text 2>/dev/null || echo UNKNOWN)"
  printf '\r    state: %-16s' "$STATE"
  case "$STATE" in
    CREATED|UPDATED) echo; break ;;
    CREATION_FAILED|UPDATE_FAILED)
      echo; echo "Build failed. Check logs: /aws/lambda/microvms/$IMAGE_NAME"; exit 1 ;;
  esac
  sleep 10
done

cat <<EOF

==> Image ready: $IMAGE_ARN
==> Exec role:   $EXEC_ROLE_ARN

You're set — Pitwall finds this image by name automatically. Just run:
    pitwall "Who had the fastest average pace in Miami?"
    pitwall-web        # or the web UI

The MicroVM assumes the exec role above so the Claude Code CLI running INSIDE
the VM can InvokeModel against Bedrock. Pitwall auto-derives the exec role's
ARN from the account/region, so no .env changes needed. Override with
PITWALL_MICROVM_EXEC_ROLE_ARN if you use a custom name.

(To pin a specific image instead of auto-discovery, set
 PITWALL_MICROVM_IMAGE_ARN=$IMAGE_ARN in your .env.)
EOF
