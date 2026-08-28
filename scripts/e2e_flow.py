"""End-to-end product flow through the control API. Exit conditions are asserted.

    uv run python scripts/e2e_flow.py --backend memory --invoker local        # everything in-process
    uv run python scripts/e2e_flow.py --backend aws --invoker local           # DynamoDB + S3 + S3 sessions, agent in-process
    uv run python scripts/e2e_flow.py --backend aws --invoker agentcore \\
        --runtime-arn ... [--stop-session-before-decision]                    # + deployed AgentCore runtime
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from biointake.api.app import create_app, staff_users_spec  # noqa: E402
from biointake.api.config import Settings  # noqa: E402
from biointake.fixtures import DEFAULT_FIXTURE_DIR, load_package  # noqa: E402

# Filled in once the app exists: the script mints the lab's credentials and then signs in with one,
# exactly as a coordinator would. There is no way in that does not carry a token.
COORDINATOR: dict[str, str] = {}
CONTROL: dict[str, str] = {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["memory", "aws"], default="memory")
    ap.add_argument("--invoker", choices=["local", "agentcore"], default="local")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--table", default="biointake-demo")
    ap.add_argument("--bucket", default="")
    ap.add_argument("--runtime-arn", default="")
    ap.add_argument(
        "--stop-session-before-decision",
        action="store_true",
        help="StopRuntimeSession before the human decision so the interrupt must resume on a fresh microVM",
    )
    args = ap.parse_args()
    users_spec, tokens = staff_users_spec()
    COORDINATOR.update({"Authorization": f"Bearer {tokens['coordinator-ama-asante']}"})
    CONTROL.update({"Authorization": f"Bearer {tokens['control-plane']}"})
    settings = Settings(
        users_spec=users_spec,
        backend=args.backend,
        invoker=args.invoker,
        profile=args.profile,
        region=args.region,
        ddb_table=args.table,
        s3_bucket=args.bucket,
        runtime_arn=args.runtime_arn,
        session_dir=Path(".local/e2e-sessions"),
    )
    client = TestClient(create_app(settings), headers=COORDINATOR)
    package = load_package(DEFAULT_FIXTURE_DIR)
    t0 = time.time()

    def step(name: str, resp) -> dict:  # type: ignore[no-untyped-def]
        print(f"[{time.time() - t0:6.1f}s] {name}: HTTP {resp.status_code}")
        assert resp.status_code < 300, resp.text[:500]
        return resp.json()

    def brief(r: dict, *keys: str) -> dict:
        out = {k: r[k] for k in keys}
        out["boot"] = r["boot_id"][:8]
        return out

    step("reset", client.post("/api/demo/reset", headers=COORDINATOR))
    loaded = step("load", client.post("/api/demo/load", headers=COORDINATOR))
    case_id = loaded["case_id"]

    r1 = step(
        "run CASE_READY",
        client.post(f"/api/cases/{case_id}/run", json={"event_type": "CASE_READY"}, headers=CONTROL),
    )
    print(
        "   ",
        brief(
            r1,
            "stable_state",
            "stop_reason",
            "tool_attempt_count",
            "checks_evaluated",
            "created_evidence_request_ids",
            "intervention_denials",
        ),
    )
    accepted1 = sorted(k for k, v in r1["committed_dispositions"].items() if v == "ACCEPTED")
    assert r1["stable_state"] == "WAITING_FOR_EVIDENCE" and len(accepted1) == 7, r1
    assert (
        r1["committed_dispositions"].get("BX-211") == "QUARANTINED"
        and len(r1["created_evidence_request_ids"]) == 1
    ), r1
    rid = r1["created_evidence_request_ids"][0]

    req = step("get evidence request", client.get(f"/api/evidence-requests/{rid}"))
    print(
        "    →",
        req["recipient"],
        "|",
        req["subject"],
        "|",
        [q["requirement_type"] + ":" + q["sample_id"] for q in req["requirements"]],
    )
    token = client.app.state.biointake.services.repo.get_request(
        rid
    ).upload_token  # the sender's link carries this
    reply = json.loads(package.later["sender-reply.json"])
    body = {
        "upload_token": token,
        "submitted_by_contact_id": reply["from_contact_id"],
        "sender_message": reply["free_text"],
        "files": [
            {
                "filename": "consent-addendum.json",
                "mime_type": "application/json",
                "content_base64": base64.b64encode(package.later["consent-addendum.json"]).decode(),
            }
        ],
    }
    r2 = step("complete evidence request", client.post(f"/api/evidence-requests/{rid}/complete", json=body))
    print("   ", brief(r2, "stable_state", "stop_reason", "checks_reverified", "tool_attempt_count"))
    assert (
        r2["stable_state"] == "NEEDS_HUMAN_DECISION"
        and r2["stop_reason"] == "interrupt"
        and r2["checks_reverified"] == 4
    ), r2
    interrupt_id = r2["pending_interrupt"]["interrupt_id"]

    dup = step(
        "duplicate evidence delivery", client.post(f"/api/evidence-requests/{rid}/complete", json=body)
    )
    assert dup["stable_state"] == "NEEDS_HUMAN_DECISION", dup

    decisions = step("decision cards", client.get(f"/api/cases/{case_id}/decisions"))
    assert decisions and decisions[0]["interrupt_id"] == interrupt_id
    if args.stop_session_before_decision:
        import boto3

        boto3.Session(profile_name=args.profile, region_name=args.region).client(
            "bedrock-agentcore"
        ).stop_runtime_session(agentRuntimeArn=args.runtime_arn, runtimeSessionId=loaded["session_id"])
        print(
            f"[{time.time() - t0:6.1f}s] StopRuntimeSession issued → the decision must resume on a fresh microVM; settling 25s"
        )
        time.sleep(25)

    r3 = step(
        "respond QUARANTINE (coordinator)",
        client.post(
            f"/api/cases/{case_id}/interrupts/{interrupt_id}/respond",
            json={"selected_option": "QUARANTINE", "comment": "hold pending PI review"},
            headers=COORDINATOR,
        ),
    )
    print("   ", brief(r3, "stable_state", "stop_reason", "tool_attempt_count"))
    assert r3["stable_state"] == "COMPLETED", r3
    fresh = args.stop_session_before_decision and r3["boot_id"] != r2["boot_id"]
    if args.stop_session_before_decision:
        assert fresh, "expected the decision to be processed by a fresh runtime process"
        print(
            f"    fresh microVM confirmed: boot {r2['boot_id'][:8]}… (interrupt) → {r3['boot_id'][:8]}… (resume)"
        )

    final = step("final case", client.get(f"/api/cases/{case_id}"))
    rep = final["report"]
    events = client.get(f"/api/cases/{case_id}/events").json()["events"]
    domain_types = [e["event_type"] for e in events if e["kind"] == "DOMAIN_EFFECT"]
    card = {
        "accepted": rep["counts"]["ACCEPTED"],
        "quarantined": rep["counts"]["QUARANTINED"],
        "evidence_requests": len(rep["evidence_requests"]),
        "human_decisions": len(rep["human_decisions"]),
        "unauthorized_acceptances": rep["unauthorized_acceptances"],
        "pending_decision_created": domain_types.count("PENDING_DECISION_CREATED"),
        "human_decision_applied": domain_types.count("HUMAN_DECISION_APPLIED"),
        "evidence_request_sent": domain_types.count("EVIDENCE_REQUEST_SENT"),
        "lims_writes": domain_types.count("LIMS_WRITE"),
        "audit_events": len(events),
        "backend": args.backend,
        "invoker": args.invoker,
        "boot_ids": [r1["boot_id"][:8], r2["boot_id"][:8], r3["boot_id"][:8]],
        "fresh_runtime_for_decision": fresh,
        "elapsed_s": round(time.time() - t0, 1),
    }
    print(json.dumps(card, indent=2))
    ok = (
        card["accepted"] == 10
        and card["quarantined"] == 2
        and card["evidence_requests"] == 1
        and card["human_decisions"] == 1
        and card["unauthorized_acceptances"] == 0
        and card["pending_decision_created"] == 1
        and card["human_decision_applied"] == 1
        and card["evidence_request_sent"] == 1
        and card["lims_writes"] == 12
    )
    print("E2E", "PASSED ✔" if ok else "FAILED ✘")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
