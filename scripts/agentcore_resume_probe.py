"""Interrupt on microVM A → StopRuntimeSession → resume on microVM B.

    uv run python scripts/agentcore_resume_probe.py --profile biointake-hack --region us-east-1 \
        --runtime-arn arn:aws:bedrock-agentcore:...:runtime/... --bucket biointake-1b-hello-000000000000

Pass condition: boot_id differs between invocations; runtimeSessionId / Strands session / interrupt id are
identical; exactly one pending decision and one applied decision exist for the issue (S3 conditional PUTs).
"""

from __future__ import annotations

import argparse
import json
import time
import uuid

import boto3


def invoke(client, arn: str, session_id: str, payload: dict) -> dict:  # type: ignore[no-untyped-def]
    r = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=session_id,
        payload=json.dumps(payload).encode(),
        contentType="application/json",
        accept="application/json",
    )
    raw = r["response"].read() if hasattr(r.get("response"), "read") else r.get("response")
    text = raw.decode() if isinstance(raw, bytes | bytearray) else str(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:  # SSE-style or wrapped
        chunks = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
        return json.loads(chunks[-1]) if chunks else {"raw": text[:500]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--runtime-arn", required=True)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--prefix", default="hello-sessions/")
    ap.add_argument("--settle-seconds", type=int, default=25)
    args = ap.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client("bedrock-agentcore")
    s3 = session.client("s3")
    runtime_session_id = f"probe-{uuid.uuid4()}-{uuid.uuid4().hex[:8]}"  # comfortably ≥ 33 chars

    t0 = time.time()
    first = invoke(
        client,
        args.runtime_arn,
        runtime_session_id,
        {"prompt": "Process sample BX-212; it has a temperature excursion."},
    )
    print(
        f"[A] {time.time() - t0:.1f}s",
        json.dumps(
            {
                k: first.get(k)
                for k in (
                    "boot_id",
                    "pid",
                    "runtime_session_id",
                    "strands_session_id",
                    "model",
                    "stop_reason",
                    "pending_decisions",
                    "applied_decisions",
                )
            }
        ),
    )
    assert first.get("stop_reason") == "interrupt", first
    interrupt_id = first["interrupts"][0]["id"]
    issue_id = first["interrupts"][0]["reason"]["issue_id"]
    print(f"[A] interrupt {interrupt_id}")

    client.stop_runtime_session(agentRuntimeArn=args.runtime_arn, runtimeSessionId=runtime_session_id)
    print(
        f"[stop] StopRuntimeSession issued; settling {args.settle_seconds}s so the next invocation lands on a fresh microVM"
    )
    time.sleep(args.settle_seconds)

    t1 = time.time()
    second = invoke(
        client,
        args.runtime_arn,
        runtime_session_id,
        {"interrupt_responses": [{"interruptId": interrupt_id, "response": "QUARANTINE"}]},
    )
    print(
        f"[B] {time.time() - t1:.1f}s",
        json.dumps(
            {
                k: second.get(k)
                for k in (
                    "boot_id",
                    "pid",
                    "runtime_session_id",
                    "strands_session_id",
                    "model",
                    "stop_reason",
                    "pending_decisions",
                    "applied_decisions",
                )
            }
        ),
    )
    print(f"[B] text: {second.get('text', '')[:200]}")

    pending_s3 = s3.list_objects_v2(Bucket=args.bucket, Prefix=f"{args.prefix}pending/{issue_id}").get(
        "KeyCount", 0
    )
    applied_s3 = s3.list_objects_v2(Bucket=args.bucket, Prefix=f"{args.prefix}decisions/{issue_id}").get(
        "KeyCount", 0
    )
    card = {
        "boot_id_A": first.get("boot_id"),
        "boot_id_B": second.get("boot_id"),
        "different_process": first.get("boot_id") != second.get("boot_id"),
        "same_runtime_session": first.get("runtime_session_id")
        == second.get("runtime_session_id")
        == runtime_session_id,
        "same_strands_session": first.get("strands_session_id") == second.get("strands_session_id"),
        "interrupt_id": interrupt_id,
        "resumed_stop_reason": second.get("stop_reason"),
        "pending_decisions": pending_s3,
        "applied_decisions": applied_s3,
    }
    print(json.dumps(card, indent=2))
    ok = (
        card["different_process"]
        and card["same_runtime_session"]
        and card["same_strands_session"]
        and second.get("stop_reason") == "end_turn"
        and pending_s3 == 1
        and applied_s3 == 1
    )
    print("CRITICAL GATE", "PASSED ✔" if ok else "FAILED ✘")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
