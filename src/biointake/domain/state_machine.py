"""The ONLY legal way to change a case or sample state.

Models are frozen, so `sample.state = X` raises. Callers get a new record back from this service,
and every transition is written to the audit log with actor, reason, evidence and policy references.
"""

from __future__ import annotations

from ..clock import Clock
from .enums import (
    TERMINAL_CASE_STATES,
    ActorType,
    AuditEventType,
    CaseState,
    Disposition,
    ReasonCode,
    SampleState,
)
from .errors import CaseFinalizedError, InvalidTransitionError
from .models import ActorContext, AuditEvent, Sample, ShipmentCase

CASE_TRANSITIONS: dict[CaseState, frozenset[CaseState]] = {
    # The intake ramp: a shipment is announced before it arrives, received when custody is taken and its
    # condition recorded, and only then reconciled (ISBER L4.2 / J6; CAP BAP.13200).
    CaseState.CREATED: frozenset({CaseState.ANNOUNCED, CaseState.VERIFYING, CaseState.FAILED}),
    CaseState.ANNOUNCED: frozenset({CaseState.RECEIVED, CaseState.FAILED}),
    CaseState.RECEIVED: frozenset({CaseState.VERIFYING, CaseState.FAILED}),
    CaseState.VERIFYING: frozenset(
        {
            CaseState.WAITING_FOR_EVIDENCE,
            CaseState.NEEDS_HUMAN_DECISION,
            CaseState.COMPLETED,
            CaseState.FAILED,
        }
    ),
    CaseState.WAITING_FOR_EVIDENCE: frozenset({CaseState.VERIFYING, CaseState.FAILED}),
    CaseState.NEEDS_HUMAN_DECISION: frozenset({CaseState.VERIFYING, CaseState.FAILED}),
    # A completed case reopens only for a quarantine review (reopen=True). Held material is resolved weeks
    # later in practice, and the case genuinely is not finished while a specimen is still under review.
    CaseState.COMPLETED: frozenset({CaseState.VERIFYING}),
    CaseState.FAILED: frozenset(
        {CaseState.VERIFYING}
    ),  # only via the trusted RETRY_REQUESTED handler (reopen=True)
}

SAMPLE_TRANSITIONS: dict[SampleState, frozenset[SampleState]] = {
    SampleState.PENDING: frozenset(
        {
            SampleState.WAITING_FOR_EVIDENCE,
            SampleState.NEEDS_HUMAN_DECISION,
            SampleState.ACCEPTED,
            SampleState.QUARANTINED,
            SampleState.ERROR,
        }
    ),
    SampleState.WAITING_FOR_EVIDENCE: frozenset(
        {
            SampleState.NEEDS_HUMAN_DECISION,
            SampleState.ACCEPTED,
            SampleState.QUARANTINED,
            SampleState.ERROR,
        }
    ),
    SampleState.NEEDS_HUMAN_DECISION: frozenset(
        {
            SampleState.ACCEPTED_WITH_EXCEPTION,
            SampleState.QUARANTINED,
            SampleState.REJECTED,
            SampleState.ERROR,
        }
    ),
    SampleState.ERROR: frozenset({SampleState.PENDING}),
    SampleState.ACCEPTED: frozenset(),
    SampleState.ACCEPTED_WITH_EXCEPTION: frozenset(),
    # Quarantine is a hold, not a grave. A review either returns the specimen for re-verification, where the
    # policy engine decides afresh, exactly as it would have the first time, or rejects it outright. What a
    # review may never do is promote a held specimen straight to ACCEPTED: that would let a human authorise an
    # acceptance the engine never sanctioned, which is the one thing this system exists to prevent.
    SampleState.QUARANTINED: frozenset({SampleState.PENDING, SampleState.REJECTED}),
    SampleState.REJECTED: frozenset(),
}

_DISPOSITION_FOR_STATE: dict[SampleState, Disposition] = {
    SampleState.ACCEPTED: Disposition.ACCEPT,
    SampleState.ACCEPTED_WITH_EXCEPTION: Disposition.ACCEPT_WITH_EXCEPTION,
    SampleState.QUARANTINED: Disposition.QUARANTINE,
    SampleState.REJECTED: Disposition.REJECT,
}


class TransitionService:
    def __init__(self, repo: Repository, clock: Clock) -> None:
        self._repo = repo
        self._clock = clock

    # -- case ----------------------------------------------------------------------------------
    def transition_case(
        self,
        case_id: str,
        to_state: CaseState,
        actor: ActorContext,
        reason_code: ReasonCode,
        *,
        operation_id: str | None = None,
        summary: str | None = None,
        reopen: bool = False,
    ) -> ShipmentCase:
        case = self._repo.get_case(case_id)
        if case.state in TERMINAL_CASE_STATES and not (
            reopen and case.state in (CaseState.FAILED, CaseState.COMPLETED)
        ):
            raise CaseFinalizedError(f"case {case_id} is {case.state.value}; no further transitions")
        if to_state not in CASE_TRANSITIONS[case.state]:
            raise InvalidTransitionError(
                f"case {case_id}: {case.state.value} → {to_state.value} is not allowed"
            )
        now = self._clock()
        updated = case.model_copy(
            update={
                "state": to_state,
                "case_version": case.case_version + 1,
                "updated_at": now,
                "completed_at": now if to_state in TERMINAL_CASE_STATES else case.completed_at,
            }
        )
        self._repo.save_case(updated)
        self._repo.append_audit(
            case_id=case_id,
            event_type=AuditEventType.CASE_TRANSITION,
            actor=actor,
            summary=summary or f"Case {case.state.value} → {to_state.value}",
            reason_codes=(reason_code,),
            operation_id=operation_id,
            metadata={"from": case.state.value, "to": to_state.value, "case_version": updated.case_version},
        )
        return updated

    # -- sample --------------------------------------------------------------------------------
    def transition_sample(
        self,
        sample_id: str,
        to_state: SampleState,
        actor: ActorContext,
        reason_code: ReasonCode,
        *,
        evidence_refs: tuple[str, ...] = (),
        policy_evaluation_id: str | None = None,
        operation_id: str | None = None,
        summary: str | None = None,
    ) -> Sample:
        sample = self._repo.get_sample(sample_id)
        case = self._repo.get_case(sample.case_id)
        if case.state in TERMINAL_CASE_STATES:
            raise CaseFinalizedError(f"case {case.case_id} is {case.state.value}; samples are immutable")
        if to_state not in SAMPLE_TRANSITIONS[sample.state]:
            raise InvalidTransitionError(
                f"sample {sample_id}: {sample.state.value} → {to_state.value} is not allowed"
            )
        if to_state in _DISPOSITION_FOR_STATE and policy_evaluation_id is None:
            raise InvalidTransitionError(
                f"sample {sample_id}: transition to {to_state.value} requires a policy evaluation id"
            )
        now = self._clock()
        updated = sample.model_copy(
            update={
                "state": to_state,
                "disposition": _DISPOSITION_FOR_STATE.get(to_state, sample.disposition),
                "sample_version": sample.sample_version + 1,
                "updated_at": now,
            }
        )
        self._repo.save_sample(updated)
        self._repo.save_case(
            case.model_copy(update={"case_version": case.case_version + 1, "updated_at": now})
        )
        self._repo.append_audit(
            case_id=sample.case_id,
            event_type=AuditEventType.SAMPLE_TRANSITION,
            actor=actor,
            summary=summary or f"{sample_id}: {sample.state.value} → {to_state.value}",
            reason_codes=(reason_code,),
            sample_ids=(sample_id,),
            operation_id=operation_id,
            metadata={
                "from": sample.state.value,
                "to": to_state.value,
                "evidence_refs": list(evidence_refs),
                "policy_evaluation_id": policy_evaluation_id,
            },
        )
        return updated


def audit_actor_type(actor: ActorContext) -> ActorType:
    return actor.actor_type


__all__ = ["CASE_TRANSITIONS", "SAMPLE_TRANSITIONS", "TransitionService", "AuditEvent"]

from ..repositories.interfaces import Repository  # noqa: E402  (circular-import guard)
