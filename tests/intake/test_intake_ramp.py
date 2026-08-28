"""The intake ramp: announcement → receipt → scanning → staging-batch commit (ADR 0004)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from biointake.clock import SteppingClock
from biointake.domain.enums import ActorRole, ActorType, CaseState, PackageCondition, ReasonCode, ScanOutcome
from biointake.domain.errors import PolicyDeniedError
from biointake.domain.models import ActorContext
from biointake.fixtures import DEFAULT_FIXTURE_DIR, load_package
from biointake.repositories.memory import InMemoryRepository
from biointake.services.intake_ramp import IntakeRampService
from biointake.storage.local import MemoryArtifactStorage

NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
SITE = ActorContext(actor_type=ActorType.SENDER, actor_id="SITE-CONTACT-002", role=ActorRole.SITE_CONTACT)
TECH = ActorContext(actor_type=ActorType.HUMAN, actor_id="tech-kojo", role=ActorRole.COORDINATOR)
CASE = "CASE-SHIP-DEMO-001"


@pytest.fixture
def ramp():
    clock = SteppingClock(NOW)
    repo = InMemoryRepository(clock)
    svc = IntakeRampService(repo, MemoryArtifactStorage(), clock)
    package = load_package(DEFAULT_FIXTURE_DIR)
    study = svc.ensure_default_study(package.policy)
    for contact in package.contacts:  # the lab's verified directory, seeded before any shipment
        repo.save_contact(contact)
    return svc, repo, package, study


def announce(ramp, manifest: bytes | None = None):
    svc, repo, package, study = ramp
    return svc.announce(
        case_id=CASE,
        shipment_id=package.shipment.shipment_id,
        study=study,
        policy=package.policy,
        sender_site_id=package.shipment.sender_site_id,
        announced_by_contact_id="SITE-CONTACT-002",
        manifest_csv=manifest if manifest is not None else package.manifest_csv,
        courier="Arctic Courier",
        tracking_reference="AC-99231",
        container_count=2,
        logger_ids=("LOGGER-A", "LOGGER-B"),
        shipping_condition="dry ice",
        actor=SITE,
    )


# ---------------------------------------------------------------- the manifest gate
def test_manifest_is_validated_before_anything_ships(ramp):
    svc, _, package, _ = ramp
    v = svc.validate_manifest(package.manifest_csv, package.policy)
    assert v.accepted and len(v.lines) == 12
    assert v.lines[6].sample_id == "BX-2O7"  # the site's own typo travels with the manifest, unaltered


def test_manifest_with_a_specimen_type_the_study_forbids_is_rejected(ramp):
    svc, _, package, _ = ramp
    bad = package.manifest_csv.replace(b"PLASMA", b"SERUM", 1)
    v = svc.validate_manifest(bad, package.policy)
    assert not v.accepted and ReasonCode.SPECIMEN_TYPE_NOT_IN_STUDY in v.reason_codes
    assert "SERUM" in v.summary


def test_forbidden_specimen_type_names_the_rows_it_is_on(ramp):
    """A site cannot fix "SERUM is not allowed" without hunting; it can fix "SERUM on row 2"."""
    svc, _, package, _ = ramp
    lines = package.manifest_csv.decode().splitlines()
    lines[2] = lines[2].replace("PLASMA", "SERUM")  # row 2
    lines[4] = lines[4].replace("PLASMA", "SERUM")  # row 4
    lines[3] = lines[3].replace("PLASMA", "URINE")  # row 3
    v = svc.validate_manifest("\n".join(lines).encode(), package.policy)
    assert not v.accepted and ReasonCode.SPECIMEN_TYPE_NOT_IN_STUDY in v.reason_codes
    problem = next(p for p in v.problems if "not permitted" in p)
    assert "SERUM on rows 2, 4" in problem, problem
    assert "URINE on row 3" in problem, problem


def test_malformed_manifest_names_the_required_columns(ramp):
    svc, _, package, _ = ramp
    v = svc.validate_manifest(b"row,sample_id\n1,BX-201\n", package.policy)
    assert not v.accepted and ReasonCode.MANIFEST_REJECTED in v.reason_codes
    assert "collection_timestamp" in v.summary


def test_duplicate_identifier_in_the_manifest_is_rejected(ramp):
    svc, _, package, _ = ramp
    lines = package.manifest_csv.decode().splitlines()
    lines[2] = lines[1]
    v = svc.validate_manifest("\n".join(lines).encode(), package.policy)
    assert not v.accepted and ReasonCode.LABEL_DUPLICATE in v.reason_codes


def test_a_rejected_manifest_opens_no_case(ramp):
    svc, repo, package, _ = ramp
    with pytest.raises(PolicyDeniedError):
        announce(ramp, manifest=package.manifest_csv.replace(b"PLASMA", b"SERUM", 1))
    assert repo.list_cases() == []


# ---------------------------------------------------------------- announcement
def test_announcement_opens_the_case_before_arrival(ramp):
    svc, repo, package, _ = ramp
    case, ann, v = announce(ramp)
    assert case.state is CaseState.ANNOUNCED
    assert case.expected_sample_count == 12 and case.observed_sample_count == 0
    assert repo.list_samples(CASE) == []  # nothing exists physically yet
    assert ann.courier == "Arctic Courier" and ann.logger_ids == ("LOGGER-A", "LOGGER-B")
    assert repo.get_artifact(ann.manifest_artifact_id).artifact_type.value == "MANIFEST"
    assert any(a.event_type.value == "SHIPMENT_ANNOUNCED" for a in repo.list_audit(CASE))


def test_the_same_shipment_cannot_be_announced_twice(ramp):
    announce(ramp)
    with pytest.raises(PolicyDeniedError) as e:
        announce(ramp)
    assert e.value.code is ReasonCode.SHIPMENT_ALREADY_ANNOUNCED


def test_scanning_before_receipt_is_refused(ramp):
    svc, _, _, _ = ramp
    announce(ramp)
    with pytest.raises(PolicyDeniedError):
        svc.scan(CASE, "BX-201", TECH)


# ---------------------------------------------------------------- receipt
def test_receipt_records_condition_and_ingests_logger_files(ramp):
    svc, repo, package, _ = ramp
    announce(ramp)
    receipt = svc.record_receipt(
        case_id=CASE,
        actor=TECH,
        package_condition=PackageCondition.ACCEPTABLE,
        package_count_received=2,
        refrigerant_condition="dry ice remaining ~2 kg",
        temperature_at_reception_c=-4.0,
        logger_files=(
            ("LOGGER-A.csv", package.temperature_logs["LOGGER-A"]),
            ("LOGGER-B.csv", package.temperature_logs["LOGGER-B"]),
        ),
    )
    assert receipt.received_by_actor_id == "tech-kojo" and receipt.package_count_expected == 2
    assert len(receipt.logger_artifact_ids) == 2
    assert repo.get_case(CASE).state is CaseState.RECEIVED
    loggers = {repo.get_artifact(a).metadata["logger_id"] for a in receipt.logger_artifact_ids}
    assert loggers == {"LOGGER-A", "LOGGER-B"}


def test_damaged_package_is_flagged_at_receipt(ramp):
    svc, repo, _, _ = ramp
    announce(ramp)
    svc.record_receipt(
        case_id=CASE,
        actor=TECH,
        package_condition=PackageCondition.DAMAGED_USABLE,
        condition_notes="outer box crushed",
        seal_intact=False,
    )
    event = next(a for a in repo.list_audit(CASE) if a.event_type.value == "SHIPMENT_RECEIVED")
    assert ReasonCode.PACKAGE_DAMAGED in event.reason_codes and "BROKEN" in event.summary


def test_receipt_is_recorded_once(ramp):
    svc, _, _, _ = ramp
    announce(ramp)
    svc.record_receipt(case_id=CASE, actor=TECH)
    with pytest.raises(PolicyDeniedError):
        svc.record_receipt(case_id=CASE, actor=TECH)


# ---------------------------------------------------------------- scanning
def _receive(ramp):
    svc, _, package, _ = ramp
    announce(ramp)
    svc.record_receipt(
        case_id=CASE,
        actor=TECH,
        package_count_received=2,
        logger_files=(
            ("LOGGER-A.csv", package.temperature_logs["LOGGER-A"]),
            ("LOGGER-B.csv", package.temperature_logs["LOGGER-B"]),
        ),
    )
    return svc


def test_the_manifest_defines_the_rows(ramp):
    svc = _receive(ramp)
    rows = svc.expected_rows(CASE)
    assert len(rows) == 12 and rows[0].sample_id == "BX-201" and all(r.scanned_value is None for r in rows)


def test_scan_outcomes(ramp):
    svc = _receive(ramp)
    assert svc.scan(CASE, "BX-201", TECH).outcome is ScanOutcome.MATCHED
    assert svc.scan(CASE, "BX-201", TECH).outcome is ScanOutcome.DUPLICATE
    assert svc.scan(CASE, "BX-999", TECH).outcome is ScanOutcome.UNEXPECTED
    near = svc.scan(CASE, "BX-207", TECH)  # the manifest says BX-2O7 (letter O)
    assert near.outcome is ScanOutcome.NEAR_MATCH and near.matched_row == 7
    assert "needs sender confirmation" in near.message


def test_near_match_is_never_silently_corrected(ramp):
    svc = _receive(ramp)
    svc.scan(CASE, "BX-207", TECH)
    row = next(r for r in svc.expected_rows(CASE) if r.row == 7)
    assert row.sample_id == "BX-2O7" and row.scanned_value == "BX-207"  # both identifiers survive


def test_batch_summary_counts_what_is_missing(ramp):
    svc = _receive(ramp)
    for n in list(range(201, 207)) + [208, 209, 210, 211]:
        svc.scan(CASE, f"BX-{n}", TECH)
    svc.scan(CASE, "BX-207", TECH)
    s = svc.batch_summary(CASE)
    assert s["expected"] == 12 and s["scanned"] == 11 and s["near_matches"] == 1
    assert s["not_scanned"] == ["BX-212"]


# ---------------------------------------------------------------- commit
def test_commit_creates_the_samples_the_agent_reconciles(ramp):
    svc, repo, _, _ = ramp
    svc = _receive(ramp)
    for n in range(201, 213):
        svc.scan(CASE, f"BX-{n}", TECH)
    samples, summary = svc.commit_batch(CASE, TECH)
    assert len(samples) == 12 and summary["not_scanned"] == []
    assert repo.get_case(CASE).state is CaseState.VERIFYING
    assert repo.get_case(CASE).observed_sample_count == 12
    by_id = {s.sample_id: s for s in repo.list_samples(CASE)}
    assert by_id["BX-207"].manifest_row == 7  # the near-matched tube keeps the identifier on the tube
    assert by_id["BX-212"].logger_id == "LOGGER-B" and by_id["BX-201"].logger_id == "LOGGER-A"
    assert by_id["BX-201"].participant_reference == "NS-P-0201"


def test_nothing_is_written_before_the_batch_is_committed(ramp):
    svc, repo, _, _ = ramp
    svc = _receive(ramp)
    for n in range(201, 213):
        svc.scan(CASE, f"BX-{n}", TECH)
    assert repo.list_samples(CASE) == []
    svc.commit_batch(CASE, TECH)
    assert len(repo.list_samples(CASE)) == 12


def test_partial_receipt_must_be_chosen_deliberately(ramp):
    svc, repo, _, _ = ramp
    svc = _receive(ramp)
    for n in range(201, 212):
        svc.scan(CASE, f"BX-{n}", TECH)
    with pytest.raises(PolicyDeniedError) as e:
        svc.commit_batch(CASE, TECH)
    assert e.value.code is ReasonCode.PARTIAL_RECEIPT and "BX-212" in str(e.value)
    samples, summary = svc.commit_batch(CASE, TECH, accept_partial=True)
    assert len(samples) == 11 and summary["not_scanned"] == ["BX-212"]
    event = next(a for a in repo.list_audit(CASE) if a.event_type.value == "STAGING_BATCH_COMMITTED")
    assert ReasonCode.PARTIAL_RECEIPT in event.reason_codes and "1 not received" in event.summary


def test_commit_requires_at_least_one_scan(ramp):
    svc = _receive(ramp)
    with pytest.raises(PolicyDeniedError):
        svc.commit_batch(CASE, TECH, accept_partial=True)
