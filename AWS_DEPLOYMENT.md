# Deploying to AWS with Docker

This guide walks through hosting the existing `backend/` FastAPI service (see
[Dockerfile](Dockerfile)) on AWS using the Docker image already defined in this
repo. It does not require any changes to the application code or
`requirements.txt` — it only adds AWS-side configuration.

The recommended path is **AWS App Runner**, which is the closest AWS
equivalent to the existing Render setup ([render.yaml](render.yaml)): you
give it a container image and it handles the load balancer, TLS, scaling,
and public URL for you. An alternative using **ECS Fargate** is included at
the end for cases where you need more control (VPC placement, custom
networking, multiple services, etc.).

This guide was validated end-to-end against a real AWS account; every step
below reflects what actually worked, including three things that are easy to
miss and will otherwise cause a failed deployment (flagged inline: a second
IAM role App Runner needs that isn't obvious from the console, and a CPU
architecture mismatch if you're building on an Apple Silicon Mac).

## Prerequisites

- An AWS account.
- [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
  installed and configured.
- Docker Desktop (or equivalent) installed and **running** — `docker --version`
  only checks the CLI is installed; the commands below need the daemon itself
  running, which you can confirm with `docker info`.
- Your `ANTHROPIC_API_KEY`.

### Create a scoped IAM user (don't deploy as root)

If `aws sts get-caller-identity` shows an `Arn` ending in `:root`, you're
authenticated as the AWS account root user, which has unrestricted access to
everything in the account. Create a dedicated user instead, with only the
permissions this deployment needs:

```bash
aws iam create-user --user-name vanderbilt-db-assistant-deployer

aws iam attach-user-policy --user-name vanderbilt-db-assistant-deployer --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryFullAccess
aws iam attach-user-policy --user-name vanderbilt-db-assistant-deployer --policy-arn arn:aws:iam::aws:policy/AWSAppRunnerFullAccess
aws iam attach-user-policy --user-name vanderbilt-db-assistant-deployer --policy-arn arn:aws:iam::aws:policy/SecretsManagerReadWrite
aws iam attach-user-policy --user-name vanderbilt-db-assistant-deployer --policy-arn arn:aws:iam::aws:policy/CloudWatchLogsReadOnlyAccess

aws iam put-user-policy --user-name vanderbilt-db-assistant-deployer --policy-name AppRunnerRoleCreation --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["iam:CreateRole","iam:GetRole","iam:AttachRolePolicy","iam:PutRolePolicy","iam:ListAttachedRolePolicies","iam:TagRole"],"Resource":"arn:aws:iam::<ACCOUNT_ID>:role/AppRunner*"},{"Effect":"Allow","Action":"iam:PassRole","Resource":"arn:aws:iam::<ACCOUNT_ID>:role/AppRunner*"}]}'

aws iam create-access-key --user-name vanderbilt-db-assistant-deployer
```

Replace `<ACCOUNT_ID>` with your account ID (from `aws sts get-caller-identity`).
The last command prints an `AccessKeyId` and `SecretAccessKey` — **do not paste
the secret value anywhere it could be logged** (including a chat/terminal
transcript you might share). Copy it straight into:

```bash
aws configure --profile vdba
```

answering the four prompts (Access Key ID, Secret Access Key, region, `json`
output format). From here on, every command in this guide assumes
`AWS_PROFILE=vdba` is set (or `--profile vdba` appended), **except** the two
places marked "as root" below — creating/editing this user's own IAM policies
requires a more privileged identity, since a user correctly can't grant
itself more permissions.

A convenience script, `deploy-env.sh` (gitignored — it contains your account
ID), holds these as shell variables:

```bash
export AWS_PROFILE=vdba
export AWS_REGION=us-east-1
export AWS_ACCOUNT_ID=<your account id>
export ECR_REPO=vanderbilt-db-assistant
```

`source deploy-env.sh` before running anything below.

Confirm you're set up:

```bash
aws sts get-caller-identity
docker info
```

---

## Option A: AWS App Runner (recommended, simplest)

### 1. Create the ECR repository

```bash
aws ecr create-repository \
  --repository-name "$ECR_REPO" \
  --region "$AWS_REGION"
```

### 2. Build and push the image

**If you're on an Apple Silicon Mac (M1/M2/M3/M4), you must pass
`--platform linux/amd64`.** Without it, `docker build` defaults to an ARM64
image; Docker Desktop runs that fine locally (masking the problem completely
in local testing), but AWS App Runner's infrastructure is x86_64 and cannot
run it — the container fails to start with no error message beyond "Failed
to deploy your application image," and no application logs are ever
produced, since the process never actually launches. Check your own
architecture with `uname -m` if unsure.

```bash
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin \
    "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

docker build --platform linux/amd64 -t "$ECR_REPO" .

docker tag "$ECR_REPO:latest" \
  "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO:latest"

docker push \
  "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO:latest"
```

This uses the exact same `Dockerfile` that already runs
`uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}` — nothing about the
application changes for AWS, only the target architecture of the build.

### 3. Store the Anthropic API key in Secrets Manager

```bash
aws secretsmanager create-secret \
  --name vanderbilt-db-assistant/anthropic-api-key \
  --secret-string "$ANTHROPIC_API_KEY" \
  --region "$AWS_REGION"
```

**Copy the exact `ARN` this returns** (it includes a random 6-character
suffix AWS appends, e.g. `...anthropic-api-key-PIFH4R`) — reconstructing it
manually without that suffix will not resolve correctly later.

### 4. Create the ECR access role

This lets App Runner *pull your image* from ECR:

```bash
aws iam create-role \
  --role-name AppRunnerECRAccessRole \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"build.apprunner.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

aws iam attach-role-policy \
  --role-name AppRunnerECRAccessRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess
```

Verify before moving on — this is the single most common point of failure:

```bash
aws iam list-attached-role-policies --role-name AppRunnerECRAccessRole
```

Should list `AWSAppRunnerServicePolicyForECRAccess`.

### 5. Create the instance role

**This is a second, separate role that the original version of this guide
omitted.** The ECR access role above only lets App Runner *pull* the image;
it has nothing to do with what the *running container* can access. Because
this app needs `ANTHROPIC_API_KEY` injected from Secrets Manager at startup
(`RuntimeEnvironmentSecrets`), App Runner needs a second role — the
**instance role** — with permission to read that specific secret. Without
it, `create-service` fails with `InvalidRequestException: Instance Role have
to be provided if passing in RuntimeEnvironmentSecrets`; and if you create
the instance role *after* calling `create-service` instead of before, the
service will reach `CREATE_FAILED` and need to be deleted and recreated.

```bash
aws iam create-role \
  --role-name AppRunnerInstanceRole \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"tasks.apprunner.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

aws iam put-role-policy \
  --role-name AppRunnerInstanceRole \
  --policy-name SecretsAccess \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"secretsmanager:GetSecretValue","Resource":"<SECRET_ARN_FROM_STEP_3>"}]}'
```

### 6. Create the App Runner service

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
          "ANTHROPIC_API_KEY": "<SECRET_ARN_FROM_STEP_3>"
        }
      }
    },
    "AutoDeploymentsEnabled": false
  }' \
  --instance-configuration '{
    "InstanceRoleArn": "arn:aws:iam::'"$AWS_ACCOUNT_ID"':role/AppRunnerInstanceRole"
  }'
```

Notes:
- `Port: "8080"` matches the `EXPOSE 8080` / default `PORT=8080` already set
  in the [Dockerfile](Dockerfile).
- `--instance-configuration` (step 6) is what step 5's role is actually for —
  omitting it is what produces the `InvalidRequestException` above.
- `AutoDeploymentsEnabled: false` means App Runner won't redeploy
  automatically on every image push — set to `true` if you want that.

### 7. Get the public URL and check status

```bash
aws apprunner describe-service \
  --service-arn <ServiceArn-from-previous-step-output> \
  --region "$AWS_REGION"
```

Look at `Status`. A first deployment typically takes 3-5 minutes and will
read `OPERATION_IN_PROGRESS` for a while — re-run the same command every
minute or so until it becomes `RUNNING` (success) or `CREATE_FAILED`.

If it fails, check the logs before guessing further — App Runner creates two
CloudWatch log groups per service, `.../service` (deployment/lifecycle
events) and `.../application` (your app's actual stdout/stderr, including
any Python traceback). **If `.../application` never gets created at all**,
that's a strong signal the container never actually started — i.e. an IAM
role/secret problem (see steps 4-5), not an application bug:

```bash
aws logs describe-log-groups --log-group-name-prefix "/aws/apprunner/vanderbilt-db-assistant/<ServiceId>"
aws logs tail "/aws/apprunner/vanderbilt-db-assistant/<ServiceId>/service" --since 1h
aws logs tail "/aws/apprunner/vanderbilt-db-assistant/<ServiceId>/application" --since 1h
```

If you suspect an application-level bug instead, reproduce it locally first —
much faster than iterating through AWS:

```bash
docker run --rm -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" -e PORT=8080 -p 8080:8080 "$ECR_REPO"
```

Once `Status` is `RUNNING`, smoke-test it:

```bash
curl https://<service-url>/health
# {"status":"ok","catalog_size":887}
```

If a service ever lands in `CREATE_FAILED`, it can't be repaired in place —
delete it and re-run `create-service`:

```bash
aws apprunner delete-service --service-arn <ServiceArn>
aws apprunner list-services --query 'ServiceSummaryList[].ServiceName'   # confirm it's gone before recreating
```

### 8. Point the frontend at the new backend

[docs/index.html](docs/index.html) sets `BACKEND_URL` to your Render URL for
any non-localhost origin. Change it to your App Runner `ServiceUrl` (from
step 6/7's output, prefixed with `https://`). This is the only place outside
of AWS config that needs a value changed — a config value, not application
logic — and it only takes effect once you commit and publish this file
wherever `docs/` is actually served from (e.g. GitHub Pages).

### 9. Redeploying after code changes

Whenever `backend/`, `data/`, or the `Dockerfile` change:

```bash
docker build --platform linux/amd64 -t "$ECR_REPO" .
docker tag "$ECR_REPO:latest" "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO:latest"
docker push "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO:latest"
aws apprunner start-deployment --service-arn <ServiceArn> --region "$AWS_REGION"
```

(Your ECR login expires after 12 hours — if the push fails on auth, re-run
the `aws ecr get-login-password | docker login ...` command from step 2.)

---

## Option B: ECS Fargate (more control, more setup)

Use this instead of App Runner if you need a VPC, an Application Load
Balancer, multiple containers/services, or tighter network controls.

1. **Push the image to ECR** — same as steps 1-2 above (still needs
   `--platform linux/amd64` on Apple Silicon).
2. **Create an ECS cluster:**
   ```bash
   aws ecs create-cluster --cluster-name vanderbilt-db-assistant --region "$AWS_REGION"
   ```
3. **Register a task definition** referencing the ECR image, with:
   - `runtimePlatform.cpuArchitecture` set to `X86_64` explicitly.
   - Container port `8080` (matches the Dockerfile's `EXPOSE 8080`).
   - A **task role** (Fargate's equivalent of App Runner's instance role)
     with `secretsmanager:GetSecretValue` on the secret from step 3, and the
     secret referenced under `secrets` in the container definition (not
     `environment`).
   - CPU/memory sized to taste (0.5 vCPU / 1GB is enough to start).
4. **Create a Fargate service** in that cluster, attached to an Application
   Load Balancer target group forwarding port 80/443 → container port 8080.
5. **Health check path:** point the target group's health check at `/health`
   (implemented in [backend/main.py](backend/main.py:244)).
6. Redeploy by pushing a new image tag and updating the service
   (`aws ecs update-service --force-new-deployment`).

The AWS Console's "Create Service" wizard under ECS → Fargate can do all of
steps 3-4 in one guided flow if you prefer not to script it.

---

## What this does *not* change

- The `Dockerfile`, `backend/`, `data/`, and existing Docker/Render workflows
  described in the [README](README.md) are untouched and still work exactly
  as before — this is an additional hosting target, not a replacement, until
  you actually repoint the frontend (step 8) and stop using the old one.
- The CLI tool (`vanderbilt_database_assistant.py`) runs locally and is
  unrelated to any of the above; nothing here affects it.
