# Deploying the control API

App Runner runs the image built by `Dockerfile`. Everything the deployment needs to be told is an
environment variable, and the two secrets are references into Secrets Manager rather than values.

## What the service needs

| Variable | Why |
|---|---|
| `BIOINTAKE_BACKEND=aws` | DynamoDB and S3 rather than memory |
| `BIOINTAKE_DDB_TABLE` | the single table |
| `BIOINTAKE_S3_BUCKET` | artifacts and Strands sessions |
| `BIOINTAKE_INVOKER` | `agentcore` to call the deployed runtime, `local` to run the agent in-process |
| `BIOINTAKE_RUNTIME_ARN` | required when the invoker is `agentcore` |
| `BIOINTAKE_MODEL_ID` | the Bedrock model, or `anthropic:<model>` |
| `BIOINTAKE_PORTAL_BASE_URL` | the origin a sender's link points at; without it links stay relative |
| `BIOINTAKE_CORS_ORIGINS` | where the console is served from |
| `BIOINTAKE_DEMO_SIGN_IN` | `1` offers each staff member's token on the sign-in screen, one click each. For a deployment meant to be reviewed; never for a lab |
| `BIOINTAKE_DELIVERY` | `ses` to send evidence requests, `recorded` to file them |
| `BIOINTAKE_MAIL_FROM` | a verified SES address, required when delivery is `ses` |
| **`BIOINTAKE_USERS`** (secret) | the lab's staff and their tokens; the service refuses to start without it |
| **`ANTHROPIC_API_KEY`** (secret) | only when `BIOINTAKE_MODEL_ID` starts with `anthropic:` |

`BIOINTAKE_USERS` is `user_id|Display Name|ROLE|token`, entries separated by `;`. Mint the tokens
with `python -c "from biointake.services.auth import mint_token; print(mint_token())"` and put the
whole string in Secrets Manager. It is the only place a plaintext token exists; the service stores
only its SHA-256.

## What the instance role needs

- `dynamodb:GetItem`, `PutItem`, `Query`, `DeleteItem`, `BatchWriteItem` on the table
- `s3:GetObject`, `PutObject`, `ListBucket`, `DeleteObject` on the bucket
- `bedrock-agentcore:InvokeAgentRuntime` on the runtime, when the invoker is `agentcore`
- `bedrock:InvokeModel` and `InvokeModelWithResponseStream`, when the agent runs in-process
- `ses:SendEmail`, when delivery is `ses`
- `secretsmanager:GetSecretValue` on the two secrets, for the *access* role rather than the
  instance role: App Runner resolves secret references before the container starts

## Health

`GET /health` is unauthenticated and answers only once the repository is reachable, so an instance
that cannot see its table is never sent traffic. Point App Runner's health check at it.

## Building

The image builds from the repository root, not from this directory:

```bash
docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
  --output type=docker -f deploy/api/Dockerfile -t biointake-api .
```

Those flags are not optional when the image is bound for App Runner. Left to itself buildx pushes
an OCI image index whose second entry is an `unknown/unknown` attestation manifest; App Runner
pulls it, refuses to run it, and writes no application log saying why. Turning off provenance and
SBOM is not enough on its own, the exporter still emits an index, so the build goes through
`--output type=docker`. Confirm with:

```bash
aws ecr batch-get-image --repository-name biointake-api --image-ids imageTag=latest \
  --query 'images[0].imageManifest' --output text | head -c 120
```

It must say `application/vnd.docker.distribution.manifest.v2+json`, not `oci.image.index`.

`scripts/deploy_apprunner.py` builds it, pushes it to ECR, and creates or updates the service.
