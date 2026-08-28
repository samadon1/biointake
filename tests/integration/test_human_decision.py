from __future__ import annotations

import pytest

from biointake.domain.commands import (
    FinalizeCaseCommand,
    OpenQuarantineReviewCommand,
    RaisePendingDecisionCommand,
    RecordHumanDecisionCommand,
)
from biointake.domain.enums import AuditEventType, CaseState, Disposition, HumanOption, SampleState
from biointake.domain.errors import CaseFinalizedError, PolicyDeniedError, UnauthorizedError
from conftest import AGENT, COORDINATOR, PI, dispose, next_op


def decide(d, option, actor, op=None, client_payload=None, issue_id=None):
    return d.svc.record_human_decision(
        RecordHumanDecisionCommand(
            operation_id=op or next_op(),
            case_id=d.case_id,
            expected_case_version=d.version(),
            actor=actor,
            issue_id=issue_id or d.issue_id,
            selected_option=option,
            client_payload=client_payload or {},
        )
    )


def test_exception_requires_pi_role(at_checkpoint_2):
    d = at_checkpoint_2
    with pytest.raises(UnauthorizedError):
        decide(d, HumanOption.APPROVE_EXCEPTION, COORDINATOR)
    assert d.repo.get_sample("BX-212").state is SampleState.NEEDS_HUMAN_DECISION
    res = decide(d, HumanOption.APPROVE_EXCEPTION, PI)
    assert res.status == "ok" and d.repo.get_sample("BX-212").state is SampleState.ACCEPTED_WITH_EXCEPTION
    rec = d.svc.lims.find_by_sample("BX-212")
    assert rec is not None and rec.status == "ACCEPTED_WITH_EXCEPTION" and rec.policy_evaluation_id


def test_client_supplied_role_is_ignored(at_checkpoint_2):
    d = at_checkpoint_2
    with pytest.raises(UnauthorizedError):
        decide(
            d,
            HumanOption.APPROVE_EXCEPTION,
            COORDINATOR,
            client_payload={"actor_role": "PRINCIPAL_INVESTIGATOR", "role": "PRINCIPAL_INVESTIGATOR"},
        )
    rejected = [a for a in d.repo.list_audit(d.case_id) if a.event_type is AuditEventType.OPERATION_REJECTED]
    assert rejected[-1].metadata["client_supplied_role_ignored"] == "PRINCIPAL_INVESTIGATOR"


def test_duplicate_human_response_is_one_effective_transition(at_checkpoint_2):
    d = at_checkpoint_2
    op = next_op()
    first = decide(d, HumanOption.QUARANTINE, COORDINATOR, op=op)
    writes = d.svc.lims.write_count
    second = decide(d, HumanOption.QUARANTINE, COORDINATOR, op=op)
    assert first == second and d.svc.lims.write_count == writes
    assert len(d.repo.list_decisions(d.case_id)) == 1
    transitions = [
        a
        for a in d.repo.list_audit(d.case_id)
        if a.event_type is AuditEventType.SAMPLE_TRANSITION
        and "BX-212" in a.sample_ids
        and a.metadata["to"] == "QUARANTINED"
    ]
    assert len(transitions) == 1
    with pytest.raises(PolicyDeniedError):  # a different operation on an already-resolved issue
        decide(d, HumanOption.APPROVE_EXCEPTION, PI)


def test_rejection_requires_the_authorised_role(at_checkpoint_2):
    """Rejection is irreversible, so it is reserved to the roles the protocol names, here the PI."""
    d = at_checkpoint_2
    with pytest.raises(UnauthorizedError):
        decide(d, HumanOption.REJECT, COORDINATOR)
    assert d.repo.get_sample("BX-212").state is SampleState.NEEDS_HUMAN_DECISION

    res = decide(d, HumanOption.REJECT, PI)
    assert res.status == "ok" and d.repo.get_sample("BX-212").state is SampleState.REJECTED
    # A rejected specimen is still written to the LIMS: "we received this and rejected it" is precisely
    # the fact a site comes back asking about, and silence would read as never having arrived.
    rec = d.svc.lims.find_by_sample("BX-212")
    assert rec is not None and rec.status == "REJECTED" and rec.policy_evaluation_id


def test_a_quarantine_can_be_reopened_and_the_engine_decides_again(at_checkpoint_2):
    """Quarantine is a hold, not a grave, but reopening it re-runs verification rather than accepting."""
    d = at_checkpoint_2
    decide(d, HumanOption.QUARANTINE, COORDINATOR)
    assert d.repo.get_sample("BX-212").state is SampleState.QUARANTINED

    with pytest.raises(UnauthorizedError):
        d.svc.open_quarantine_review(
            OpenQuarantineReviewCommand(
                operation_id=next_op(),
                case_id=d.case_id,
                actor=AGENT,
                sample_id="BX-212",
                reason="the agent may not lift its own hold",
            )
        )

    res = d.svc.open_quarantine_review(
        OpenQuarantineReviewCommand(
            operation_id=next_op(),
            case_id=d.case_id,
            actor=COORDINATOR,
            sample_id="BX-212",
            reason="site confirmed the excursion window; re-checking",
        )
    )
    assert res.status == "ok"
    # Re-verified, not accepted: the specimen is back in the queue and the engine will decide afresh.
    assert d.repo.get_sample("BX-212").state is not SampleState.ACCEPTED
    opened = [
        a for a in d.repo.list_audit(d.case_id) if a.event_type is AuditEventType.QUARANTINE_REVIEW_OPENED
    ]
    assert len(opened) == 1 and "site confirmed" in opened[0].metadata["reason"]


def test_a_quarantine_review_needs_a_stated_reason(at_checkpoint_2):
    d = at_checkpoint_2
    decide(d, HumanOption.QUARANTINE, COORDINATOR)
    with pytest.raises(PolicyDeniedError):
        d.svc.open_quarantine_review(
            OpenQuarantineReviewCommand(
                operation_id=next_op(),
                case_id=d.case_id,
                actor=COORDINATOR,
                sample_id="BX-212",
                reason="   ",
            )
        )


def test_only_a_quarantined_specimen_has_a_hold_to_review(at_checkpoint_2):
    d = at_checkpoint_2
    with pytest.raises(PolicyDeniedError):
        d.svc.open_quarantine_review(
            OpenQuarantineReviewCommand(
                operation_id=next_op(),
                case_id=d.case_id,
                actor=COORDINATOR,
                sample_id="BX-212",
                reason="nothing is on hold yet",
            )
        )


def test_pending_decision_is_idempotent_on_issue_id(at_checkpoint_2):
    d = at_checkpoint_2
    res = d.svc.raise_pending_decision(
        RaisePendingDecisionCommand(
            operation_id=next_op(), case_id=d.case_id, actor=AGENT, sample_id="BX-212"
        )
    )
    assert res.data["created"] is False and res.data["issue_id"] == d.issue_id
    assert len(d.repo.list_pending_decisions(d.case_id)) == 1
    requested = [
        a for a in d.repo.list_audit(d.case_id) if a.event_type is AuditEventType.PENDING_DECISION_CREATED
    ]
    assert len(requested) == 1


def test_finalize_refuses_while_decision_pending(at_checkpoint_2):
    d = at_checkpoint_2
    with pytest.raises(PolicyDeniedError):
        d.svc.finalize(FinalizeCaseCommand(operation_id=next_op(), case_id=d.case_id, actor=AGENT))
    assert d.repo.get_case(d.case_id).state is CaseState.NEEDS_HUMAN_DECISION


def test_completed_case_rejects_ordinary_mutation(completed):
    d = completed
    assert d.repo.get_case(d.case_id).state is CaseState.COMPLETED
    with pytest.raises(CaseFinalizedError):
        dispose(d, "BX-212", Disposition.ACCEPT)
    with pytest.raises(CaseFinalizedError):
        d.svc.verify(d.case_id, AGENT)
    with pytest.raises(CaseFinalizedError):
        d.svc.raise_pending_decision(
            RaisePendingDecisionCommand(
                operation_id=next_op(), case_id=d.case_id, actor=AGENT, sample_id="BX-212"
            )
        )
    assert d.svc.recompute_case_state(d.case_id, AGENT).state is CaseState.COMPLETED
