#!/usr/bin/env bash
# Rebuild the backend image and redeploy it to the existing AWS App Runner
# service. See AWS_DEPLOYMENT.md for the one-time setup this assumes is
# already done (ECR repo, IAM roles, Secrets Manager secret, App Runner
# service). Run this any time backend/, data/, or the Dockerfile change.
#
# Usage:
#   ./redeploy.sh

set -euo pipefail
cd "$(dirname "$0")"

echo "== Loading AWS deploy environment =="
source ./deploy-env.sh

: "${APP_RUNNER_SERVICE_ARN:?deploy-env.sh must set APP_RUNNER_SERVICE_ARN}"

if ! docker info > /dev/null 2>&1; then
  echo "Error: Docker daemon isn't running. Start Docker Desktop and try again." >&2
  exit 1
fi

IMAGE_URI="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO:latest"

echo
echo "== Logging in to ECR =="
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

echo
echo "== Building image (linux/amd64 -- required even on Apple Silicon; App Runner is x86_64) =="
docker build --platform linux/amd64 -t "$ECR_REPO" .

echo
echo "== Tagging and pushing to ECR =="
docker tag "$ECR_REPO:latest" "$IMAGE_URI"
docker push "$IMAGE_URI"

echo
echo "== Triggering App Runner deployment =="
aws apprunner start-deployment --service-arn "$APP_RUNNER_SERVICE_ARN" --region "$AWS_REGION" > /dev/null
echo "Deployment started."

echo
echo "== Waiting for it to finish (checking every 15s, up to ~5 minutes) =="
for _ in $(seq 1 20); do
  sleep 15
  STATUS=$(aws apprunner describe-service --service-arn "$APP_RUNNER_SERVICE_ARN" --region "$AWS_REGION" --query 'Service.Status' --output text)
  echo "Status: $STATUS"
  if [ "$STATUS" = "RUNNING" ]; then
    SERVICE_URL=$(aws apprunner describe-service --service-arn "$APP_RUNNER_SERVICE_ARN" --region "$AWS_REGION" --query 'Service.ServiceUrl' --output text)
    echo
    echo "Deployed successfully -> https://$SERVICE_URL"
    exit 0
  fi
  if [ "$STATUS" != "OPERATION_IN_PROGRESS" ]; then
    echo "Unexpected status '$STATUS' -- check the CloudWatch logs (see AWS_DEPLOYMENT.md's troubleshooting section)." >&2
    exit 1
  fi
done

echo "Still in progress after 5 minutes -- keep checking manually:" >&2
echo "  aws apprunner describe-service --service-arn \"$APP_RUNNER_SERVICE_ARN\" --region \"$AWS_REGION\" --query 'Service.Status' --output text" >&2
exit 1
