"""Local three-invocation agent demonstration through the REAL Strands loop with the offline stand-in.

Invocation 1 (CASE_READY)            → WAITING_FOR_EVIDENCE
Invocation 2 (EVIDENCE_RECEIVED)     → NEEDS_HUMAN_DECISION (Strands interrupt raised)
Invocation 3 (HUMAN_DECISION_RECEIVED, fresh Agent object) → COMPLETED (10 accepted / 2 quarantined)
"""

from __future__ import annotations

import json
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strands.session import FileSessionManager  # noqa: E402

from biointake.agent.events import EvidenceDelivery, InvocationEvent  # noqa: E402
from biointake.agent.runtime import run_event  # noqa: E402
from biointake.agent.testing import StandInModel, canonical_policy  # noqa: E402
from biointake.clock import SteppingClock  # noqa: E402
from biointake.domain.commands import IncomingArtifact  # noqa: E402
from biointake.domain.enums import ActorRole, InvocationEventType  # noqa: E402
from biointake.domain.models import ActorContext  # noqa: E402
from biointake.fixtures import DEFAULT_FIXTURE_DIR, load_package  # noqa: E402
from biointake.repositories.memory import InMemoryRepository  # noqa: E402
from biointake.services.intake import IntakeService  # noqa: E402
from biointake.storage.local import MemoryArtifactStorage  # noqa: E402

SESSIONS = Path(__file__).resolve().parents[1] / ".local" / "agent-sessions"


def main() -> int:
    package = load_package(DEFAULT_FIXTURE_DIR)
    clock = SteppingClock(datetime(2026, 8, 26, 16, 0, tzinfo=UTC))
    svc = IntakeService(
        InMemoryRepository(clock),
        MemoryArtifactStorage(),
        package.policy,
        clock,
        token_factory=lambda: "demo-upload-token-0001",
    )
    case = svc.create_case(package, ActorContext.system())
    svc.begin_verification(case.case_id, ActorContext.system())
    session_id = case.agent_session_id
    shutil.rmtree(SESSIONS, ignore_errors=True)

    def sm() -> FileSessionManager:
        return FileSessionManager(session_id=session_id, storage_dir=str(SESSIONS))

    def show(title: str, r) -> None:  # type: ignore[no-untyped-def]
        print("=" * 78 + f"\n{title}\n" + "=" * 78)
        print(json.dumps(r.model_dump(mode="json", exclude={"final_text"}), indent=2))
        if r.final_text:
            print("final text:", r.final_text)

    # --- 1: shipment arrives
    r1 = run_event(
        InvocationEvent(
            case_id=case.case_id,
            event_id="EVT-CASE-READY-1",
            event_type=InvocationEventType.CASE_READY,
            trusted_actor_id="control-plane",
            trusted_actor_role=ActorRole.SYSTEM,
            session_id=session_id,
            trace_id=str(uuid.uuid4()),
        ),
        svc,
        StandInModel(canonical_policy),
        sm(),
    )
    show("INVOCATION 1, CASE_READY", r1)
    assert r1.stable_state.value == "WAITING_FOR_EVIDENCE", r1.stable_state

    # --- 2: sender replies (control layer stages the upload + free text)
    request = svc.repo.list_requests(case.case_id)[0]
    reply = json.loads(package.later["sender-reply.json"])
    r2 = run_event(
        InvocationEvent(
            case_id=case.case_id,
            event_id="EVT-EVIDENCE-1",
            event_type=InvocationEventType.EVIDENCE_RECEIVED,
            trusted_actor_id=reply["from_contact_id"],
            trusted_actor_role=ActorRole.SITE_CONTACT,
            session_id=session_id,
            evidence=EvidenceDelivery(
                request_id=request.request_id,
                upload_token=request.upload_token,
                submitted_by_contact_id=reply["from_contact_id"],
                artifacts=(
                    IncomingArtifact(
                        filename="consent-addendum.json",
                        mime_type="application/json",
                        content=package.later["consent-addendum.json"],
                    ),
                ),
                sender_message=reply["free_text"],
            ),
        ),
        svc,
        StandInModel(canonical_policy),
        sm(),
    )
    show("INVOCATION 2, EVIDENCE_RECEIVED", r2)
    assert (
        r2.stop_reason == "interrupt"
        and r2.pending_interrupt is not None
        and r2.stable_state.value == "NEEDS_HUMAN_DECISION"
    )

    # --- 3: coordinator decides; a FRESH Agent object is built from the persisted session
    r3 = run_event(
        InvocationEvent(
            case_id=case.case_id,
            event_id="EVT-HUMAN-1",
            event_type=InvocationEventType.HUMAN_DECISION_RECEIVED,
            trusted_actor_id="coordinator-ama-asante",
            trusted_actor_role=ActorRole.COORDINATOR,
            session_id=session_id,
            interrupt_responses=(
                {
                    "interruptId": r2.pending_interrupt.interrupt_id,
                    "response": {
                        "selected_option": "QUARANTINE",
                        "comment": "19-minute excursion; hold pending PI review.",
                        "actor_role": "PRINCIPAL_INVESTIGATOR",
                    },
                },
            ),
        ),
        svc,
        StandInModel(canonical_policy),
        sm(),
    )
    show("INVOCATION 3, HUMAN_DECISION_RECEIVED (fresh agent, restored interrupt)", r3)
    report = svc.build_report(case.case_id)
    print(
        "\nFINAL:",
        {k: report["counts"][k] for k in ("ACCEPTED", "QUARANTINED", "ACCEPTED_WITH_EXCEPTION")},
        "unauthorized:",
        report["unauthorized_acceptances"],
    )
    print("audit counts by kind:", report["audit_counts_by_kind"])
    ok = (
        report["counts"]["ACCEPTED"] == 10
        and report["counts"]["QUARANTINED"] == 2
        and report["unauthorized_acceptances"] == 0
        and r3.stable_state.value == "COMPLETED"
    )
    print("AGENT DEMO", "PASSED ✔" if ok else "FAILED ✘")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
