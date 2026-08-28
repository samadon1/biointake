"""The Shipment Verification Report, what the receiving lab owes the sending site.

This is not a feature we invented. ISBER Best Practices §J6 and §L4.5 already specify the artifact, its
name, and its contents: receipt date and time, tracking number, package and container condition including
visible signs of damage, the condition of the refrigerant, whether the container count matched, whether the
specimens received match those on the manifest, the discrepancies noted and any resolutions reached, and the
name of the person recording it. NCI Best Practices §C.2.10 adds the obligation plainly, any discrepancies,
damage or condition deviations "should be documented and reported immediately to the sender".

Every field is already recorded elsewhere in this system as a side effect of doing the work. The report is a
READ over those records, never a separate store, so it cannot drift from what actually happened. What a lab
would otherwise fill in by hand hours later, and often never send at all, is generated from the audit trail.

The one substitution: where the paper form has a handwritten resolution and a signature, this carries the
policy version that decided, the reason codes, and the attributed actor, which is strictly more than a
signature conveys.
"""

from __future__ import annotations

from typing import Any

from ..domain.enums import CaseState, SampleState, ScanOutcome
from ..domain.models import ShipmentCase
from ..repositories.interfaces import Repository
from .intake import IntakeService
from .intake_ramp import IntakeRampService

# ISBER §J6 lists what the report must cover. Keeping the clause against each section means a reviewer can
# check us against the standard rather than against our own opinion of what matters.
CLAUSES: dict[str, str] = {
    "receipt": "ISBER §J6, receipt date and time, tracking number, recording person",
    "condition": "ISBER §J6, package and container condition, refrigerant condition, container count",
    "reconciliation": "ISBER §J6, specimens received match those listed on the shipment manifest",
    "discrepancies": "ISBER §J6, discrepancies noted and any resolutions reached",
    "disposition": "ISO 20387 §7.3.2.2, acceptance criteria, verified upon acquisition/reception",
}


def build_verification_report(
    services: IntakeService, ramp: IntakeRampService, case_id: str
) -> dict[str, Any]:
    repo: Repository = services.repo
    case: ShipmentCase = repo.get_case(case_id)
    announcement = repo.get_announcement(case_id)
    receipt = repo.get_receipt(case_id)
    batch = ramp.batch_summary(case_id) if announcement else None
    samples = list(repo.list_samples(case_id))

    rows = batch["rows"] if batch else []
    near = [r for r in rows if r["outcome"] == ScanOutcome.NEAR_MATCH.value]
    quality_notes = [r for r in rows if (r.get("received_quality") or "ACCEPTABLE") != "ACCEPTABLE"]

    declared = len(announcement.expected_lines) if announcement else 0
    received = batch["scanned"] if batch else 0

    # A resolution is what the sender most wants to read, and it is the part a paper form leaves blank for
    # weeks. Each is attributed to whatever actually settled it: an authenticated sender attestation, a named
    # person's decision, or the policy engine reaching a determinate answer on its own.
    resolutions: list[dict[str, Any]] = []
    for decision in repo.list_decisions(case_id):
        resolutions.append(
            {
                "sample_id": decision.sample_id,
                "resolution": decision.selected_option.value,
                "settled_by": f"{decision.actor_id} ({decision.actor_role.value})",
                "comment": decision.comment,
                "at": decision.created_at.isoformat(),
            }
        )
    for request in repo.list_requests(case_id):
        resolutions.append(
            {
                "sample_id": ", ".join(sorted(request.affected_sample_ids)),
                "resolution": f"evidence request {request.status.value.lower()}",
                "settled_by": request.recipient_contact_id,
                "comment": request.subject,
                "at": request.sent_at.isoformat(),
            }
        )

    by_state: dict[str, list[str]] = {}
    for s in samples:
        by_state.setdefault(s.state.value, []).append(s.sample_id)

    return {
        "case_id": case_id,
        "shipment_id": case.shipment_id,
        "complete": case.state in (CaseState.COMPLETED, CaseState.FAILED),
        "case_state": case.state.value,
        "clauses": CLAUSES,
        "receipt": {
            "sending_site": announcement.sender_site_id if announcement else None,
            "announced_by": announcement.announced_by_contact_id if announcement else None,
            "courier": announcement.courier if announcement else "",
            "tracking_reference": announcement.tracking_reference if announcement else "",
            "received_at": receipt.received_at.isoformat() if receipt else None,
            "received_by": receipt.received_by_actor_id if receipt else None,
        },
        "condition": {
            "package_condition": receipt.package_condition.value if receipt else None,
            "condition_notes": receipt.condition_notes if receipt else "",
            "seal_intact": receipt.seal_intact if receipt else None,
            "refrigerant_condition": receipt.refrigerant_condition if receipt else "",
            "temperature_at_reception_c": receipt.temperature_at_reception_c if receipt else None,
            "containers_declared": announcement.container_count if announcement else None,
            "containers_received": receipt.package_count_received if receipt else None,
            "container_count_matched": bool(
                announcement and receipt and announcement.container_count == receipt.package_count_received
            ),
            "logger_files_received": len(receipt.logger_artifact_ids) if receipt else 0,
            "specimen_condition_notes": [
                {"sample_id": r["sample_id"], "received_quality": r["received_quality"]}
                for r in quality_notes
            ],
        },
        "reconciliation": {
            "declared": declared,
            "received": received,
            "matched": batch["matched"] if batch else 0,
            # A near-match counts against reconciliation. The tube read differently from the manifest and
            # nobody at the bench was entitled to decide which was right, so the row is not reconciled,
            # it is resolved later, by the sender, and that appears under resolutions rather than here.
            "manifest_fully_reconciled": bool(
                batch
                and not batch["not_scanned"]
                and not batch["unexpected"]
                and not batch["duplicates"]
                and not near
            ),
            "not_received": list(batch["not_scanned"]) if batch else [],
            "not_on_manifest": list(batch["unexpected"]) if batch else [],
            "duplicate_identifiers": list(batch["duplicates"]) if batch else [],
            "identifier_near_matches": [
                {"row": r["row"], "declared": r["sample_id"], "read_on_tube": r["scanned_value"]}
                for r in near
            ],
        },
        "resolutions": resolutions,
        "disposition": {
            "accepted": by_state.get(SampleState.ACCEPTED.value, []),
            "accepted_with_exception": by_state.get(SampleState.ACCEPTED_WITH_EXCEPTION.value, []),
            "held": by_state.get(SampleState.QUARANTINED.value, []),
            "rejected": by_state.get(SampleState.REJECTED.value, []),
            "still_open": [
                sid
                for state, ids in by_state.items()
                if state
                in (
                    SampleState.PENDING.value,
                    SampleState.WAITING_FOR_EVIDENCE.value,
                    SampleState.NEEDS_HUMAN_DECISION.value,
                )
                for sid in ids
            ],
            "policy": f"{services.policy.policy_id}@{services.policy.version}",
        },
    }
