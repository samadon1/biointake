"""Deterministic evidence dependencies and invalidation plans (Phase 1A.1 §2.1–2.2)."""

from __future__ import annotations

import json

import pytest

from biointake.domain.commands import ApplyInvalidationPlanCommand, ProposedCorrection
from biointake.domain.enums import CaseState, CheckCategory, CheckStatus, ReasonCode, SampleState
from biointake.domain.errors import NotFoundError, PolicyDeniedError
from biointake.services.dependencies import plan_digest
from conftest import AGENT, addendum_artifact, apply_plan, dispose, next_op, receive, row7_correction

C = CheckCategory


def _checks(d, sid):
    return d.repo.checks_by_category(sid, d.case_id)


def test_check_results_carry_dependency_metadata(at_checkpoint_1):
    d = at_checkpoint_1
    for c in d.repo.current_checks(d.case_id):
        assert c.evidence_dependency_ids and c.input_fingerprint and c.policy_version == "3.0.0"
    consent = _checks(d, "BX-209")[C.CONSENT_VALIDITY]
    assert "participant:NS-P-0209" in consent.evidence_dependency_ids
    temp = _checks(d, "BX-212")[C.TEMPERATURE_REQUIREMENT]
    assert any(v == d.repo.get_artifact(k).sha256 for k, v in temp.source_record_versions.items())


def test_bx207_results_are_provisional_and_flagged(at_checkpoint_1):
    d = at_checkpoint_1
    checks = _checks(d, "BX-207")
    for cat in (C.PROTOCOL_ELIGIBILITY, C.CONSENT_VALIDITY):
        assert checks[cat].provisional and "assoc:BX-207:row7" in checks[cat].evidence_dependency_ids
    assert (
        not checks[C.IDENTITY_MATCH].provisional and checks[C.IDENTITY_MATCH].status is CheckStatus.AMBIGUOUS
    )
    assert not any(c.provisional for c in _checks(d, "BX-208").values())


def test_canonical_reply_invalidates_exactly_four_checks(at_checkpoint_1, package):
    d = at_checkpoint_1
    res = receive(d, artifacts=(addendum_artifact(package),), corrections=(row7_correction(),))
    plan = d.repo.get_plan(res.data["plan_id"])
    assert plan is not None
    invalidated = {(d_id.split("-", 1)[1]) for d_id in plan.invalidated_check_ids}
    assert {i.rsplit("-", 1)[0] for i in invalidated} == {
        "BX-207-IDENTITY_MATCH",
        "BX-207-MANIFEST_MATCH",
        "BX-209-CONSENT_VALIDITY",
        "BX-210-CONSENT_VALIDITY",
    }
    assert len(plan.invalidated_check_ids) == 4 and len(plan.reasons_by_check) == 4
    assert plan.digest == plan_digest(plan)
    # provisional BX-207 results whose inputs did not change are retained, not re-run
    retained_cats = {
        d.repo.current_checks(d.case_id, "BX-207")[0].case_id and cid.split("-BX-207-")[1].rsplit("-", 1)[0]
        for cid in plan.retained_provisional_check_ids
    }
    assert {
        "PROTOCOL_ELIGIBILITY",
        "CONSENT_VALIDITY",
        "TEMPERATURE_REQUIREMENT",
        "CHAIN_OF_CUSTODY",
        "LIMS_RECONCILIATION",
    } >= retained_cats
    assert len(plan.retained_provisional_check_ids) >= 2
    applied = apply_plan(d, res)
    assert len(applied.data["produced_check_ids"]) == 4
    assert dispose(d, "BX-207").status == "ok"


def test_provisional_results_cannot_be_used_without_identity_confirmation(at_checkpoint_1):
    d = at_checkpoint_1
    r = dispose(d, "BX-207")
    assert r.status == "waiting" and set(r.data["blocking_checks"]) <= {"IDENTITY_MATCH", "MANIFEST_MATCH"}


def test_refuting_attestation_invalidates_dependents_and_accepts_nothing(at_checkpoint_1):
    d = at_checkpoint_1
    refute = ProposedCorrection(
        manifest_row=7,
        manifest_value="BX-2O7",
        corrected_value="BX-217",
        sender_statement="Row 7 is BX-217, which was not shipped.",
    )
    res = receive(d, corrections=(refute,))
    assert res.status == "ok"
    plan = d.repo.get_plan(res.data["plan_id"])
    assert plan is not None
    # everything that depended on the tentative association is invalidated: identity, manifest, and the
    # provisional protocol/consent results. Temperature, custody and LIMS never depended on the row.
    assert len(plan.invalidated_check_ids) == 4 and plan.retained_provisional_check_ids == ()
    assert {cid.split("-BX-207-")[1].rsplit("-", 1)[0] for cid in plan.invalidated_check_ids} == {
        "IDENTITY_MATCH",
        "MANIFEST_MATCH",
        "PROTOCOL_ELIGIBILITY",
        "CONSENT_VALIDITY",
    }
    apply_plan(d, res)
    checks = _checks(d, "BX-207")
    assert checks[C.IDENTITY_MATCH].status is CheckStatus.UNAVAILABLE
    assert ReasonCode.ASSOCIATION_REFUTED in checks[C.IDENTITY_MATCH].reason_codes
    assert (
        checks[C.CONSENT_VALIDITY].status is CheckStatus.UNAVAILABLE
        and not checks[C.CONSENT_VALIDITY].provisional
    )
    r = dispose(d, "BX-207")
    assert r.status == "waiting"
    assert d.repo.get_sample("BX-207").state is SampleState.WAITING_FOR_EVIDENCE
    assert d.svc.build_report(d.case_id)["unauthorized_acceptances"] == 0


def test_refutation_may_not_reassign_row_to_another_shipped_sample(at_checkpoint_1):
    d = at_checkpoint_1
    res = receive(
        d,
        corrections=(ProposedCorrection(manifest_row=7, manifest_value="BX-2O7", corrected_value="BX-208"),),
    )
    assert res.status == "denied" and ReasonCode.EVIDENCE_CONTRADICTORY in res.reason_codes


def test_plan_rejects_unknown_or_foreign_evidence(at_checkpoint_1):
    d = at_checkpoint_1
    with pytest.raises(NotFoundError):
        d.svc.dependencies.compute_invalidation_plan(d.case_id, ["ART-9999"], AGENT)
    manifest = d.repo.list_artifacts(d.case_id)[0]
    foreign = manifest.model_copy(update={"artifact_id": "ART-FOREIGN", "case_id": "CASE-OTHER"})
    d.repo.save_artifact(foreign)
    with pytest.raises(PolicyDeniedError) as e:
        d.svc.dependencies.compute_invalidation_plan(d.case_id, ["ART-FOREIGN"], AGENT)
    assert e.value.code is ReasonCode.INVALIDATION_PLAN_INVALID


def test_altered_plan_is_rejected(at_checkpoint_1, package):
    d = at_checkpoint_1
    res = receive(d, artifacts=(addendum_artifact(package),), corrections=(row7_correction(),))
    plan = d.repo.get_plan(res.data["plan_id"])
    assert plan is not None
    # a model (or anyone) trying to trim the set → digest no longer matches
    d.repo.save_plan(plan.model_copy(update={"invalidated_check_ids": plan.invalidated_check_ids[:2]}))
    with pytest.raises(PolicyDeniedError) as e:
        apply_plan(d, res)
    assert e.value.code is ReasonCode.INVALIDATION_PLAN_INVALID
    # …and trying to add a check id
    d.repo.save_plan(
        plan.model_copy(
            update={"invalidated_check_ids": plan.invalidated_check_ids + ("CHK-BX-201-IDENTITY_MATCH-0001",)}
        )
    )
    with pytest.raises(PolicyDeniedError):
        apply_plan(d, res)


def test_stale_plan_is_rejected_and_application_is_idempotent(at_checkpoint_1, package):
    d = at_checkpoint_1
    res = receive(d, artifacts=(addendum_artifact(package),), corrections=(row7_correction(),))
    # advance the case version before applying → stale
    d.svc.transitions.transition_case(
        d.case_id, CaseState.WAITING_FOR_EVIDENCE, AGENT, ReasonCode.EVIDENCE_RECOVERY_IN_PROGRESS
    )
    with pytest.raises(PolicyDeniedError) as e:
        apply_plan(d, res)
    assert "case version" in str(e.value)
    # recompute a fresh plan against the current version and apply it twice → one re-verification
    plan = d.svc.dependencies.compute_invalidation_plan(
        d.case_id, list(res.data["admitted_artifact_ids"]), AGENT
    )
    op = next_op()
    n_before = len(d.repo.check_history(d.case_id))
    a1 = d.svc.apply_invalidation_plan(
        ApplyInvalidationPlanCommand(operation_id=op, case_id=d.case_id, actor=AGENT, plan_id=plan.plan_id)
    )
    a2 = d.svc.apply_invalidation_plan(
        ApplyInvalidationPlanCommand(operation_id=op, case_id=d.case_id, actor=AGENT, plan_id=plan.plan_id)
    )
    assert a1 == a2 and len(d.repo.check_history(d.case_id)) == n_before + 4
    a3 = d.svc.apply_invalidation_plan(
        ApplyInvalidationPlanCommand(
            operation_id=next_op(), case_id=d.case_id, actor=AGENT, plan_id=plan.plan_id
        )
    )
    assert len(d.repo.check_history(d.case_id)) == n_before + 4 and len(a3.data["produced_check_ids"]) == 4


def test_plan_for_another_case_is_rejected(at_checkpoint_1, package):
    d = at_checkpoint_1
    res = receive(d, artifacts=(addendum_artifact(package),))
    with pytest.raises((PolicyDeniedError, NotFoundError)):
        d.svc.apply_invalidation_plan(
            ApplyInvalidationPlanCommand(
                operation_id=next_op(), case_id="CASE-OTHER", actor=AGENT, plan_id=res.data["plan_id"]
            )
        )


def test_report_lists_provisional_checks(at_checkpoint_1):
    rep = at_checkpoint_1.svc.build_report(at_checkpoint_1.case_id)
    by_id = {s["sample_id"]: s for s in rep["samples"]}
    assert (
        "CONSENT_VALIDITY" in by_id["BX-207"]["provisional_checks"]
        and by_id["BX-208"]["provisional_checks"] == []
    )
    assert json.dumps(rep, default=str)  # serialisable
