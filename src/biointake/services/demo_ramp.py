"""Play the demonstration shipment through the real intake ramp.

The fixture is the *sending site's* data, the manifest a coordinator would upload and the logger files a
technician would pull off the loggers, not the receiving lab's pre-loaded truth. Replaying it through
announcement, receipt and scanning means the demo exercises the same path a lab would, and a viewer sees the
work rather than its result.
"""

from __future__ import annotations

from typing import Any

from ..domain.enums import ActorRole, ActorType, PackageCondition
from ..domain.models import ActorContext, Sample
from ..fixtures import ShipmentPackage
from ..services.intake_ramp import IntakeRampService
from ..services.lims_demo import parse_lims_records
from .intake import IntakeService

SITE = ActorContext(actor_type=ActorType.SENDER, actor_id="SITE-CONTACT-002", role=ActorRole.SITE_CONTACT)
TECH = ActorContext(actor_type=ActorType.HUMAN, actor_id="tech-kojo-mensah", role=ActorRole.COORDINATOR)


def play_demo_through_ramp(
    services: IntakeService, ramp: IntakeRampService, package: ShipmentPackage, case_id: str
) -> dict[str, Any]:
    repo = services.repo
    info = package.shipment
    study = ramp.ensure_default_study(package.policy)

    # The verified contact directory and the demonstration LIMS belong to the receiving lab, so they exist
    # before any shipment arrives, unlike the manifest, which the site supplies.
    for contact in package.contacts:
        repo.save_contact(contact)
    services.lims.seed(
        package.lims_records if package.lims_records else parse_lims_records(b'{"records": []}')
    )

    # 1. the site announces the shipment and uploads its manifest
    case, announcement, validation = ramp.announce(
        case_id=case_id,
        shipment_id=info.shipment_id,
        study=study,
        policy=package.policy,
        sender_site_id=info.sender_site_id,
        announced_by_contact_id="SITE-CONTACT-002",
        manifest_csv=package.manifest_csv,
        courier="Arctic Cold Chain",
        tracking_reference="ACC-2026-08-0412",
        container_count=len(info.containers),
        logger_ids=tuple(c.logger_id for c in info.containers),
        shipping_condition="dry ice",
        actor=SITE,
    )

    # 2. the box arrives: custody, condition, refrigerant, and the logger files come off the loggers
    ramp.record_receipt(
        case_id=case_id,
        actor=TECH,
        package_condition=PackageCondition.ACCEPTABLE,
        package_count_received=len(info.containers),
        refrigerant_condition="dry ice remaining, approx. 2 kg",
        temperature_at_reception_c=-6.0,
        seal_intact=True,
        logger_files=tuple(
            (f"{logger_id}.csv", data) for logger_id, data in sorted(package.temperature_logs.items())
        ),
    )

    # 3. every tube is scanned against the manifest-derived rows. The scanner reads what is on the tube,
    #    including BX-207, which the manifest declares as BX-2O7. That near-match is recorded, not corrected.
    from ..services.manifest import parse_scanner_export

    export = parse_scanner_export(package.scanner_export_json)
    for scan in export.scans:
        ramp.scan(case_id, scan.sample_id, TECH, container_id=scan.container_id, encoded_barcode=scan.barcode)

    # 4. the staging batch is committed: only now do the samples exist
    samples, summary = ramp.commit_batch(case_id, TECH)
    _attach_receiving_lab_evidence(services, package, case_id)
    return {
        "case_id": case.case_id,
        "session_id": case.agent_session_id,
        "state": repo.get_case(case_id).state.value,
        "declared": len(validation.lines),
        "scanned": summary["scanned"],
        "committed": len(samples),
        "near_matches": summary["near_matches"],
    }


def _attach_receiving_lab_evidence(services: IntakeService, package: ShipmentPackage, case_id: str) -> None:
    """Attach the records the receiving lab already holds: the protocol it runs, the consent registry it
    maintains and the custody log that travelled with the shipment. These are not scanned; they are the
    lab's own reference data, which is why they arrive here rather than through the bench."""
    from ..domain.enums import ArtifactType

    for artifact_type, filename, mime, data in (
        (ArtifactType.SCANNER_EXPORT, "scanner-export.json", "application/json", package.scanner_export_json),
        (
            ArtifactType.PROTOCOL,
            f"{package.shipment.protocol_id}.json",
            "application/json",
            package.protocol_json,
        ),
        (
            ArtifactType.CONSENT_RECORDS,
            "consent-records.json",
            "application/json",
            package.consent_records_json,
        ),
        (ArtifactType.CUSTODY_LOG, "chain-of-custody.json", "application/json", package.custody_log_json),
    ):
        services._store(case_id, artifact_type, filename, mime, data)


def demo_samples(services: IntakeService, case_id: str) -> list[Sample]:
    return list(services.repo.list_samples(case_id))
