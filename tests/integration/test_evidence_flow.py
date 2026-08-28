"""Evidence requests and admission: contacts, tokens, partial evidence, duplicates, ordering."""

from __future__ import annotations

import hashlib
import json

import pytest

from biointake.domain.commands import CreateEvidenceRequestCommand, RaisePendingDecisionCommand
from biointake.domain.enums import (
    AuditEventType,
    CaseState,
    CheckCategory,
    Disposition,
    ReasonCode,
    RequirementType,
    SampleState,
)
from biointake.domain.errors import EvidenceRejectedError, PolicyDeniedError, RecipientNotVerifiedError
from biointake.domain.models import EvidenceRequirement
from conftest import AGENT, addendum_artifact, apply_plan, dispose, next_op, receive, row7_correction


def _reqs(demo, *pairs):
    return tuple(EvidenceRequirement(requirement_type=t, sample_id=s, description="d") for t, s in pairs)


def test_request_goes_to_verified_contact_only(at_checkpoint_1):
    d = at_checkpoint_1
    reqs = _reqs(d, (RequirementType.CONSENT_ADDENDUM, "BX-209"))
    for bad in (
        "nobody@example.com",
        "SITE-CONTACT-009",
        "SITE-CONTACT-003",
    ):  # unknown / other shipment / inactive
        with pytest.raises(RecipientNotVerifiedError):
            d.svc.create_evidence_request(
                CreateEvidenceRequestCommand(
                    operation_id=next_op(),
                    case_id=d.case_id,
                    actor=AGENT,
                    recipient_contact_id=bad,
                    requirements=reqs,
                )
            )


def test_duplicate_active_request_same_fingerprint_is_refused(at_checkpoint_1):
    d = at_checkpoint_1
    existing = d.repo.get_request(d.request_id)
    with pytest.raises(PolicyDeniedError) as e:
        d.svc.create_evidence_request(
            CreateEvidenceRequestCommand(
                operation_id=next_op(),
                case_id=d.case_id,
                actor=AGENT,
                recipient_contact_id="SITE-CONTACT-001",
                requirements=existing.requirements,
            )
        )
    assert e.value.code is ReasonCode.DUPLICATE_EVIDENCE_REQUEST


def test_cannot_request_evidence_that_is_not_missing(at_checkpoint_1):
    d = at_checkpoint_1
    with pytest.raises(PolicyDeniedError):
        d.svc.create_evidence_request(
            CreateEvidenceRequestCommand(
                operation_id=next_op(),
                case_id=d.case_id,
                actor=AGENT,
                recipient_contact_id="SITE-CONTACT-002",
                requirements=_reqs(d, (RequirementType.CONSENT_ADDENDUM, "BX-201")),
            )
        )


def test_case_waits_for_evidence_even_though_a_sample_needs_a_human(at_checkpoint_1):
    d = at_checkpoint_1
    assert d.repo.get_sample("BX-212").state is SampleState.NEEDS_HUMAN_DECISION
    assert d.repo.get_case(d.case_id).state is CaseState.WAITING_FOR_EVIDENCE
    with pytest.raises(PolicyDeniedError) as e:
        d.svc.raise_pending_decision(
            RaisePendingDecisionCommand(
                operation_id=next_op(), case_id=d.case_id, actor=AGENT, sample_id="BX-212"
            )
        )
    assert e.value.code is ReasonCode.EVIDENCE_RECOVERY_IN_PROGRESS
    assert d.repo.list_pending_decisions(d.case_id) == []


def test_addendum_alone_clears_209_210_but_not_207(at_checkpoint_1, package):
    d = at_checkpoint_1
    res = receive(d, artifacts=(addendum_artifact(package),))
    assert res.status == "ok"
    assert set(res.data["satisfied_requirement_keys"]) == {
        "CONSENT_ADDENDUM:BX-209",
        "CONSENT_ADDENDUM:BX-210",
    }
    assert res.data["remaining_requirement_keys"] == ["MANIFEST_CORRECTION:BX-207"]
    assert len(res.data["invalidated_check_ids"]) == 2
    apply_plan(d, res)
    assert dispose(d, "BX-209").status == "ok"
    assert dispose(d, "BX-210").status == "ok"
    r = dispose(d, "BX-207")
    assert r.status == "waiting" and set(r.data["blocking_checks"]) == {"IDENTITY_MATCH", "MANIFEST_MATCH"}
    assert d.repo.get_request(d.request_id).status.value == "ACTIVE"
    assert d.svc.recompute_case_state(d.case_id, AGENT).state is CaseState.WAITING_FOR_EVIDENCE


def test_attestation_alone_clears_207_but_not_209_210(at_checkpoint_1):
    d = at_checkpoint_1
    res = receive(d, corrections=(row7_correction(),))
    assert res.data["satisfied_requirement_keys"] == ["MANIFEST_CORRECTION:BX-207"]
    assert len(res.data["invalidated_check_ids"]) == 2
    apply_plan(d, res)
    assert dispose(d, "BX-207").status == "ok"
    assert dispose(d, "BX-209").status == "waiting"
    assert dispose(d, "BX-210").status == "waiting"


def test_207_cannot_pass_without_attestation(at_checkpoint_1):
    d = at_checkpoint_1
    d.svc.verify(d.case_id, AGENT, sample_ids=("BX-207",))
    r = dispose(d, "BX-207")
    assert r.status == "waiting" and ReasonCode.MANIFEST_IDENTIFIER_NEAR_MATCH in r.reason_codes


def test_attestation_from_unauthorized_contact_is_rejected(at_checkpoint_1, package):
    d = at_checkpoint_1
    with pytest.raises(EvidenceRejectedError) as e:  # verified for the shipment, but not the addressee
        receive(d, corrections=(row7_correction(),), contact="SITE-CONTACT-001")
    assert e.value.code is ReasonCode.UNAUTHORIZED_ATTESTATION
    with pytest.raises(RecipientNotVerifiedError):  # not associated with this shipment
        receive(d, corrections=(row7_correction(),), contact="SITE-CONTACT-009")
    with pytest.raises(RecipientNotVerifiedError):  # inactive
        receive(d, corrections=(row7_correction(),), contact="SITE-CONTACT-003")
    assert d.repo.get_request(d.request_id).satisfied_requirement_keys == ()


def test_wrong_token_and_bad_checksum_are_rejected(at_checkpoint_1, package):
    d = at_checkpoint_1
    with pytest.raises(EvidenceRejectedError) as e:
        receive(d, artifacts=(addendum_artifact(package),), token="not-the-token")
    assert e.value.code is ReasonCode.UPLOAD_TOKEN_INVALID
    res = receive(
        d, artifacts=(addendum_artifact(package, declared_sha256=hashlib.sha256(b"other").hexdigest()),)
    )
    assert res.status == "denied" and ReasonCode.EVIDENCE_CHECKSUM_MISMATCH in res.reason_codes
    assert res.data["admitted_artifact_ids"] == []


def test_contradictory_correction_is_rejected(at_checkpoint_1):
    d = at_checkpoint_1
    from biointake.domain.commands import ProposedCorrection

    bad = ProposedCorrection(
        manifest_row=7, manifest_value="BX-2O7", corrected_value="BX-208"
    )  # would collide
    res = receive(d, corrections=(bad,))
    assert res.status == "denied" and ReasonCode.EVIDENCE_CONTRADICTORY in res.reason_codes
    wrong_row = ProposedCorrection(manifest_row=8, manifest_value="BX-208", corrected_value="BX-207")
    assert receive(d, corrections=(wrong_row,)).status == "denied"


def test_contradictory_addendum_is_rejected(at_checkpoint_1, package):
    d = at_checkpoint_1
    doc = json.loads(package.later["consent-addendum.json"])
    doc["protocol_id"] = "PROTO-017"
    res = receive(d, artifacts=(addendum_artifact(package, json.dumps(doc).encode()),))
    assert res.status == "denied" and ReasonCode.EVIDENCE_CONTRADICTORY in res.reason_codes
    doc = json.loads(package.later["consent-addendum.json"])
    doc["version"] = 2
    assert receive(d, artifacts=(addendum_artifact(package, json.dumps(doc).encode()),)).status == "denied"


def test_duplicate_evidence_event_is_replayed_not_reapplied(at_checkpoint_1, package):
    d = at_checkpoint_1
    op = next_op()
    first = receive(d, artifacts=(addendum_artifact(package),), corrections=(row7_correction(),), op=op)
    n_artifacts = len(d.repo.list_artifacts(d.case_id))
    second = receive(d, artifacts=(addendum_artifact(package),), corrections=(row7_correction(),), op=op)
    assert first == second
    assert len(d.repo.list_artifacts(d.case_id)) == n_artifacts
    received = [a for a in d.repo.list_audit(d.case_id) if a.event_type is AuditEventType.EVIDENCE_RECEIVED]
    assert len(received) == 1
    # a *new* event against the now-satisfied request is refused outright
    with pytest.raises(EvidenceRejectedError) as e:
        receive(d, artifacts=(addendum_artifact(package),))
    assert e.value.code is ReasonCode.REQUEST_NOT_ACTIVE


def test_reverify_touches_only_affected_checks(at_checkpoint_1, package):
    d = at_checkpoint_1
    before = {(c.sample_id, c.category): c.check_id for c in d.repo.current_checks(d.case_id)}
    res = receive(d, artifacts=(addendum_artifact(package),), corrections=(row7_correction(),))
    applied = apply_plan(d, res)
    assert len(applied.data["produced_check_ids"]) == 4 and applied.data["total_check_slots"] == 84
    after = {(c.sample_id, c.category): c.check_id for c in d.repo.current_checks(d.case_id)}
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {
        ("BX-207", CheckCategory.IDENTITY_MATCH),
        ("BX-207", CheckCategory.MANIFEST_MATCH),
        ("BX-209", CheckCategory.CONSENT_VALIDITY),
        ("BX-210", CheckCategory.CONSENT_VALIDITY),
    }


def test_quarantine_of_barcode_collision_never_needs_a_human(at_checkpoint_1):
    d = at_checkpoint_1
    assert d.repo.get_sample("BX-211").state is SampleState.QUARANTINED
    assert d.repo.get_sample("BX-211").disposition is Disposition.QUARANTINE
    assert d.repo.list_decisions(d.case_id) == []
