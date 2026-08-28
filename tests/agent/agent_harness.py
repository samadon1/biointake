from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from strands.session import FileSessionManager

from biointake.agent.events import EvidenceDelivery, InvocationEvent, RunResult
from biointake.agent.runtime import run_event
from biointake.agent.testing import Policy, StandInModel, canonical_policy
from biointake.clock import SteppingClock
from biointake.domain.commands import IncomingArtifact
from biointake.domain.enums import ActorRole, InvocationEventType
from biointake.domain.models import ActorContext
from biointake.fixtures import DEFAULT_FIXTURE_DIR, load_package
from biointake.repositories.memory import InMemoryRepository
from biointake.services.intake import IntakeService
from biointake.storage.local import MemoryArtifactStorage


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.package = load_package(DEFAULT_FIXTURE_DIR)
        clock = SteppingClock(datetime(2026, 8, 26, 16, 0, tzinfo=UTC))
        self.svc = IntakeService(
            InMemoryRepository(clock),
            MemoryArtifactStorage(),
            self.package.policy,
            clock,
            token_factory=lambda: "t-upload-token",
        )
        case = self.svc.create_case(self.package, ActorContext.system())
        self.svc.begin_verification(case.case_id, ActorContext.system())
        self.case_id = case.case_id
        self.session_id = case.agent_session_id
        self.sessions = tmp_path / "sessions"
        self.models: list[StandInModel] = []
        self.results: list[RunResult] = []

    def sm(self) -> FileSessionManager:
        return FileSessionManager(session_id=self.session_id, storage_dir=str(self.sessions))

    def run(
        self,
        event_type: InvocationEventType,
        *,
        event_id: str,
        policy: Policy = canonical_policy,
        actor_id: str = "control-plane",
        role: ActorRole = ActorRole.SYSTEM,
        evidence: EvidenceDelivery | None = None,
        interrupt_responses: tuple[dict, ...] = (),
        injector: dict[str, int] | None = None,
    ) -> RunResult:
        model = StandInModel(policy)
        self.models.append(model)
        event = InvocationEvent(
            case_id=self.case_id,
            event_id=event_id,
            event_type=event_type,
            trusted_actor_id=actor_id,
            trusted_actor_role=role,
            session_id=self.session_id,
            evidence=evidence,
            interrupt_responses=interrupt_responses,
        )
        result = run_event(event, self.svc, model, self.sm(), tool_failure_injector=injector)
        self.results.append(result)
        return result

    def evidence(
        self, *, sender_message: str | None = None, contact: str = "SITE-CONTACT-002"
    ) -> EvidenceDelivery:
        request = self.svc.repo.list_requests(self.case_id)[0]
        reply = json.loads(self.package.later["sender-reply.json"])
        return EvidenceDelivery(
            request_id=request.request_id,
            upload_token=request.upload_token,
            submitted_by_contact_id=contact,
            artifacts=(
                IncomingArtifact(
                    filename="consent-addendum.json",
                    mime_type="application/json",
                    content=self.package.later["consent-addendum.json"],
                ),
            ),
            sender_message=reply["free_text"] if sender_message is None else sender_message,
        )

    def stage_a(self) -> RunResult:
        return self.run(InvocationEventType.CASE_READY, event_id="EVT-A")

    def stage_b(self, **kw) -> RunResult:  # type: ignore[no-untyped-def]
        return self.run(
            InvocationEventType.EVIDENCE_RECEIVED,
            event_id="EVT-B",
            actor_id="SITE-CONTACT-002",
            role=ActorRole.SITE_CONTACT,
            evidence=self.evidence(),
            **kw,
        )

    def stage_c(self, option: str = "QUARANTINE", role: ActorRole = ActorRole.COORDINATOR) -> RunResult:
        pending = self.results[-1].pending_interrupt
        assert pending is not None
        return self.run(
            InvocationEventType.HUMAN_DECISION_RECEIVED,
            event_id="EVT-C",
            actor_id="coordinator-1",
            role=role,
            interrupt_responses=(
                {
                    "interruptId": pending.interrupt_id,
                    "response": {
                        "selected_option": option,
                        "comment": "hold",
                        "actor_role": "PRINCIPAL_INVESTIGATOR",
                    },
                },
            ),
        )

    def audit(self, event_type=None, tool_name=None):  # type: ignore[no-untyped-def]
        return [
            a
            for a in self.svc.repo.list_audit(self.case_id)
            if (event_type is None or a.event_type is event_type)
            and (tool_name is None or a.tool_name == tool_name)
        ]
