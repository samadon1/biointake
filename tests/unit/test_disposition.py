from __future__ import annotations

import pytest

from biointake.domain.disposition import DispositionEngine
from biointake.domain.enums import (
    ActorRole,
    CheckCategory,
    CheckStatus,
    Disposition,
    HumanOption,
    PolicyDecision,
    ReasonCode,
)
from biointake.domain.models import HumanDecision
from conftest import NOW, all_pass, make_check

C = CheckCategory


def decision(role: ActorRole, option: HumanOption, sample_id: str = "S1") -> HumanDecision:
    return HumanDecision(
        decision_id="HD-1",
        case_id="CASE-T",
        issue_id="i",
        sample_id=sample_id,
        actor_id="h",
        actor_role=role,
        selected_option=option,
        operation_id="op",
        created_at=NOW,
    )


def evaluate(policy, checks, requested=Disposition.ACCEPT, human=None, sample_id="S1"):
    return DispositionEngine(policy).evaluate(
        evaluation_id="PE-T",
        case_id="CASE-T",
        sample_id=sample_id,
        checks=checks,
        requested=requested,
        human_decision=human,
        now=NOW,
    )


def test_all_pass_allows_acceptance(policy):
    ev = evaluate(policy, all_pass())
    assert ev.decision is PolicyDecision.ALLOWED
    assert ev.reason_codes == (ReasonCode.ALL_CHECKS_PASS,)


@pytest.mark.parametrize("status", [CheckStatus.UNAVAILABLE, CheckStatus.AMBIGUOUS])
def test_recoverable_statuses_block_and_wait(policy, status):
    checks = all_pass()
    checks[C.CONSENT_VALIDITY] = make_check(
        "S1", C.CONSENT_VALIDITY, status, (ReasonCode.CONSENT_ADDENDUM_MISSING,)
    )
    ev = evaluate(policy, checks)
    assert ev.decision is PolicyDecision.WAITING_FOR_EVIDENCE
    assert ev.blocking_checks == (C.CONSENT_VALIDITY,)


def test_error_status_blocks_with_system_error(policy):
    checks = all_pass()
    checks[C.CHAIN_OF_CUSTODY] = make_check(
        "S1", C.CHAIN_OF_CUSTODY, CheckStatus.ERROR, (ReasonCode.CHECK_ERROR,)
    )
    assert evaluate(policy, checks).decision is PolicyDecision.SYSTEM_ERROR


def test_consent_fail_is_denied(policy):
    checks = all_pass()
    checks[C.CONSENT_VALIDITY] = make_check(
        "S1", C.CONSENT_VALIDITY, CheckStatus.FAIL, (ReasonCode.CONSENT_INVALID,)
    )
    ev = evaluate(policy, checks)
    assert ev.decision is PolicyDecision.DENIED
    assert ReasonCode.CONSENT_INVALID in ev.reason_codes


def test_missing_required_check_fails_closed(policy):
    checks = all_pass()
    del checks[C.LIMS_RECONCILIATION]
    ev = evaluate(policy, checks)
    assert ev.decision is PolicyDecision.DENIED
    assert ev.reason_codes == (ReasonCode.REQUIRED_CHECK_MISSING,)


def test_check_for_other_sample_does_not_count(policy):
    checks = all_pass()
    checks[C.IDENTITY_MATCH] = make_check("OTHER", C.IDENTITY_MATCH, CheckStatus.PASS)
    assert evaluate(policy, checks).decision is PolicyDecision.DENIED


def _temp_fail():
    checks = all_pass()
    checks[C.TEMPERATURE_REQUIREMENT] = make_check(
        "S1", C.TEMPERATURE_REQUIREMENT, CheckStatus.FAIL, (ReasonCode.TEMPERATURE_EXCURSION,)
    )
    return checks


def test_temperature_failure_without_human_requires_decision(policy):
    ev = evaluate(policy, _temp_fail())
    assert ev.decision is PolicyDecision.HUMAN_DECISION_REQUIRED
    assert ReasonCode.HUMAN_AUTHORITY_REQUIRED in ev.reason_codes


def test_exception_with_pi_approval_is_allowed(policy):
    ev = evaluate(
        policy,
        _temp_fail(),
        Disposition.ACCEPT_WITH_EXCEPTION,
        decision(ActorRole.PRINCIPAL_INVESTIGATOR, HumanOption.APPROVE_EXCEPTION),
    )
    assert ev.decision is PolicyDecision.ALLOWED
    assert ev.human_decision_id == "HD-1"


def test_exception_with_unauthorized_role_is_denied(policy):
    ev = evaluate(
        policy,
        _temp_fail(),
        Disposition.ACCEPT_WITH_EXCEPTION,
        decision(ActorRole.COORDINATOR, HumanOption.APPROVE_EXCEPTION),
    )
    assert ev.decision is PolicyDecision.DENIED
    assert ReasonCode.INSUFFICIENT_ROLE in ev.reason_codes


def test_exception_with_decision_for_other_sample_not_allowed(policy):
    ev = evaluate(
        policy,
        _temp_fail(),
        Disposition.ACCEPT_WITH_EXCEPTION,
        decision(ActorRole.PRINCIPAL_INVESTIGATOR, HumanOption.APPROVE_EXCEPTION, "S2"),
    )
    assert ev.decision is not PolicyDecision.ALLOWED


def test_exception_path_needs_temperature_failure(policy):
    ev = evaluate(
        policy,
        all_pass(),
        Disposition.ACCEPT_WITH_EXCEPTION,
        decision(ActorRole.PRINCIPAL_INVESTIGATOR, HumanOption.APPROVE_EXCEPTION),
    )
    assert ev.decision is PolicyDecision.DENIED


def test_exception_cannot_bypass_missing_evidence(policy):
    checks = _temp_fail()
    checks[C.CONSENT_VALIDITY] = make_check(
        "S1", C.CONSENT_VALIDITY, CheckStatus.UNAVAILABLE, (ReasonCode.CONSENT_ADDENDUM_MISSING,)
    )
    ev = evaluate(
        policy,
        checks,
        Disposition.ACCEPT_WITH_EXCEPTION,
        decision(ActorRole.PRINCIPAL_INVESTIGATOR, HumanOption.APPROVE_EXCEPTION),
    )
    assert ev.decision is PolicyDecision.WAITING_FOR_EVIDENCE


def test_barcode_collision_denies_accept_and_allows_quarantine(policy):
    checks = all_pass()
    checks[C.LIMS_RECONCILIATION] = make_check(
        "S1", C.LIMS_RECONCILIATION, CheckStatus.FAIL, (ReasonCode.BARCODE_COLLISION,)
    )
    assert evaluate(policy, checks).decision is PolicyDecision.DENIED
    q = evaluate(policy, checks, Disposition.QUARANTINE)
    assert q.decision is PolicyDecision.ALLOWED
    assert q.reason_codes == (ReasonCode.BARCODE_COLLISION,)


def test_quarantine_without_conflict_needs_human(policy):
    assert (
        evaluate(policy, all_pass(), Disposition.QUARANTINE).decision
        is PolicyDecision.HUMAN_DECISION_REQUIRED
    )


def test_quarantine_with_coordinator_decision_allowed(policy):
    ev = evaluate(
        policy, _temp_fail(), Disposition.QUARANTINE, decision(ActorRole.COORDINATOR, HumanOption.QUARANTINE)
    )
    assert ev.decision is PolicyDecision.ALLOWED


def test_quarantine_with_site_contact_role_denied(policy):
    ev = evaluate(
        policy, _temp_fail(), Disposition.QUARANTINE, decision(ActorRole.SITE_CONTACT, HumanOption.QUARANTINE)
    )
    assert ev.decision is PolicyDecision.DENIED


def _every_check_failing() -> dict:
    return {c: make_check("S1", c, CheckStatus.FAIL) for c in all_pass()}


def test_reject_is_never_automatic(policy):
    """Not even when every check fails. Rejection is irreversible, so it always needs a named human."""
    for checks in (all_pass(), _every_check_failing()):
        ev = evaluate(policy, checks, Disposition.REJECT)
        assert ev.decision is PolicyDecision.HUMAN_DECISION_REQUIRED
        assert ReasonCode.HUMAN_AUTHORITY_REQUIRED in ev.reason_codes


def test_reject_requires_the_option_and_the_role(policy):
    reject_role = policy.reject_roles[0]
    wrong_role = next(r for r in ActorRole if r not in policy.reject_roles)

    allowed = evaluate(
        policy, all_pass(), Disposition.REJECT, human=decision(reject_role, HumanOption.REJECT)
    )
    assert allowed.decision is PolicyDecision.ALLOWED
    assert ReasonCode.SPECIMEN_REJECTED in allowed.reason_codes

    unauthorised = evaluate(
        policy, all_pass(), Disposition.REJECT, human=decision(wrong_role, HumanOption.REJECT)
    )
    assert unauthorised.decision is PolicyDecision.DENIED
    assert ReasonCode.INSUFFICIENT_ROLE in unauthorised.reason_codes

    # A decision recorded for a different option cannot be reused to authorise a rejection.
    mismatched = evaluate(
        policy, all_pass(), Disposition.REJECT, human=decision(reject_role, HumanOption.QUARANTINE)
    )
    assert mismatched.decision is PolicyDecision.DENIED
