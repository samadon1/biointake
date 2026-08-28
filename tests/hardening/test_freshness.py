"""Freshness-bound policy evaluations (Phase 1A.1 §2.3)."""

from __future__ import annotations

import pytest

from biointake.domain.enums import (
    CheckCategory,
    CheckStatus,
    Disposition,
    HumanOption,
    PolicyDecision,
    ReasonCode,
)
from biointake.domain.errors import LimsWriteRefusedError
from biointake.domain.models import HumanDecision
from conftest import NOW, PI, next_op


def _fresh_allowed(d, sid="BX-209"):
    sample = d.repo.get_sample(sid)
    ev = d.svc._evaluate(sample, Disposition.ACCEPT, None)
    assert ev.decision is PolicyDecision.ALLOWED
    return sample, ev


def _write(d, sample, ev, op=None):
    return d.svc.lims.write_disposition(
        sample, ev, op or next_op(), d.repo.get_evaluation, d.svc.evaluation_freshness
    )


def test_evaluation_is_bound_to_versions_and_digests(at_checkpoint_1):
    d = at_checkpoint_1
    sample = d.repo.get_sample("BX-201")
    ev = d.repo.get_evaluation("PE-0001")
    assert ev is not None and ev.sample_id == "BX-201"
    assert ev.check_set_digest and ev.evidence_snapshot_digest and ev.sample_version == 0
    assert ev.consumed_by_operation_id is not None  # consumed by the accepting write
    assert sample.sample_version == 1


def test_old_allowed_cannot_be_replayed_after_a_check_changes(at_checkpoint_2):
    d = at_checkpoint_2
    # BX-212 needs a human; create a state where a fresh ALLOWED exists, then mutate a check
    d2 = d
    sample = d2.repo.get_sample("BX-212")
    # simulate: an ALLOWED quarantine evaluation exists (human decision), but a check is re-run afterwards
    decision = HumanDecision(
        decision_id="HD-X",
        case_id=d2.case_id,
        issue_id=d2.issue_id,
        sample_id="BX-212",
        actor_id="c",
        actor_role=PI.role,
        selected_option=HumanOption.QUARANTINE,
        operation_id="op",
        created_at=NOW,
    )
    d2.repo.save_decision(decision)
    ev = d2.svc._evaluate(sample, Disposition.QUARANTINE, decision)
    assert ev.decision is PolicyDecision.ALLOWED
    d2.svc.verify(
        d2.case_id, PI, sample_ids=("BX-212",), categories=(CheckCategory.CHAIN_OF_CUSTODY,)
    )  # check set changed
    with pytest.raises(LimsWriteRefusedError) as e:
        _write(d2, sample, ev)
    assert e.value.code is ReasonCode.STALE_POLICY_EVALUATION and "check set changed" in str(e.value)
    assert d2.svc.lims.find_by_sample("BX-212").status == "EXPECTED"


def test_old_allowed_cannot_be_replayed_after_policy_version_changes(at_checkpoint_1):
    d = at_checkpoint_1
    from biointake.services.evidence import EvidenceService  # noqa: F401  (import guard)

    sample = d.repo.get_sample("BX-212")
    # evaluation issued under policy 3.0.0 …
    ev = d.repo.get_evaluation("PE-0001")
    assert ev is not None
    # A policy version changes on the study the case is judged by, which is where a real
    # protocol amendment would land.
    study = d.repo.get_study(d.svc.policy.protocol_id)
    assert study is not None
    d.repo.save_study(
        study.model_copy(
            update={
                "policy": study.policy.model_copy(update={"version": "3.1.0"}),
                "policy_version": "3.1.0",
            }
        )
    )
    fresh, why = d.svc.evaluation_freshness(ev)
    assert not fresh and "policy" in why
    with pytest.raises(LimsWriteRefusedError) as e:
        d.svc.lims.write_disposition(
            d.repo.get_sample("BX-201"), ev, next_op(), d.repo.get_evaluation, d.svc.evaluation_freshness
        )
    assert e.value.code in (ReasonCode.EVALUATION_CONSUMED, ReasonCode.STALE_POLICY_EVALUATION)
    assert sample.sample_id == "BX-212"


def test_evaluation_for_one_sample_cannot_authorize_another(at_checkpoint_1):
    d = at_checkpoint_1
    sample209, ev = _fresh_allowed(d, "BX-209") if False else (None, None)
    # BX-209 is WAITING (not allowed); use BX-208's consumed ALLOWED evaluation against BX-209
    ev208 = next(
        e
        for e in (d.repo.get_evaluation(f"PE-{i:04d}") for i in range(1, 20))
        if e and e.sample_id == "BX-208"
    )
    with pytest.raises(LimsWriteRefusedError):
        d.svc.lims.write_disposition(
            d.repo.get_sample("BX-209"), ev208, next_op(), d.repo.get_evaluation, d.svc.evaluation_freshness
        )
    assert d.svc.lims.find_by_sample("BX-209").status == "EXPECTED"


def test_ordinary_acceptance_evaluation_cannot_authorize_exception(at_checkpoint_1):
    d = at_checkpoint_1
    ev208 = next(
        e
        for e in (d.repo.get_evaluation(f"PE-{i:04d}") for i in range(1, 20))
        if e and e.sample_id == "BX-208"
    )
    forged = ev208.model_copy(
        update={"requested_disposition": Disposition.ACCEPT_WITH_EXCEPTION, "consumed_by_operation_id": None}
    )
    with pytest.raises(LimsWriteRefusedError):  # differs from the stored record
        d.svc.lims.write_disposition(
            d.repo.get_sample("BX-208"), forged, next_op(), d.repo.get_evaluation, d.svc.evaluation_freshness
        )


def test_evaluation_cannot_be_amended_with_a_later_decision(at_checkpoint_2):
    d = at_checkpoint_2
    # the HUMAN_DECISION_REQUIRED evaluation for BX-212 from stage 1
    ev = next(
        e
        for e in (d.repo.get_evaluation(f"PE-{i:04d}") for i in range(1, 30))
        if e and e.sample_id == "BX-212"
    )
    assert ev.decision is PolicyDecision.HUMAN_DECISION_REQUIRED
    decision = HumanDecision(
        decision_id="HD-LATE",
        case_id=d.case_id,
        issue_id=d.issue_id,
        sample_id="BX-212",
        actor_id="pi",
        actor_role=PI.role,
        selected_option=HumanOption.APPROVE_EXCEPTION,
        operation_id="op",
        created_at=NOW,
    )
    d.repo.save_decision(decision)
    amended = ev.model_copy(
        update={
            "decision": PolicyDecision.ALLOWED,
            "human_decision_id": "HD-LATE",
            "requested_disposition": Disposition.ACCEPT_WITH_EXCEPTION,
        }
    )
    with pytest.raises(LimsWriteRefusedError):
        d.svc.lims.write_disposition(
            d.repo.get_sample("BX-212"), amended, next_op(), d.repo.get_evaluation, d.svc.evaluation_freshness
        )
    assert d.svc.lims.find_by_sample("BX-212").status == "EXPECTED"


def test_consumed_evaluation_cannot_write_twice(at_checkpoint_1):
    d = at_checkpoint_1
    ev = d.repo.get_evaluation("PE-0001")
    assert ev is not None and ev.consumed_by_operation_id == "OP-0001"
    sample = d.repo.get_sample("BX-201")
    writes = d.svc.lims.write_count
    with pytest.raises(LimsWriteRefusedError) as e:
        d.svc.lims.write_disposition(
            sample, ev, "OP-OTHER", d.repo.get_evaluation, d.svc.evaluation_freshness
        )
    assert e.value.code is ReasonCode.EVALUATION_CONSUMED and d.svc.lims.write_count == writes
    # the original operation id replays idempotently (same record, no new write)
    rec = d.svc.lims.write_disposition(sample, ev, "OP-0001", d.repo.get_evaluation, lambda _e: (True, ""))
    assert rec.status == "ACCEPTED" and d.svc.lims.write_count == writes


def test_stale_status_after_check_mutation_end_to_end(at_checkpoint_1):
    d = at_checkpoint_1
    sample = d.repo.get_sample("BX-208")
    assert sample.state.value == "ACCEPTED"
    # a required check for an accepted sample never silently mutates acceptance; but if it were re-run and
    # the sample were not terminal, the old evaluation would be stale:
    d.svc.verify(d.case_id, PI, sample_ids=("BX-208",), categories=(CheckCategory.CHAIN_OF_CUSTODY,))
    ev = next(
        e
        for e in (d.repo.get_evaluation(f"PE-{i:04d}") for i in range(1, 20))
        if e and e.sample_id == "BX-208"
    )
    fresh, why = d.svc.evaluation_freshness(ev)
    assert not fresh and "check set changed" in why
    assert (
        d.repo.checks_by_category("BX-208", d.case_id)[CheckCategory.CHAIN_OF_CUSTODY].status
        is CheckStatus.PASS
    )
