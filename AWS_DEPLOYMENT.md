# Deploying to AWS with Docker

This guide walks through hosting the existing `backend/` FastAPI service (see
[Dockerfile](Dockerfile)) on AWS using the Docker image already defined in this
repo. It does not require any changes to the application code, `Dockerfile`,
or `requirements.txt` — it only adds AWS-side configuration.

The recommended path is **AWS App Runner**, which is the closest AWS
equivalent to the existing Render setup ([render.yaml](render.yaml)): you
give it a container image and it handles the load balancer, TLS, scaling,
and public URL for you. An alternative using **ECS Fargate** is included at
the end for cases where you need more control (VPC placement, custom
networking, multiple services, etc.).

## Prerequisites

- An AWS account with permission to use ECR, App Runner (or ECS), and IAM.
- [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
  installed and configured (`aws configure`) with a user/role that has
  access to the services above.
- Docker installed locally (Docker Desktop or equivalent).
- Your `ANTHROPIC_API_KEY`.

Run these once to confirm you're set up:

```bash
aws sts get-caller-identity
docker --version
```

---

## Option A: AWS App Runner (recommended, simplest)

### 1. Set a few shell variables

```bash
export AWS_REGION=us-east-1
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ECR_REPO=vanderbilt-db-assistant
```

Adjust `AWS_REGION` to whichever region you want to deploy in.

### 2. Create an ECR repository to hold the image

```bash
aws ecr create-repository \
  --repository-name "$ECR_REPO" \
  --region "$AWS_REGION"
```

### 3. Build the existing Dockerfile and push it to ECR

From the project root (same directory as `Dockerfile`):

```bash
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin \
    "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

docker build -t "$ECR_REPO" .

docker tag "$ECR_REPO:latest" \
  "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO:latest"

docker push \
  "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO:latest"
```

This uses the exact same `Dockerfile` that already runs
`uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}` — nothing about the
image changes for AWS.

### 4. Store the Anthropic API key in AWS Secrets Manager

Don't put the key in plaintext in App Runner's console/config; store it as a
secret and reference it instead:

```bash
aws secretsmanager create-secret \
  --name vanderbilt-db-assistant/anthropic-api-key \
  --secret-string "$ANTHROPIC_API_KEY" \
  --region "$AWS_REGION"
```

### 5. Create an App Runner service

You can do this via the console (Services → App Runner → Create service →
"Container registry" → select the ECR image) or via CLI:

```bash
aws apprunner create-service \
  --region "$AWS_REGION" \
  --service-name vanderbilt-db-assistant \
  --source-configuration '{
    "AuthenticationConfiguration": {
      "AccessRoleArn": "arn:aws:iam::'"$AWS_ACCOUNT_ID"':role/AppRunnerECRAccessRole"
    },
    "ImageRepository": {
      "ImageIdentifier": "'"$AWS_ACCOUNT_ID"'.dkr.ecr.'"$AWS_REGION"'.amazonaws.com/'"$ECR_REPO"':latest",
      "ImageRepositoryType": "ECR",
      "ImageConfiguration": {
        "Port": "8080",
        "RuntimeEnvironmentSecrets": {
          "ANTHROPIC_API_KEY": "arn:aws:secretsmanager:'"$AWS_REGION"':'"$AWS_ACCOUNT_ID"':secret:vanderbilt-db-assistant/anthropic-api-key"
        }
      }
    },
    "AutoDeploymentsEnabled": false
  }'
```

Notes:
- `Port: "8080"` matches the `EXPOSE 8080` / default `PORT=8080` already set
  in the [Dockerfile](Dockerfile).
- `AppRunnerECRAccessRole` needs to exist first with a trust policy for
  `build.apprunner.amazonaws.com` and the
  `AWSAppRunnerServicePolicyForECRAccess` managed policy attached. The
  console flow creates this role for you automatically the first time if you
  don't want to create it by hand.
- `AutoDeploymentsEnabled: false` means App Runner won't redeploy
  automatically on every image push — set to `true` if you want that.

### 6. Get the public URL and smoke-test it

```bash
aws apprunner describe-service \
  --service-arn <ServiceArn-from-previous-step-output> \
  --region "$AWS_REGION" \
  --query 'Service.ServiceUrl' --output text
```

Then:

```bash
curl https://<service-url>/health
# {"status":"ok","catalog_size":887}
```

### 7. Point the frontend at the new backend

[docs/index.html](docs/index.html) calls the backend's `/chat` endpoint.
Update whatever base URL it (or wherever it's hosted, e.g. GitHub Pages)
points to, so it targets the new App Runner URL instead of localhost/Render.
This is the only place outside of AWS config that needs a value changed, and
it's a config value, not application logic.

### 8. Redeploying after code changes

Whenever `backend/`, `data/`, or the `Dockerfile` change:

```bash
docker build -t "$ECR_REPO" .
docker tag "$ECR_REPO:latest" "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO:latest"
docker push "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO:latest"
aws apprunner start-deployment --service-arn <ServiceArn> --region "$AWS_REGION"
```

---

## Option B: ECS Fargate (more control, more setup)

Use this instead of App Runner if you need a VPC, an Application Load
Balancer, multiple containers/services, or tighter network controls.

1. **Push the image to ECR** — same as steps 1–3 above.
2. **Create an ECS cluster:**
   ```bash
   aws ecs create-cluster --cluster-name vanderbilt-db-assistant --region "$AWS_REGION"
   ```
3. **Register a task definition** referencing the ECR image, with:
   - Container port `8080` (matches the Dockerfile's `EXPOSE 8080`)
   - An environment secret for `ANTHROPIC_API_KEY` sourced from Secrets
     Manager (same secret created in step 4 above), using
     `secrets` in the container definition rather than `environment`
   - CPU/memory sized to taste (0.5 vCPU / 1GB is enough to start)
4. **Create a Fargate service** in that cluster, attached to an Application
   Load Balancer target group forwarding port 80/443 → container port 8080.
5. **Health check path:** point the target group's health check at `/health`
   (already implemented in [backend/main.py](backend/main.py:178)).
6. Redeploy by pushing a new image tag and updating the service
   (`aws ecs update-service --force-new-deployment`).

The AWS Console's "Create Service" wizard under ECS → Fargate can do all of
steps 3–4 in one guided flow if you prefer not to script it.

---

## What this does *not* change

- The `Dockerfile`, `backend/`, `data/`, and existing Docker/Render workflows
  described in the [README](README.md) are untouched and still work exactly
  as before — this is an additional hosting target, not a replacement.
- The CLI tool (`vanderbilt_database_assistant.py`) runs locally and is
  unrelated to any of the above; nothing here affects it.
