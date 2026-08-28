"""Put the control API on App Runner: build the image, push it to ECR, create or update the service.

    uv run python scripts/deploy_apprunner.py --profile biointake-hack --region us-east-1 \
        --secret-users biointake/users

Everything it makes is named `biointake-api` so a second run updates rather than duplicates. It
refuses to create a service without the users secret, because a BioIntake with no configured staff
refuses to start anyway and would fail its health check in a loop.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
NAME = "biointake-api"


def run(*cmd: str, stdin: bytes | None = None) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, input=stdin)


def ensure_repository(ecr, name: str) -> str:
    try:
        described = ecr.describe_repositories(repositoryNames=[name])
    except ecr.exceptions.RepositoryNotFoundException:
        described = {"repositories": [ecr.create_repository(repositoryName=name)["repository"]]}
        print(f"ecr: created {name}")
    return str(described["repositories"][0]["repositoryUri"])


def build_and_push(session, uri: str, tag: str) -> str:
    """Build from the repository root; the Dockerfile expects that context, not deploy/api."""
    ecr = session.client("ecr")
    auth = ecr.get_authorization_token()["authorizationData"][0]
    user, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
    run(
        "docker",
        "login",
        "--username",
        user,
        "--password-stdin",
        auth["proxyEndpoint"],
        stdin=password.encode(),
    )
    image = f"{uri}:{tag}"
    # App Runner needs a plain single-platform Docker manifest. Left to itself buildx pushes an OCI
    # image index whose second entry is an unknown/unknown attestation manifest; App Runner pulls
    # that image successfully, refuses to run it, and writes no application log saying why. Turning
    # off provenance and SBOM is not sufficient on its own, the exporter still emits an index, so
    # the build goes through `--output type=docker`, which produces one image for one platform.
    run(
        "docker",
        "buildx",
        "build",
        "--platform",
        "linux/amd64",
        "--provenance=false",
        "--sbom=false",
        "--output",
        "type=docker",
        "-f",
        str(ROOT / "deploy" / "api" / "Dockerfile"),
        "-t",
        image,
        str(ROOT),
    )
    run("docker", "push", image)
    verify_plain_manifest(ecr, uri.split("/")[-1], tag)
    return image


def verify_plain_manifest(ecr, repository: str, tag: str) -> None:
    """Refuse to go further on the manifest shape App Runner cannot run.

    Checked here rather than left to the service, because the failure it causes is a CREATE_FAILED
    with no application logs, and a service in that state has to be deleted before it can be tried
    again. Ten seconds here saves fifteen minutes there.
    """
    manifest = json.loads(
        ecr.batch_get_image(repositoryName=repository, imageIds=[{"imageTag": tag}])["images"][0][
            "imageManifest"
        ]
    )
    if "manifests" in manifest:
        raise SystemExit(
            f"pushed {manifest.get('mediaType')}, an image index. App Runner cannot run one, and "
            "will fail without saying so. Check that this Docker supports --output type=docker."
        )


def role_arn(iam, name: str, trust: dict, policy: dict) -> str:
    """Create or refresh a role. The inline policy is replaced every run so the deployed
    permissions are whatever this file says, rather than whatever an earlier run left behind."""
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="BioIntake control API on App Runner",
        )["Role"]["Arn"]
        print(f"iam: created {name}")
        time.sleep(10)  # IAM is eventually consistent; App Runner rejects a role it cannot yet see
    iam.put_role_policy(RoleName=name, PolicyName=f"{name}-inline", PolicyDocument=json.dumps(policy))
    return str(arn)


def _wait(runner, arn: str) -> None:
    """Block until the service is out of OPERATION_IN_PROGRESS; it rejects overlapping operations."""
    while runner.describe_service(ServiceArn=arn)["Service"]["Status"] == "OPERATION_IN_PROGRESS":
        time.sleep(10)


def main() -> int:  # noqa: C901
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--table", default="biointake-demo")
    ap.add_argument("--bucket", default=None)
    ap.add_argument("--tag", default="latest")
    ap.add_argument("--secret-users", required=True, help="Secrets Manager name or ARN of BIOINTAKE_USERS")
    ap.add_argument("--secret-anthropic", default="", help="likewise for ANTHROPIC_API_KEY, if used")
    ap.add_argument("--model-id", default="anthropic:claude-sonnet-5")
    ap.add_argument("--invoker", default="local", choices=["local", "agentcore"])
    ap.add_argument("--runtime-arn", default="")
    ap.add_argument("--portal-base-url", default="", help="the origin a sender's link points at")
    ap.add_argument("--cors-origins", default="", help="where the console is served from")
    ap.add_argument(
        "--demo-sign-in",
        action="store_true",
        help="offer each staff member's token on the sign-in screen, for a deployment being reviewed",
    )
    ap.add_argument("--delivery", default="recorded", choices=["recorded", "ses"])
    ap.add_argument("--mail-from", default="")
    ap.add_argument("--skip-build", action="store_true", help="reuse the image already at --tag")
    args = ap.parse_args()

    if args.invoker == "agentcore" and not args.runtime_arn:
        print("--invoker agentcore needs --runtime-arn", file=sys.stderr)
        return 2
    if args.delivery == "ses" and not args.mail_from:
        print("--delivery ses needs --mail-from, a verified SES address", file=sys.stderr)
        return 2

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    bucket = args.bucket or f"biointake-demo-{account}"
    ecr = session.client("ecr")
    iam = session.client("iam")
    runner = session.client("apprunner")

    uri = ensure_repository(ecr, NAME)
    image = f"{uri}:{args.tag}" if args.skip_build else build_and_push(session, uri, args.tag)

    # Every secret is resolved to its full ARN before it is referenced. App Runner accepts a bare
    # name in RuntimeEnvironmentSecrets without complaint and then never resolves it: the variable
    # arrives unset, the container exits on the missing configuration, and the service reports
    # CREATE_FAILED with no application log. Resolving here also fails loudly on a typo.
    sm = session.client("secretsmanager")

    def secret_arn(name: str) -> str:
        if name.startswith("arn:"):
            return name
        try:
            return str(sm.describe_secret(SecretId=name)["ARN"])
        except sm.exceptions.ResourceNotFoundException:
            raise SystemExit(f"no secret named {name} in {args.region}") from None

    args.secret_users = secret_arn(args.secret_users)
    if args.secret_anthropic:
        args.secret_anthropic = secret_arn(args.secret_anthropic)
    secret_arns = [s for s in (args.secret_users, args.secret_anthropic) if s]

    # The access role pulls the image. It is granted the secret too, because the two roles are
    # documented inconsistently and a redundant read on one named secret is cheaper than another
    # fifteen-minute CREATE_FAILED; the instance role is the one that actually resolves them.
    access_arn = role_arn(
        iam,
        f"{NAME}-access",
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Principal": {"Service": "build.apprunner.amazonaws.com"},
                }
            ],
        },
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "ecr:GetAuthorizationToken",
                        "ecr:BatchCheckLayerAvailability",
                        "ecr:GetDownloadUrlForLayer",
                        "ecr:BatchGetImage",
                        "ecr:DescribeImages",
                    ],
                    "Resource": "*",
                },
                *(
                    [{"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": secret_arns}]
                    if secret_arns
                    else []
                ),
            ],
        },
    )

    instance_statements: list[dict] = [
        # RuntimeEnvironmentSecrets are resolved with the *instance* role, not the access role. Get
        # this wrong and the image pulls, the container never starts, and App Runner reports
        # CREATE_FAILED with no application log to say what happened.
        *(
            [{"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": secret_arns}]
            if secret_arns
            else []
        ),
        {
            "Effect": "Allow",
            "Action": [
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:Query",
                "dynamodb:DeleteItem",
                "dynamodb:BatchWriteItem",
                "dynamodb:UpdateItem",
                # Scan backs list_cases and the demo reset. Omitting it does not stop the service
                # starting; it fails later, on the first listing, as a 500.
                "dynamodb:Scan",
                "dynamodb:DescribeTable",
            ],
            "Resource": [
                f"arn:aws:dynamodb:{args.region}:{account}:table/{args.table}",
                f"arn:aws:dynamodb:{args.region}:{account}:table/{args.table}/index/*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
            "Resource": f"arn:aws:s3:::{bucket}/*",
        },
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}"},
    ]
    if args.invoker == "agentcore":
        instance_statements.append(
            {
                "Effect": "Allow",
                "Action": "bedrock-agentcore:InvokeAgentRuntime",
                "Resource": [args.runtime_arn, f"{args.runtime_arn}/*"],
            }
        )
    elif not args.model_id.startswith("anthropic:"):
        instance_statements.append(
            {
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": "*",
            }
        )
    if args.delivery == "ses":
        instance_statements.append({"Effect": "Allow", "Action": "ses:SendEmail", "Resource": "*"})

    instance_arn = role_arn(
        iam,
        f"{NAME}-instance",
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Principal": {"Service": "tasks.apprunner.amazonaws.com"},
                }
            ],
        },
        {"Version": "2012-10-17", "Statement": instance_statements},
    )

    env = {
        "BIOINTAKE_BACKEND": "aws",
        "BIOINTAKE_DDB_TABLE": args.table,
        "BIOINTAKE_S3_BUCKET": bucket,
        "BIOINTAKE_INVOKER": args.invoker,
        "BIOINTAKE_MODEL_ID": args.model_id,
        "BIOINTAKE_DELIVERY": args.delivery,
    }
    for key, value in (
        ("BIOINTAKE_RUNTIME_ARN", args.runtime_arn),
        ("BIOINTAKE_PORTAL_BASE_URL", args.portal_base_url),
        ("BIOINTAKE_CORS_ORIGINS", args.cors_origins),
        ("BIOINTAKE_MAIL_FROM", args.mail_from),
        ("BIOINTAKE_DEMO_SIGN_IN", "1" if args.demo_sign_in else ""),
    ):
        if value:
            env[key] = value

    secret_env = {"BIOINTAKE_USERS": args.secret_users}
    if args.secret_anthropic:
        secret_env["ANTHROPIC_API_KEY"] = args.secret_anthropic

    source = {
        "ImageRepository": {
            "ImageIdentifier": image,
            "ImageRepositoryType": "ECR",
            "ImageConfiguration": {
                "Port": "8000",
                "RuntimeEnvironmentVariables": env,
                "RuntimeEnvironmentSecrets": secret_env,
            },
        },
        "AutoDeploymentsEnabled": False,
        "AuthenticationConfiguration": {"AccessRoleArn": access_arn},
    }
    health = {
        "Protocol": "HTTP",
        "Path": "/health",
        "Interval": 10,
        "Timeout": 5,
        "HealthyThreshold": 1,
        "UnhealthyThreshold": 5,
    }

    existing = next(
        (s for s in runner.list_services()["ServiceSummaryList"] if s["ServiceName"] == NAME), None
    )
    if existing:
        runner.update_service(
            ServiceArn=existing["ServiceArn"],
            SourceConfiguration=source,
            HealthCheckConfiguration=health,
            InstanceConfiguration={"InstanceRoleArn": instance_arn},
        )
        arn, url = existing["ServiceArn"], existing["ServiceUrl"]
        print(f"apprunner: updating {NAME}")
        _wait(runner, arn)
        # The image identifier does not change between builds; it is always :latest, so App Runner
        # sees no reason to pull, applies the new configuration to the old code, and reports success.
        # Asking for a deployment explicitly is what actually fetches what was just pushed.
        runner.start_deployment(ServiceArn=arn)
    else:
        created = runner.create_service(
            ServiceName=NAME,
            SourceConfiguration=source,
            HealthCheckConfiguration=health,
            InstanceConfiguration={"Cpu": "1 vCPU", "Memory": "2 GB", "InstanceRoleArn": instance_arn},
        )["Service"]
        arn, url = created["ServiceArn"], created["ServiceUrl"]
        print(f"apprunner: creating {NAME}")

    while True:
        status = runner.describe_service(ServiceArn=arn)["Service"]["Status"]
        print("  status:", status)
        if status in ("RUNNING", "CREATE_FAILED", "DELETE_FAILED", "OPERATION_IN_PROGRESS"):
            if status != "OPERATION_IN_PROGRESS":
                break
        time.sleep(15)

    print(f"\nhttps://{url}")
    print(f"health: curl https://{url}/health")
    if not args.portal_base_url:
        print(
            "\nEvidence-request links are relative. Re-run with --portal-base-url set to the origin "
            "the CONSOLE is served from (not this API, /portal is a console route) so a sending "
            "site can open the link from its email."
        )
    return 0 if status == "RUNNING" else 1


if __name__ == "__main__":
    raise SystemExit(main())
