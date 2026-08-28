"""Consent registry + admitted addenda → consent validity; protocol eligibility."""

from __future__ import annotations

import json
from datetime import date

from pydantic import BaseModel, ConfigDict

from ..domain.enums import CheckStatus, ReasonCode
from ..domain.policies import ConsentRule, ProtocolPolicy


class ConsentRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    participant_reference: str
    protocol_id: str
    consent_version: int
    scope: str
    effective_date: date
    status: str  # "ACTIVE" | "WITHDRAWN"
    notes: str = ""


class ConsentAddendum(BaseModel):
    """A structured addendum document. Untrusted until validated against the shipment."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    document: str
    protocol_id: str
    version: int
    scope: str
    signed_date: date
    site_id: str
    participants: tuple[str, ...]
    notes: str = ""


def parse_consent_records(data: bytes) -> list[ConsentRecord]:
    payload = json.loads(data)
    return [ConsentRecord.model_validate(r) for r in payload["records"]]


def parse_consent_addendum(data: bytes) -> ConsentAddendum:
    doc = ConsentAddendum.model_validate(json.loads(data))
    if doc.document != "CONSENT_ADDENDUM":
        raise ValueError("not a consent addendum document")
    return doc


class ConsentOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: CheckStatus
    reason_codes: tuple[ReasonCode, ...]
    observed: str
    expected: str
    evidence_refs: tuple[str, ...]
    summary: str


def evaluate_consent(
    participant_reference: str | None,
    protocol_id: str,
    records: list[ConsentRecord],
    addenda: list[tuple[str, ConsentAddendum]],  # (artifact_id, admitted addendum)
    rule: ConsentRule,
    registry_ref: str,
) -> ConsentOutcome:
    expected = f"{protocol_id} consent v{rule.min_version}+ scope {rule.required_scope}"
    if participant_reference is None:
        return ConsentOutcome(
            status=CheckStatus.UNAVAILABLE,
            reason_codes=(ReasonCode.CONSENT_RECORD_MISSING,),
            observed="no participant reference linked",
            expected=expected,
            evidence_refs=(registry_ref,),
            summary="Participant reference unknown; consent cannot be looked up.",
        )
    matches = [
        r
        for r in records
        if r.participant_reference == participant_reference and r.protocol_id == protocol_id
    ]
    if not matches:
        return ConsentOutcome(
            status=CheckStatus.UNAVAILABLE,
            reason_codes=(ReasonCode.CONSENT_RECORD_MISSING,),
            observed=f"no registry record for {participant_reference}",
            expected=expected,
            evidence_refs=(registry_ref,),
            summary=f"No consent record for {participant_reference} under {protocol_id}.",
        )
    record = max(matches, key=lambda r: r.consent_version)
    if record.status != "ACTIVE":
        return ConsentOutcome(
            status=CheckStatus.FAIL,
            reason_codes=(ReasonCode.CONSENT_INVALID,),
            observed=f"consent status {record.status}",
            expected=expected,
            evidence_refs=(registry_ref,),
            summary=f"Consent for {participant_reference} is {record.status}.",
        )
    if record.consent_version >= rule.min_version and record.scope == rule.required_scope:
        return ConsentOutcome(
            status=CheckStatus.PASS,
            reason_codes=(),
            observed=f"registry consent v{record.consent_version} scope {record.scope}",
            expected=expected,
            evidence_refs=(registry_ref,),
            summary=f"Registry consent v{record.consent_version} satisfies {rule.required_scope}.",
        )
    covering = [
        (aid, a)
        for aid, a in addenda
        if participant_reference in a.participants
        and a.protocol_id == protocol_id
        and a.version >= rule.min_version
        and a.scope == rule.required_scope
    ]
    if covering:
        aid, a = covering[0]
        return ConsentOutcome(
            status=CheckStatus.PASS,
            reason_codes=(),
            observed=f"registry v{record.consent_version} + addendum v{a.version} ({a.signed_date})",
            expected=expected,
            evidence_refs=(registry_ref, aid),
            summary=f"Addendum v{a.version} covers {participant_reference}.",
        )
    return ConsentOutcome(
        status=CheckStatus.UNAVAILABLE,
        reason_codes=(ReasonCode.CONSENT_ADDENDUM_MISSING,),
        observed=f"registry consent v{record.consent_version}; no admitted addendum",
        expected=expected,
        evidence_refs=(registry_ref,),
        summary=f"Consent addendum v{rule.min_version}+ required for {participant_reference}; not on file.",
    )


def evaluate_protocol_eligibility(
    expected_protocol_id: str, specimen_type: str, policy: ProtocolPolicy, protocol_ref: str
) -> ConsentOutcome:
    problems = []
    if expected_protocol_id != policy.protocol_id:
        problems.append(f"protocol {expected_protocol_id} ≠ {policy.protocol_id}")
    if specimen_type.upper() not in policy.allowed_specimen_types:
        problems.append(f"specimen type {specimen_type} not in {list(policy.allowed_specimen_types)}")
    if problems:
        return ConsentOutcome(
            status=CheckStatus.FAIL,
            reason_codes=(ReasonCode.PROTOCOL_MISMATCH,),
            observed="; ".join(problems),
            expected=f"{policy.protocol_id} / {', '.join(policy.allowed_specimen_types)}",
            evidence_refs=(protocol_ref,),
            summary="Sample is not eligible under the protocol policy.",
        )
    return ConsentOutcome(
        status=CheckStatus.PASS,
        reason_codes=(),
        observed=f"{expected_protocol_id} / {specimen_type.upper()}",
        expected=f"{policy.protocol_id} / {', '.join(policy.allowed_specimen_types)}",
        evidence_refs=(protocol_ref,),
        summary="Protocol and specimen type eligible.",
    )
