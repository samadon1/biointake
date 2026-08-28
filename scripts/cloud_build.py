"""Build a container image on AWS instead of on this machine, the `gcloud builds submit` shape.

    uv run python scripts/cloud_build.py --profile biointake-hack \
        --dockerfile deploy/web/Dockerfile --repository biointake-web \
        --include web deploy/web \
        --build-arg NEXT_PUBLIC_API_BASE=https://example.awsapprunner.com

It zips the paths you name, puts the zip in S3, and has CodeBuild build and push to ECR. Nothing is
built locally, which matters on a machine that runs out of disk part-way through a Node build and
corrupts its own Docker store doing it. It also needs no git remote, because the source is a zip
rather than a repository connection.

CodeBuild's standard image is already linux/amd64 and builds with plain docker, so the image it
pushes is an ordinary Docker v2 manifest, the thing App Runner needs, without the buildx
incantation that produces it locally.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import zipfile
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    ".next",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "cdk.out",
    "__pycache__",
    ".local",
}
BUILD_IMAGE = "aws/codebuild/standard:7.0"


def make_zip(includes: list[str], out: Path) -> int:
    """Bundle only the paths the Dockerfile actually reaches for.

    Deliberately explicit rather than "everything except": a build context is a thing you should
    have to choose, and zipping the whole tree here would carry 200 MB of unrelated CDK output.
    """
    written = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for include in includes:
            source = ROOT / include
            if not source.exists():
                raise SystemExit(f"--include {include} does not exist")
            if source.is_file():
                z.write(source, source.relative_to(ROOT))
                written += 1
                continue
            for path, dirs, names in os.walk(source):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for name in names:
                    file = Path(path, name)
                    z.write(file, file.relative_to(ROOT))
                    written += 1
    return written


def buildspec(registry: str, repository: str, tag: str, dockerfile: str, args: list[str]) -> str:
    build_args = " ".join(f'--build-arg "{a}"' for a in args)
    return json.dumps(
        {
            "version": "0.2",
            "phases": {
                "pre_build": {
                    "commands": [
                        "aws ecr get-login-password --region $AWS_DEFAULT_REGION | "
                        f"docker login --username AWS --password-stdin {registry}"
                    ]
                },
                "build": {
                    "commands": [
                        f"docker build -f {dockerfile} {build_args} -t {registry}/{repository}:{tag} ."
                    ]
                },
                "post_build": {"commands": [f"docker push {registry}/{repository}:{tag}"]},
            },
        }
    )


def ensure_role(iam, name: str, bucket: str, region: str, account: str) -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "codebuild.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                # Both forms: the log group for CreateLogGroup, and its :log-stream:* child for
                # the other two. Granting only the group fails the build before it starts, in
                # QUEUED, with no logs to read, because writing the logs is what was refused.
                "Resource": [
                    f"arn:aws:logs:{region}:{account}:log-group:/aws/codebuild/*",
                    f"arn:aws:logs:{region}:{account}:log-group:/aws/codebuild/*:*",
                ],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                "Resource": f"arn:aws:s3:::{bucket}/*",
            },
            {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
            {
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:CompleteLayerUpload",
                    "ecr:InitiateLayerUpload",
                    "ecr:PutImage",
                    "ecr:UploadLayerPart",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                ],
                "Resource": f"arn:aws:ecr:{region}:{account}:repository/*",
            },
        ],
    }
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Builds BioIntake container images",
        )["Role"]["Arn"]
        print(f"iam: created {name}")
        time.sleep(10)  # CodeBuild rejects a role it cannot yet see
    iam.put_role_policy(RoleName=name, PolicyName=f"{name}-inline", PolicyDocument=json.dumps(policy))
    return str(arn)


def ensure_project(cb, name: str, role: str, bucket: str, key: str, spec: str) -> None:
    config = {
        "source": {"type": "S3", "location": f"{bucket}/{key}", "buildspec": spec},
        "artifacts": {"type": "NO_ARTIFACTS"},
        "environment": {
            "type": "LINUX_CONTAINER",
            "image": BUILD_IMAGE,
            "computeType": "BUILD_GENERAL1_MEDIUM",
            "privilegedMode": True,  # required to run a Docker daemon inside the build
        },
        "serviceRole": role,
    }
    existing = cb.batch_get_projects(names=[name])["projects"]
    if existing:
        cb.update_project(name=name, **config)
    else:
        cb.create_project(name=name, **config)
        print(f"codebuild: created {name}")


def tail(logs, group: str, stream: str) -> None:
    token = None
    while True:
        kwargs = {"logGroupName": group, "logStreamName": stream, "startFromHead": True}
        if token:
            kwargs["nextToken"] = token
        r = logs.get_log_events(**kwargs)
        for e in r["events"]:
            print("   ", e["message"].rstrip())
        if r["nextForwardToken"] == token:
            return
        token = r["nextForwardToken"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--bucket", default=None, help="defaults to biointake-demo-<account>")
    ap.add_argument("--dockerfile", required=True, help="path from the repository root")
    ap.add_argument("--repository", required=True, help="ECR repository name")
    ap.add_argument("--tag", default="latest")
    ap.add_argument("--include", nargs="+", required=True, help="paths to put in the build context")
    ap.add_argument("--build-arg", action="append", default=[], dest="build_args")
    ap.add_argument("--project", default=None, help="defaults to codebuild-<repository>")
    args = ap.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    bucket = args.bucket or f"biointake-demo-{account}"
    project = args.project or f"codebuild-{args.repository}"
    registry = f"{account}.dkr.ecr.{args.region}.amazonaws.com"

    ecr = session.client("ecr")
    try:
        ecr.describe_repositories(repositoryNames=[args.repository])
    except ecr.exceptions.RepositoryNotFoundException:
        ecr.create_repository(repositoryName=args.repository)
        print(f"ecr: created {args.repository}")

    zip_path = ROOT / ".local" / f"{project}-source.zip"
    zip_path.parent.mkdir(exist_ok=True)
    count = make_zip(args.include, zip_path)
    size = zip_path.stat().st_size
    print(f"source: {count} files, {size / 1e6:.1f} MB from {', '.join(args.include)}")

    key = f"codebuild/{project}-source.zip"
    session.client("s3").upload_file(str(zip_path), bucket, key)
    print(f"uploaded: s3://{bucket}/{key}")

    role = ensure_role(session.client("iam"), f"{project}-role", bucket, args.region, account)
    spec = buildspec(registry, args.repository, args.tag, args.dockerfile, args.build_args)
    cb = session.client("codebuild")
    ensure_project(cb, project, role, bucket, key, spec)

    build_id = cb.start_build(projectName=project)["build"]["id"]
    print(f"building: {build_id}")
    while True:
        build = cb.batch_get_builds(ids=[build_id])["builds"][0]
        if build["buildStatus"] != "IN_PROGRESS":
            break
        print("  ", build.get("currentPhase", "…"))
        time.sleep(15)

    status = build["buildStatus"]
    group = build.get("logs", {}).get("groupName")
    stream = build.get("logs", {}).get("streamName")
    if status != "SUCCEEDED" and group and stream:
        print("\n--- build log ---")
        tail(session.client("logs"), group, stream)
    print(f"\n{status}: {registry}/{args.repository}:{args.tag}")
    if build.get("logs", {}).get("deepLink"):
        print(build["logs"]["deepLink"])
    return 0 if status == "SUCCEEDED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
