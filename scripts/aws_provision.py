"""Provision the (deliberately minimal) BioIntake demo infrastructure: one DynamoDB table + one S3 bucket.

uv run python scripts/aws_provision.py --profile biointake-hack --region us-east-1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from biointake.repositories.dynamodb import ensure_table  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--table", default="biointake-demo")
    ap.add_argument("--bucket", default=None)
    args = ap.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    bucket = args.bucket or f"biointake-demo-{account}"
    print("table:", args.table, ensure_table(args.table, session))
    s3 = session.client("s3")
    try:
        s3.head_bucket(Bucket=bucket)
        print("bucket:", bucket, "exists")
    except s3.exceptions.ClientError:
        if args.region == "us-east-1":
            s3.create_bucket(Bucket=bucket)
        else:
            s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": args.region})
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        print("bucket:", bucket, "created")
    print(
        f"\nexport BIOINTAKE_BACKEND=aws BIOINTAKE_AWS_PROFILE={args.profile} AWS_REGION={args.region} BIOINTAKE_DDB_TABLE={args.table} BIOINTAKE_S3_BUCKET={bucket}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
