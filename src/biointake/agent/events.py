"""Typed invocation events (control layer → agent) and typed run results (agent → control layer)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..domain.commands import IncomingArtifact
from ..domain.enums import ActorRole, ActorType, CaseState, InvocationEventType
from ..domain.models import ActorContext


class EvidenceDelivery(BaseModel):
    """Trusted description of evidence that arrived through a case-scoped upload link."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    request_id: str
    upload_token: str
    submitted_by_contact_id: str
    artifacts: tuple[IncomingArtifact, ...] = ()
    artifact_refs: tuple[str, ...] = ()  # storage URIs of staged uploads (resolved by the runtime)
    sender_message: str = ""  # free text, untrusted, shown to the model fenced


class InvocationEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    case_id: str
    event_id: str
    event_type: InvocationEventType
    trusted_actor_id: str
    trusted_actor_role: ActorRole
    session_id: str
    trace_id: str = ""
    new_artifact_ids: tuple[str, ...] = ()
    evidence: EvidenceDelivery | None = None
    interrupt_responses: tuple[dict[str, Any], ...] = ()  # [{"interruptId": ..., "response": {...}}]

    def actor(self) -> ActorContext:
        if self.event_type is InvocationEventType.HUMAN_DECISION_RECEIVED:
            actor_type = ActorType.HUMAN
        elif self.event_type is InvocationEventType.EVIDENCE_RECEIVED:
            actor_type = ActorType.SENDER
        else:
            actor_type = ActorType.SYSTEM
        return ActorContext(
            actor_type=actor_type, actor_id=self.trusted_actor_id, role=self.trusted_actor_role
        )


class PendingInterrupt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    interrupt_id: str
    name: str
    reason: dict[str, Any]


class RunResult(BaseModel):
    """Structured outcome of one agent invocation. The control layer never scrapes prose."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    case_id: str
    event_id: str
    session_id: str
    stable_state: CaseState
    is_stable: bool
    case_version: int
    stop_reason: str
    committed_dispositions: dict[str, str] = Field(default_factory=dict)  # sample_id → state
    created_evidence_request_ids: tuple[str, ...] = ()
    pending_interrupt: PendingInterrupt | None = None
    checks_evaluated: int = 0
    checks_reverified: int = 0
    tool_attempt_count: int = 0
    logical_effect_count: int = 0
    retry_count: int = 0
    model_call_count: int = 0
    intervention_denials: int = 0
    unauthorized_acceptances: int = 0
    warnings: tuple[str, ...] = ()
    final_text: str = ""
    boot_id: str = ""  # per-process id of the runtime that produced this result (fresh-microVM proof)
