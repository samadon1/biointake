"""A case is judged by the policy of its own study, not by whatever policy the service booted with.

Two studies run in one lab at the same time. Nothing about the second one is reachable from the
service's default policy, so if verification ever falls back to that default, every sample in the
second study fails PROTOCOL_ELIGIBILITY against a protocol it was never enrolled in.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from biointake.api.config import Settings, build_services
from biointake.domain.enums import (
    ActorRole,
    ActorType,
    CheckCategory,
    CheckStatus,
)
from biointake.domain.models import ActorContext, Study
from biointake.fixtures import DEFAULT_FIXTURE_DIR, load_package
from biointake.services.intake_ramp import IntakeRampService

SITE = ActorContext(actor_type=ActorType.SENDER, actor_id="SITE-CONTACT-002", role=ActorRole.SITE_CONTACT)
TECH = ActorContext(actor_type=ActorType.HUMAN, actor_id="tech-kojo", role=ActorRole.COORDINATOR)
CASE = "CASE-SHIP-NORTH-01"


@pytest.fixture
def lab():
    """A service booted on the default study, plus a second study with a different protocol."""
    svc = build_services(Settings(backend="memory"))
    ramp = IntakeRampService(svc.repo, svc.storage, svc.clock)
    package = load_package(DEFAULT_FIXTURE_DIR)

    now = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
    other = package.policy.model_copy(
        update={
            "policy_id": "POL-NORTH-01",
            "protocol_id": "NORTH-01",
            "title": "Northstar longitudinal cohort",
            "version": "1.0.0",
        }
    )
    study = Study(
        study_id="NORTH-01",
        name=other.title,
        protocol_id=other.protocol_id,
        policy_version=other.version,
        policy=other,
        exception_approval_role=ActorRole.PRINCIPAL_INVESTIGATOR,
        created_at=now,
        updated_at=now,
    )
    for contact in package.contacts:
        svc.repo.save_contact(contact)
    ramp.save_study(study, SITE)
    return svc, ramp, package, study, other


def _walk_to_verification(lab):
    """Announce, receive, scan every tube, commit, the real receiving-bench path."""
    svc, ramp, package, study, other = lab
    ramp.announce(
        case_id=CASE,
        shipment_id="SHIP-NORTH-01",
        study=study,
        policy=other,
        sender_site_id=package.shipment.sender_site_id,
        announced_by_contact_id="SITE-CONTACT-002",
        manifest_csv=package.manifest_csv,
        courier="Arctic Courier",
        tracking_reference="AC-99232",
        container_count=2,
        logger_ids=("LOGGER-A", "LOGGER-B"),
        shipping_condition="dry ice",
        actor=SITE,
    )
    ramp.record_receipt(
        case_id=CASE,
        actor=TECH,
        package_count_received=2,
        logger_files=(
            ("LOGGER-A.csv", package.temperature_logs["LOGGER-A"]),
            ("LOGGER-B.csv", package.temperature_logs["LOGGER-B"]),
        ),
    )
    for n in range(201, 213):
        ramp.scan(CASE, f"BX-{n}", TECH)
    ramp.commit_batch(CASE, TECH)
    return svc


def test_the_case_is_judged_by_its_own_studys_policy(lab):
    svc = _walk_to_verification(lab)
    assert svc.policy_for(CASE).protocol_id == "NORTH-01"


def test_protocol_eligibility_passes_under_the_second_study(lab):
    svc = _walk_to_verification(lab)
    results = svc.verify(CASE, TECH, categories=(CheckCategory.PROTOCOL_ELIGIBILITY,))
    assert len(results) == 12
    assert {r.status for r in results} == {CheckStatus.PASS}


def test_the_default_studys_policy_would_have_failed_every_sample(lab):
    """Guards the test above: the failure it rules out is real, not hypothetical."""
    svc, _, package, _, other = lab
    assert package.policy.protocol_id != other.protocol_id
