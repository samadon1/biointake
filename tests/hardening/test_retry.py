"""RETRY_REQUESTED trusted event (Phase 1A.1 §2.5)."""

from __future__ import annotations

import pytest

from biointake.domain.commands import RetryRequestedCommand
from biointake.domain.enums import (
    AuditEventType,
    CaseState,
    CheckCategory,
    CheckStatus,
    Disposition,
    ReasonCode,
    SampleState,
)
from biointake.domain.errors import CaseFinalizedError, InvalidTransitionError, PolicyDeniedError
from conftest import AGENT, dispose, next_op


def _break_lims_once(d, times=1):
    d.svc.verification.fault_injector[CheckCategory.LIMS_RECONCILIATION] = times


def _error_out(demo, sid="BX-201"):
    demo.svc.create_case(demo.package, AGENT)
    demo.case_id = "CASE-SHIP-DEMO-001"
    demo.svc.begin_verification(demo.case_id, AGENT)
    _break_lims_once(demo, 12)  # every sample's LIMS check crashes once this run
    demo.svc.verify(demo.case_id, AGENT)
    r = dispose(demo, sid)
    assert r.status == "error" and r.data["retryable"] is True
    assert demo.repo.get_sample(sid).state is SampleState.ERROR
    demo.svc.recompute_case_state(demo.case_id, AGENT)
    assert demo.repo.get_case(demo.case_id).state is CaseState.FAILED
    return demo


def retry(d, sid="BX-201"):
    return d.svc.retry_requested(
        RetryRequestedCommand(
            operation_id=next_op(),
            case_id=d.case_id,
            actor=AGENT,
            sample_id=sid,
            attempt_reason="transient LIMS outage",
        )
    )


def test_agent_cannot_move_error_to_pending_directly(demo):
    d = _error_out(demo)
    with pytest.raises(CaseFinalizedError):  # case is FAILED; ordinary transitions are closed
        d.svc.transitions.transition_sample("BX-201", SampleState.PENDING, AGENT, ReasonCode.CHECK_ERROR)
    with pytest.raises(CaseFinalizedError):
        d.svc.transitions.transition_case(
            d.case_id, CaseState.VERIFYING, AGENT, ReasonCode.CHECK_ERROR
        )  # no reopen flag


def test_retry_reopens_case_and_recovers(demo):
    d = _error_out(demo)
    res = retry(d)
    assert (
        res.status == "ok"
        and res.data["attempt"] == 1
        and res.data["results"]["LIMS_RECONCILIATION"] == "PASS"
    )
    assert d.repo.get_sample("BX-201").state is SampleState.PENDING
    assert d.repo.get_case(d.case_id).state is CaseState.VERIFYING
    types = [a.event_type for a in d.repo.list_audit(d.case_id)]
    assert AuditEventType.RETRY_REQUESTED in types
    assert dispose(d, "BX-201").status == "ok"
    # prior ERROR check remains in history
    assert any(
        c.status is CheckStatus.ERROR for c in d.repo.check_history(d.case_id) if c.sample_id == "BX-201"
    )


def test_retry_budget_is_two(demo):
    d = _error_out(demo)
    _break_lims_once(d, 5)
    assert retry(d).data["results"]["LIMS_RECONCILIATION"] == "ERROR"
    dispose(d, "BX-201")  # back to ERROR
    assert retry(d).data["attempt"] == 2
    dispose(d, "BX-201")
    with pytest.raises(PolicyDeniedError) as e:
        retry(d)
    assert e.value.code is ReasonCode.RETRY_BUDGET_EXHAUSTED
    assert any(a.event_type is AuditEventType.RETRY_REFUSED for a in d.repo.list_audit(d.case_id))


def test_deterministic_failures_are_never_retried(at_checkpoint_1):
    d = at_checkpoint_1
    for sid in ("BX-211", "BX-212", "BX-209"):  # collision / temperature FAIL / missing evidence
        with pytest.raises(PolicyDeniedError) as e:
            retry(d, sid)
        assert e.value.code is ReasonCode.RETRY_NOT_PERMITTED


def test_retry_is_refused_on_completed_case(completed):
    with pytest.raises(CaseFinalizedError):
        retry(completed, "BX-212")


def test_invalid_transition_table_still_enforced(demo):
    d = _error_out(demo)
    retry(d)
    with pytest.raises(InvalidTransitionError):
        d.svc.transitions.transition_sample(
            "BX-201",
            SampleState.ACCEPTED_WITH_EXCEPTION,
            AGENT,
            ReasonCode.ALL_CHECKS_PASS,
            policy_evaluation_id="PE",
        )
    assert Disposition.ACCEPT  # sanity
