"""AWS readiness gate: proves the primitives by USING them, never by listing permissions.

    uv run python scripts/aws_readiness.py --profile biointake-hack --region us-east-1

Every check is disposable (temporary bucket/table with a random suffix, cleaned up afterwards).
Nothing here deploys BioIntake. Output: a pass/fail card + .local/aws-readiness.json.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

BLOCKED_ACCOUNT = "866733613374"  # old identity with llm_eval_boundary_policy, must NOT be used
ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Check:
    gate: str
    status: str  # PASS | FAIL | BLOCKED | SKIPPED
    detail: str = ""


def run_check(results: list[Check], gate: str, fn):  # type: ignore[no-untyped-def]
    t0 = time.time()
    try:
        detail = fn()
        results.append(Check(gate, "PASS", f"{detail} ({time.time() - t0:.1f}s)"))
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        msg = e.response.get("Error", {}).get("Message", str(e))
        status = (
            "BLOCKED"
            if code
            in (
                "AccessDeniedException",
                "AccessDenied",
                "UnauthorizedOperation",
                "UnrecognizedClientException",
            )
            else "FAIL"
        )
        results.append(Check(gate, status, f"{code}: {msg[:300]}"))
    except Exception as e:  # noqa: BLE001
        results.append(Check(gate, "FAIL", f"{type(e).__name__}: {str(e)[:300]}"))
    print(f"  [{results[-1].status:<7}] {gate}: {results[-1].detail[:160]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--skip-anthropic", action="store_true")
    args = ap.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    results: list[Check] = []
    suffix = uuid.uuid4().hex[:8]
    print(f"AWS readiness, profile={args.profile} region={args.region}")

    # 1 identity ---------------------------------------------------------------------------------
    identity: dict[str, str] = {}

    def check_identity() -> str:
        ident = session.client("sts").get_caller_identity()
        identity.update(ident)
        if ident["Account"] == BLOCKED_ACCOUNT:
            raise RuntimeError(f"profile resolves to the blocked account {BLOCKED_ACCOUNT}")
        arn = ident["Arn"]
        if ":root" in arn:
            raise RuntimeError("root credentials in use, configure an SSO/IAM user profile instead")
        return f"account {ident['Account']} as {arn}; region {session.region_name}"

    run_check(results, "New AWS identity confirmed (not root, not the blocked account)", check_identity)

    # 2 bedrock: a REAL invocation ---------------------------------------------------------------
    def check_bedrock_amazon() -> str:
        rt = session.client("bedrock-runtime")
        last = None
        for model_id in (
            "amazon.nova-micro-v1:0",
            "us.amazon.nova-micro-v1:0",
            "amazon.nova-lite-v1:0",
            "us.amazon.nova-lite-v1:0",
        ):
            try:
                t0 = time.time()
                r = rt.converse(
                    modelId=model_id,
                    messages=[{"role": "user", "content": [{"text": "Reply with the single word READY."}]}],
                    inferenceConfig={"maxTokens": 10},
                )
                text = r["output"]["message"]["content"][0]["text"].strip()
                usage = r.get("usage", {})
                return f"{model_id} → {text!r} in {time.time() - t0:.2f}s; tokens in/out {usage.get('inputTokens')}/{usage.get('outputTokens')}"
            except ClientError as e:
                last = e
        raise last or RuntimeError("no Amazon model responded")

    run_check(results, "Real Bedrock invocation (Amazon Nova, converse)", check_bedrock_amazon)

    if not args.skip_anthropic:

        def check_bedrock_anthropic() -> str:
            rt = session.client("bedrock-runtime")
            last = None
            for model_id in (
                "us.anthropic.claude-3-5-haiku-20241022-v1:0",
                "anthropic.claude-3-5-haiku-20241022-v1:0",
                "us.anthropic.claude-sonnet-4-20250514-v1:0",
            ):
                try:
                    r = rt.converse(
                        modelId=model_id,
                        messages=[
                            {"role": "user", "content": [{"text": "Reply with the single word READY."}]}
                        ],
                        inferenceConfig={"maxTokens": 10},
                    )
                    return f"{model_id} → {r['output']['message']['content'][0]['text'].strip()!r}"
                except ClientError as e:
                    last = e
            raise last or RuntimeError("no Anthropic model responded")

        run_check(
            results,
            "Bedrock Anthropic invocation (informational; may need the one-time use-case form)",
            check_bedrock_anthropic,
        )

    # 3 s3 CRUD + Strands S3SessionManager spike -----------------------------------------------
    bucket = f"biointake-1b-{identity.get('Account', 'x')}-{suffix}".lower()
    s3 = session.client("s3")

    def check_s3() -> str:
        if args.region == "us-east-1":
            s3.create_bucket(Bucket=bucket)
        else:
            s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": args.region})
        s3.put_object(Bucket=bucket, Key="probe/hello.txt", Body=b"hello")
        body = s3.get_object(Bucket=bucket, Key="probe/hello.txt")["Body"].read()
        assert body == b"hello"
        keys = [o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix="probe/").get("Contents", [])]
        assert keys == ["probe/hello.txt"]
        s3.delete_object(Bucket=bucket, Key="probe/hello.txt")
        return f"bucket {bucket}: put/get/list/delete ok"

    run_check(results, "S3 CRUD", check_s3)

    def check_s3_session_spike() -> str:
        env = {
            **os.environ,
            "AWS_PROFILE": args.profile,
            "AWS_DEFAULT_REGION": args.region,
            "BIOINTAKE_S3_BUCKET": bucket,
            "BIOINTAKE_S3_PREFIX": "spike-sessions/",
        }
        spike = ROOT / "spikes" / "interrupt_resume_s3.py"
        out1 = subprocess.run(
            [sys.executable, str(spike), "start"],
            capture_output=True,
            text=True,
            env=env,
            cwd=ROOT,
            timeout=180,
        )
        out2 = subprocess.run(
            [sys.executable, str(spike), "resume"],
            capture_output=True,
            text=True,
            env=env,
            cwd=ROOT,
            timeout=180,
        )
        if out1.returncode or out2.returncode:
            raise RuntimeError((out1.stderr or out1.stdout)[-400:] + (out2.stderr or out2.stdout)[-400:])
        line = next((ln for ln in out2.stdout.splitlines() if "tool_attempts=" in ln), "")
        if "pending_decision_records=1" not in line or "decisions_applied=1" not in line:
            raise RuntimeError(f"unexpected spike output: {out2.stdout[-400:]}")
        return line.strip()

    run_check(results, "S3SessionManager interrupt resume across processes", check_s3_session_spike)

    def cleanup_bucket() -> str:
        paginator = s3.get_paginator("list_objects_v2")
        n = 0
        for page in paginator.paginate(Bucket=bucket):
            for o in page.get("Contents", []):
                s3.delete_object(Bucket=bucket, Key=o["Key"])
                n += 1
        s3.delete_bucket(Bucket=bucket)
        return f"deleted {n} objects and bucket {bucket}"

    run_check(results, "S3 cleanup (delete objects + bucket)", cleanup_bucket)

    # 4 dynamodb CRUD + conditional lease --------------------------------------------------------
    table = f"biointake-1b-lease-{suffix}"
    ddb = session.client("dynamodb")

    def check_ddb() -> str:
        ddb.create_table(
            TableName=table,
            AttributeDefinitions=[{"AttributeName": "case_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "case_id", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.get_waiter("table_exists").wait(TableName=table)
        ddb.put_item(TableName=table, Item={"case_id": {"S": "CASE-1"}, "case_version": {"N": "0"}})
        now = datetime.now(UTC)
        expires = (now + timedelta(minutes=5)).isoformat()
        acquire = dict(
            TableName=table,
            Key={"case_id": {"S": "CASE-1"}},
            UpdateExpression="SET lease_owner = :o, lease_expires_at = :e",
            ConditionExpression="attribute_not_exists(lease_owner) OR lease_expires_at < :now",
            ExpressionAttributeValues={
                ":o": {"S": "process-A"},
                ":e": {"S": expires},
                ":now": {"S": now.isoformat()},
            },
        )
        ddb.update_item(**acquire)  # process A acquires
        try:
            acquire_b = {
                **acquire,
                "ExpressionAttributeValues": {
                    **acquire["ExpressionAttributeValues"],
                    ":o": {"S": "process-B"},
                },
            }
            ddb.update_item(**acquire_b)
            raise RuntimeError(
                "process B acquired an already-held lease, conditional write did not protect it"
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        item = ddb.get_item(TableName=table, Key={"case_id": {"S": "CASE-1"}})["Item"]
        assert item["lease_owner"]["S"] == "process-A"
        # conditional version bump (optimistic concurrency)
        ddb.update_item(
            TableName=table,
            Key={"case_id": {"S": "CASE-1"}},
            UpdateExpression="SET case_version = case_version + :one",
            ConditionExpression="case_version = :v",
            ExpressionAttributeValues={":one": {"N": "1"}, ":v": {"N": "0"}},
        )
        try:
            ddb.update_item(
                TableName=table,
                Key={"case_id": {"S": "CASE-1"}},
                UpdateExpression="SET case_version = case_version + :one",
                ConditionExpression="case_version = :v",
                ExpressionAttributeValues={":one": {"N": "1"}, ":v": {"N": "0"}},
            )
            raise RuntimeError("stale version accepted")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        ddb.delete_item(TableName=table, Key={"case_id": {"S": "CASE-1"}})
        return "create/put/lease A ok/lease B refused/version bump ok/stale refused/get/delete ok"

    run_check(results, "DynamoDB CRUD + conditional lease + optimistic version", check_ddb)

    def cleanup_table() -> str:
        ddb.delete_table(TableName=table)
        return f"deleted table {table}"

    run_check(results, "DynamoDB cleanup", cleanup_table)

    # 5 agentcore CLI + CDK bootstrap ------------------------------------------------------------
    def check_cli() -> str:
        out = subprocess.run(
            ["npx", "-y", "@aws/agentcore", "--version"], capture_output=True, text=True, timeout=300
        )
        if out.returncode:
            raise RuntimeError(out.stderr[-300:] or out.stdout[-300:])
        return f"@aws/agentcore {out.stdout.strip().splitlines()[-1]}"

    run_check(results, "AgentCore CLI (npm @aws/agentcore) available", check_cli)

    def check_cdk_bootstrap() -> str:
        cf = session.client("cloudformation")
        stacks = cf.describe_stacks(StackName="CDKToolkit")["Stacks"]
        return f"CDKToolkit stack {stacks[0]['StackStatus']}"

    run_check(results, "CDK bootstrap present (needed by `agentcore deploy`)", check_cdk_bootstrap)

    def check_agentcore_control() -> str:
        c = session.client("bedrock-agentcore-control")
        n = len(c.list_agent_runtimes(maxResults=5).get("agentRuntimes", []))
        return f"control plane reachable; {n} runtime(s) listed"

    run_check(results, "AgentCore control plane reachable", check_agentcore_control)

    # card ---------------------------------------------------------------------------------------
    manual = [
        Check("agentcore dev works (hello agent)", "MANUAL", "run it and invoke the runtime"),
        Check("agentcore deploy --dry-run", "MANUAL", "run it against deploy/biointakeruntime"),
        Check("Minimal AgentCore deployment + deployed invocation", "MANUAL", "deploy, then invoke it"),
        Check(
            "Interrupt survives fresh runtime process (boot_id differs)",
            "MANUAL",
            "scripts/agentcore_resume_probe.py",
        ),
    ]
    print("\n| Gate | Status | Detail |\n|---|---|---|")
    for c in results + manual:
        print(f"| {c.gate} | {c.status} | {c.detail[:140].replace('|', '/')} |")
    out = ROOT / ".local" / "aws-readiness.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "run_at": datetime.now(UTC).isoformat(),
                "profile": args.profile,
                "region": args.region,
                "identity": {k: identity.get(k) for k in ("Account", "Arn")},
                "checks": [asdict(c) for c in results + manual],
            },
            indent=2,
        )
    )
    print(f"\nwrote {out}")
    return 0 if all(c.status == "PASS" for c in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
