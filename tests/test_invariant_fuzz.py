"""10,000 random check-status vectors: ALLOWED for ACCEPT never appears unless every required check is PASS."""

from __future__ import annotations

import random

from biointake.domain.disposition import DispositionEngine
from biointake.domain.enums import (
    ActorRole,
    CheckCategory,
    CheckStatus,
    Disposition,
    HumanOption,
    PolicyDecision,
)
from biointake.domain.models import HumanDecision
from conftest import NOW, make_check

ITERATIONS = 10_000


def test_accept_never_allowed_unless_all_pass(policy):
    rng = random.Random(20260827)
    engine = DispositionEngine(policy)
    # PASS-heavy sampling so all-PASS vectors occur often enough to exercise the ALLOWED path.
    statuses = [CheckStatus.PASS] * 12 + [s for s in CheckStatus if s is not CheckStatus.PASS]
    allowed_seen = 0
    for i in range(ITERATIONS):
        checks = {}
        for cat in CheckCategory:
            if rng.random() < 0.03:
                continue  # missing check
            checks[cat] = make_check("S1", cat, rng.choice(statuses))
        ev = engine.evaluate(
            evaluation_id=f"PE-{i}",
            case_id="CASE-T",
            sample_id="S1",
            checks=checks,
            requested=Disposition.ACCEPT,
            human_decision=None,
            now=NOW,
        )
        every_pass = all(c in checks and checks[c].status is CheckStatus.PASS for c in policy.required_checks)
        if ev.decision is PolicyDecision.ALLOWED:
            allowed_seen += 1
            assert every_pass, (
                f"ALLOWED with non-PASS checks at iteration {i}: {[(c.value, r.status.value) for c, r in checks.items()]}"
            )
        if every_pass:
            assert ev.decision is PolicyDecision.ALLOWED
    assert allowed_seen > 0  # the fuzz actually exercised the allowed path


def test_exception_never_allowed_with_non_temperature_blockers(policy):
    rng = random.Random(7)
    engine = DispositionEngine(policy)
    pi = HumanDecision(
        decision_id="HD",
        case_id="CASE-T",
        issue_id="i",
        sample_id="S1",
        actor_id="pi",
        actor_role=ActorRole.PRINCIPAL_INVESTIGATOR,
        selected_option=HumanOption.APPROVE_EXCEPTION,
        operation_id="op",
        created_at=NOW,
    )
    for i in range(ITERATIONS // 4):
        pool = [CheckStatus.PASS] * 12 + [s for s in CheckStatus if s is not CheckStatus.PASS]
        checks = {cat: make_check("S1", cat, rng.choice(pool)) for cat in CheckCategory}
        ev = engine.evaluate(
            evaluation_id=f"PE-{i}",
            case_id="CASE-T",
            sample_id="S1",
            checks=checks,
            requested=Disposition.ACCEPT_WITH_EXCEPTION,
            human_decision=pi,
            now=NOW,
        )
        others_pass = all(
            checks[c].status is CheckStatus.PASS
            for c in CheckCategory
            if c is not CheckCategory.TEMPERATURE_REQUIREMENT
        )
        temp_fail = checks[CheckCategory.TEMPERATURE_REQUIREMENT].status is CheckStatus.FAIL
        assert (ev.decision is PolicyDecision.ALLOWED) == (others_pass and temp_fail)
