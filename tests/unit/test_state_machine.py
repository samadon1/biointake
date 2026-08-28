from __future__ import annotations

import pytest
from pydantic import ValidationError

from biointake.clock import SteppingClock
from biointake.domain.enums import CaseState, ReasonCode, SampleState
from biointake.domain.errors import CaseFinalizedError, InvalidTransitionError
from biointake.domain.models import Sample, ShipmentCase
from biointake.domain.state_machine import TransitionService
from biointake.repositories.memory import InMemoryRepository
from conftest import AGENT, NOW


@pytest.fixture
def env():
    clock = SteppingClock(NOW)
    repo = InMemoryRepository(clock)
    case = ShipmentCase(
        case_id="C1",
        shipment_id="S",
        protocol_id="P",
        protocol_version="1",
        sender_site_id="site",
        received_at=NOW,
        agent_session_id="x" * 36,
        expected_sample_count=1,
        created_at=NOW,
        updated_at=NOW,
    )
    repo.save_case(case)
    repo.save_sample(
        Sample(
            sample_id="BX-1",
            case_id="C1",
            barcode="b",
            specimen_type="PLASMA",
            container_id="BOX",
            expected_protocol_id="P",
            updated_at=NOW,
        )
    )
    return repo, TransitionService(repo, clock)


def test_models_are_frozen_direct_assignment_is_impossible(env):
    repo, _ = env
    sample = repo.get_sample("BX-1")
    with pytest.raises(ValidationError):
        sample.state = SampleState.ACCEPTED  # type: ignore[misc]
    case = repo.get_case("C1")
    with pytest.raises(ValidationError):
        case.state = CaseState.COMPLETED  # type: ignore[misc]
    assert repo.get_sample("BX-1").state is SampleState.PENDING


def test_invalid_sample_transition_rejected(env):
    _, ts = env
    with pytest.raises(InvalidTransitionError):
        ts.transition_sample(
            "BX-1",
            SampleState.ACCEPTED_WITH_EXCEPTION,
            AGENT,
            ReasonCode.ALL_CHECKS_PASS,
            policy_evaluation_id="PE",
        )


def test_terminal_disposition_requires_policy_evaluation_id(env):
    _, ts = env
    with pytest.raises(InvalidTransitionError):
        ts.transition_sample("BX-1", SampleState.ACCEPTED, AGENT, ReasonCode.ALL_CHECKS_PASS)


def test_valid_transition_bumps_case_version_and_audits(env):
    repo, ts = env
    v0 = repo.get_case("C1").case_version
    s = ts.transition_sample(
        "BX-1", SampleState.ACCEPTED, AGENT, ReasonCode.ALL_CHECKS_PASS, policy_evaluation_id="PE-1"
    )
    assert s.state is SampleState.ACCEPTED and s.disposition is not None
    assert repo.get_case("C1").case_version == v0 + 1
    events = repo.list_audit("C1")
    assert events[-1].metadata["policy_evaluation_id"] == "PE-1"
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))


def test_terminal_sample_cannot_transition_again(env):
    _, ts = env
    ts.transition_sample(
        "BX-1", SampleState.QUARANTINED, AGENT, ReasonCode.BARCODE_COLLISION, policy_evaluation_id="PE-1"
    )
    with pytest.raises(InvalidTransitionError):
        ts.transition_sample(
            "BX-1", SampleState.ACCEPTED, AGENT, ReasonCode.ALL_CHECKS_PASS, policy_evaluation_id="PE-2"
        )


def test_case_transition_table(env):
    _, ts = env
    ts.transition_case("C1", CaseState.VERIFYING, AGENT, ReasonCode.ALL_CHECKS_PASS)
    with pytest.raises(InvalidTransitionError):
        ts.transition_case("C1", CaseState.CREATED, AGENT, ReasonCode.ALL_CHECKS_PASS)
    ts.transition_case("C1", CaseState.COMPLETED, AGENT, ReasonCode.ALL_CHECKS_PASS)
    with pytest.raises(CaseFinalizedError):
        ts.transition_case("C1", CaseState.VERIFYING, AGENT, ReasonCode.ALL_CHECKS_PASS)
    with pytest.raises(CaseFinalizedError):
        ts.transition_sample(
            "BX-1", SampleState.ACCEPTED, AGENT, ReasonCode.ALL_CHECKS_PASS, policy_evaluation_id="PE"
        )


def test_rejection_is_reachable_only_where_a_human_decides():
    """REJECTED must not be arrivable from PENDING or WAITING_FOR_EVIDENCE: those are states the agent moves
    through on its own, and nothing the agent does may destroy a specimen."""
    from biointake.domain.state_machine import SAMPLE_TRANSITIONS

    reachable_from = {src for src, targets in SAMPLE_TRANSITIONS.items() if SampleState.REJECTED in targets}
    assert reachable_from == {SampleState.NEEDS_HUMAN_DECISION, SampleState.QUARANTINED}


def test_quarantine_is_a_hold_that_can_never_be_promoted_to_accepted():
    """A quarantine review may return a specimen for re-verification or reject it. It may not accept it:
    that would let a human authorise an acceptance the policy engine never sanctioned."""
    from biointake.domain.state_machine import SAMPLE_TRANSITIONS

    out = SAMPLE_TRANSITIONS[SampleState.QUARANTINED]
    assert out == {SampleState.PENDING, SampleState.REJECTED}
    assert SampleState.ACCEPTED not in out and SampleState.ACCEPTED_WITH_EXCEPTION not in out
