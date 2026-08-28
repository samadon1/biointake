"""Evidence requests (outbound) and evidence admission (inbound).

Outbound: the request goes only to a verified site contact, consolidates real unresolved requirements,
and is fingerprinted so the same requirement set cannot be requested twice while active.

Inbound: uploads are validated (token, contact, MIME, size, checksum) and then *admitted* only if the
content actually matches the shipment. A sender's free-text correction becomes authoritative only
after the deterministic admissibility checks here, never because a model believed it.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from ..clock import Clock
from ..domain.commands import IncomingArtifact, ProposedCorrection
from ..domain.enums import (
    ArtifactType,
    ArtifactValidation,
    CheckCategory,
    CheckStatus,
    EvidenceRequestStatus,
    ReasonCode,
    RequirementType,
)
from ..domain.errors import EvidenceRejectedError, PolicyDeniedError
from ..domain.models import (
    EvidenceArtifact,
    EvidenceRequest,
    EvidenceRequirement,
    Sample,
    ShipmentCase,
    SiteContact,
)
from ..repositories.interfaces import Repository
from ..storage.interfaces import ArtifactStorage, sha256_hex
from . import consent as consent_svc
from . import custody as custody_svc
from .contacts import ContactDirectory
from .verification import CaseContext

MIME_ALLOWLIST = frozenset({"application/json", "text/csv", "text/plain", "application/pdf"})
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
REQUEST_TTL = timedelta(days=7)

REQUIREMENT_CATEGORIES: dict[RequirementType, tuple[CheckCategory, ...]] = {
    RequirementType.CONSENT_ADDENDUM: (CheckCategory.CONSENT_VALIDITY,),
    RequirementType.MANIFEST_CORRECTION: (CheckCategory.IDENTITY_MATCH, CheckCategory.MANIFEST_MATCH),
    RequirementType.CUSTODY_RECORD: (CheckCategory.CHAIN_OF_CUSTODY,),
    RequirementType.CONSENT_REGISTRY: (CheckCategory.CONSENT_VALIDITY,),
    RequirementType.CUSTODY_LOG: (CheckCategory.CHAIN_OF_CUSTODY,),
}

UNRESOLVED = frozenset({CheckStatus.UNAVAILABLE, CheckStatus.AMBIGUOUS})


SATISFYING_ARTIFACT: dict[RequirementType, ArtifactType] = {
    RequirementType.CONSENT_REGISTRY: ArtifactType.CONSENT_RECORDS,
    RequirementType.CUSTODY_LOG: ArtifactType.CUSTODY_LOG,
}


def requirements_fingerprint(requirements: tuple[EvidenceRequirement, ...]) -> str:
    keys = sorted(r.key() for r in requirements)
    return hashlib.sha256("|".join(keys).encode()).hexdigest()[:24]


def default_token_factory() -> str:
    return secrets.token_urlsafe(24)


class Rejection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    filename: str
    reason_code: ReasonCode
    detail: str


class ReceiveOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    admitted_artifact_ids: tuple[str, ...]
    rejections: tuple[Rejection, ...]
    satisfied_requirement_keys: tuple[str, ...]
    remaining_requirement_keys: tuple[str, ...]
    affected_checks: tuple[tuple[str, CheckCategory], ...]
    request_status: EvidenceRequestStatus
    untrusted_text_excerpts: tuple[str, ...] = Field(default_factory=tuple)


class EvidenceService:
    def __init__(
        self,
        repo: Repository,
        storage: ArtifactStorage,
        contacts: ContactDirectory,
        clock: Clock,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repo = repo
        self._storage = storage
        self._contacts = contacts
        self._clock = clock
        self._token_factory = token_factory or default_token_factory

    # -- outbound --------------------------------------------------------------------------------
    def _unresolved_in(self, sample_id: str, case_id: str, cats: tuple[CheckCategory, ...]) -> bool:
        current = self._repo.checks_by_category(sample_id, case_id)
        return any(c in current and current[c].status in UNRESOLVED for c in cats)

    def build_request(
        self,
        case: ShipmentCase,
        recipient_contact_id: str,
        requirements: tuple[EvidenceRequirement, ...],
        note_for_recipient: str = "",
    ) -> tuple[EvidenceRequest, SiteContact]:
        if not requirements:
            raise PolicyDeniedError("an evidence request needs at least one requirement")
        contact = self._contacts.resolve(recipient_contact_id, case.shipment_id)  # raises if not verified
        sample_ids = {s.sample_id for s in self._repo.list_samples(case.case_id)}
        blocked: dict[str, tuple[str, ...]] = {}
        for req in requirements:
            if not req.is_shipment_wide and req.sample_id not in sample_ids:
                raise PolicyDeniedError(f"requirement references unknown sample {req.sample_id}")
            targets = sorted(sample_ids) if req.is_shipment_wide else [req.sample_id]
            cats = REQUIREMENT_CATEGORIES[req.requirement_type]
            waiting = tuple(sid for sid in targets if self._unresolved_in(sid, case.case_id, cats))
            if not waiting:
                raise PolicyDeniedError(
                    f"{req.key()} is not an unresolved requirement; refusing to request evidence that is not missing"
                )
            blocked[req.key()] = waiting
        fp = requirements_fingerprint(requirements)
        for existing in self._repo.list_requests(case.case_id, EvidenceRequestStatus.ACTIVE):
            if existing.fingerprint == fp:
                raise PolicyDeniedError(
                    f"an active request with the same requirements already exists ({existing.request_id})",
                    code=ReasonCode.DUPLICATE_EVIDENCE_REQUEST,
                )
        now = self._clock()
        affected = tuple(sorted({sid for w in blocked.values() for sid in w}))
        remaining = len(sample_ids) - len(affected)
        request = EvidenceRequest(
            request_id=self._repo.next_id("REQ"),
            case_id=case.case_id,
            recipient_contact_id=contact.contact_id,
            requirements=tuple(sorted(requirements, key=lambda r: r.key())),
            affected_sample_ids=affected,
            fingerprint=fp,
            upload_token=self._token_factory(),
            subject=f"Additional evidence required for {case.shipment_id}",
            body=self._draft_body(case, contact, requirements, affected, remaining, note_for_recipient),
            sent_at=now,
            expires_at=now + REQUEST_TTL,
        )
        return request, contact

    @staticmethod
    def _draft_body(
        case: ShipmentCase,
        contact: SiteContact,
        requirements: tuple[EvidenceRequirement, ...],
        affected: tuple[str, ...],
        remaining: int,
        note: str,
    ) -> str:
        lines = [
            f"Dear {contact.display_name},",
            "",
            f"BioIntake identified {len(requirements)} recoverable missing requirement(s) for shipment "
            f"{case.shipment_id} ({case.protocol_id}), affecting samples {', '.join(affected)}:",
            "",
        ]
        for req in sorted(requirements, key=lambda r: r.key()):
            lines.append(f"- {req.sample_id or 'whole shipment'}: {req.description}")
        if note:
            lines += ["", note.strip()]
        lines += [
            "",
            "Please upload the requested items using the secure case link. The link is valid for this request only.",
        ]
        if remaining > 0:
            lines.append(f"\nNo action is required for the remaining {remaining} samples.")
        lines += [
            "",
            "- BioIntake (automated intake coordinator, synthetic demonstration)",
        ]
        return "\n".join(lines)

    # -- inbound ---------------------------------------------------------------------------------
    def receive(
        self,
        case: ShipmentCase,
        ctx: CaseContext,
        request: EvidenceRequest,
        upload_token: str,
        submitted_by_contact_id: str,
        artifacts: tuple[IncomingArtifact, ...],
        proposed_corrections: tuple[ProposedCorrection, ...],
    ) -> ReceiveOutcome:
        if request.case_id != case.case_id:
            raise EvidenceRejectedError(
                "request does not belong to this case", code=ReasonCode.REQUEST_NOT_ACTIVE
            )
        if request.status is not EvidenceRequestStatus.ACTIVE:
            raise EvidenceRejectedError(
                f"request {request.request_id} is {request.status.value}", code=ReasonCode.REQUEST_NOT_ACTIVE
            )
        if not secrets.compare_digest(upload_token, request.upload_token):
            raise EvidenceRejectedError("upload token invalid", code=ReasonCode.UPLOAD_TOKEN_INVALID)
        if self._clock() > request.expires_at:
            raise EvidenceRejectedError("request expired", code=ReasonCode.REQUEST_NOT_ACTIVE)
        contact = self._contacts.resolve(
            submitted_by_contact_id, case.shipment_id
        )  # raises RecipientNotVerified
        if contact.contact_id != request.recipient_contact_id:
            raise EvidenceRejectedError(
                f"evidence submitted by {contact.contact_id}, request was addressed to {request.recipient_contact_id}",
                code=ReasonCode.UNAUTHORIZED_ATTESTATION,
            )

        admitted: list[str] = []
        rejections: list[Rejection] = []
        excerpts: list[str] = []
        satisfied: set[str] = set(request.satisfied_requirement_keys)
        affected: list[tuple[str, CheckCategory]] = []
        now = self._clock()

        samples = {s.sample_id: s for s in self._repo.list_samples(case.case_id)}
        participants = {s.participant_reference for s in samples.values() if s.participant_reference}

        for incoming in artifacts:
            rej = self._validate_upload(incoming)
            if rej is not None:
                rejections.append(rej)
                continue
            uri, digest = self._storage.put(case.case_id, incoming.filename, incoming.content)
            artifact_type, validation, detail, meta = self._classify(incoming, ctx, participants)
            art = EvidenceArtifact(
                artifact_id=self._repo.next_id("ART"),
                case_id=case.case_id,
                artifact_type=artifact_type,
                storage_uri=uri,
                sha256=digest,
                mime_type=incoming.mime_type,
                source="sender_upload",
                original_filename=incoming.filename,
                received_at=now,
                validation_status=validation,
                request_id=request.request_id,
                submitted_by_contact_id=contact.contact_id,
                metadata=meta,
            )
            self._repo.save_artifact(art)
            if validation is not ArtifactValidation.VALID:
                rejections.append(
                    Rejection(
                        filename=incoming.filename,
                        reason_code=ReasonCode(str(meta.get("reason_code", ReasonCode.EVIDENCE_UNMATCHED))),
                        detail=detail,
                    )
                )
                continue
            admitted.append(art.artifact_id)
            if "untrusted_text" in meta:
                excerpts.append(str(meta["untrusted_text"])[:200])
            for req in request.requirements:
                # A shipment document answers its requirement for every sample the requirement blocks.
                if SATISFYING_ARTIFACT.get(req.requirement_type) is not artifact_type:
                    continue
                cats = REQUIREMENT_CATEGORIES[req.requirement_type]
                satisfied.add(req.key())
                for sid in request.affected_sample_ids:
                    affected.extend((sid, c) for c in cats)
            if artifact_type is ArtifactType.CONSENT_ADDENDUM:
                covered = set(cast(list[str], meta["participants"]))
                for req in request.requirements:
                    s = samples.get(req.sample_id)
                    if (
                        req.requirement_type is RequirementType.CONSENT_ADDENDUM
                        and s is not None
                        and s.participant_reference in covered
                    ):
                        satisfied.add(req.key())
                        affected.append((req.sample_id, CheckCategory.CONSENT_VALIDITY))

        for corr in proposed_corrections:
            result = self._admit_correction(case, ctx, request, contact, corr, samples, now)
            if isinstance(result, Rejection):
                rejections.append(result)
                continue
            admitted.append(result.artifact_id)
            sid = str(result.metadata["corrected_value"])
            for req in request.requirements:
                if req.requirement_type is RequirementType.MANIFEST_CORRECTION and req.sample_id == sid:
                    satisfied.add(req.key())
                    affected.extend(
                        [(sid, CheckCategory.IDENTITY_MATCH), (sid, CheckCategory.MANIFEST_MATCH)]
                    )

        all_keys = {r.key() for r in request.requirements}
        remaining = sorted(all_keys - satisfied)
        status = EvidenceRequestStatus.SATISFIED if not remaining else EvidenceRequestStatus.ACTIVE
        updated = request.model_copy(
            update={
                "satisfied_requirement_keys": tuple(sorted(satisfied)),
                "status": status,
                "satisfied_at": now if status is EvidenceRequestStatus.SATISFIED else None,
            }
        )
        self._repo.save_request(updated)
        return ReceiveOutcome(
            admitted_artifact_ids=tuple(admitted),
            rejections=tuple(rejections),
            satisfied_requirement_keys=tuple(sorted(satisfied)),
            remaining_requirement_keys=tuple(remaining),
            affected_checks=tuple(dict.fromkeys(affected)),
            request_status=status,
            untrusted_text_excerpts=tuple(excerpts),
        )

    @staticmethod
    def _validate_upload(incoming: IncomingArtifact) -> Rejection | None:
        if incoming.mime_type not in MIME_ALLOWLIST:
            return Rejection(
                filename=incoming.filename,
                reason_code=ReasonCode.EVIDENCE_UNMATCHED,
                detail=f"mime type {incoming.mime_type} not allowed",
            )
        if len(incoming.content) > MAX_UPLOAD_BYTES or not incoming.content:
            return Rejection(
                filename=incoming.filename,
                reason_code=ReasonCode.EVIDENCE_UNMATCHED,
                detail="empty or oversized upload",
            )
        if incoming.declared_sha256 and incoming.declared_sha256 != sha256_hex(incoming.content):
            return Rejection(
                filename=incoming.filename,
                reason_code=ReasonCode.EVIDENCE_CHECKSUM_MISMATCH,
                detail="declared checksum does not match content",
            )
        return None

    @staticmethod
    def _classify_custody_log(
        incoming: IncomingArtifact, ctx: CaseContext
    ) -> tuple[ArtifactType, ArtifactValidation, str, dict[str, object]]:
        """A custody log for this shipment: it must be readable and be about these specimens."""
        try:
            events = custody_svc.parse_custody_log(incoming.content)
        except Exception as e:
            return (
                ArtifactType.CUSTODY_LOG,
                ArtifactValidation.REJECTED,
                f"not a readable chain-of-custody log: {e}",
                {"reason_code": ReasonCode.EVIDENCE_UNMATCHED},
            )
        shipment = {row.sample_id for row in ctx.manifest_rows}
        covered = sorted({e.sample_id for e in events} & shipment)
        if not covered:
            return (
                ArtifactType.CUSTODY_LOG,
                ArtifactValidation.REJECTED,
                "the log covers no specimen in this shipment",
                {"reason_code": ReasonCode.EVIDENCE_UNMATCHED},
            )
        return (
            ArtifactType.CUSTODY_LOG,
            ArtifactValidation.VALID,
            f"chain-of-custody log admitted for {len(covered)} specimen(s)",
            {"events": len(events), "samples": covered},
        )

    @staticmethod
    def _classify_consent_registry(
        incoming: IncomingArtifact, ctx: CaseContext, participants: set[str]
    ) -> tuple[ArtifactType, ArtifactValidation, str, dict[str, object]]:
        """The site's consent registry: readable, for this protocol, about these participants."""
        try:
            records = consent_svc.parse_consent_records(incoming.content)
        except Exception as e:
            return (
                ArtifactType.CONSENT_RECORDS,
                ArtifactValidation.REJECTED,
                f"not a readable consent registry: {e}",
                {"reason_code": ReasonCode.EVIDENCE_UNMATCHED},
            )
        wrong = sorted({r.protocol_id for r in records if r.protocol_id != ctx.case.protocol_id})
        if wrong:
            return (
                ArtifactType.CONSENT_RECORDS,
                ArtifactValidation.REJECTED,
                f"registry is for protocol {', '.join(wrong)}, this shipment is {ctx.case.protocol_id}",
                {"reason_code": ReasonCode.EVIDENCE_CONTRADICTORY},
            )
        covered = sorted({r.participant_reference for r in records} & participants)
        if not covered:
            return (
                ArtifactType.CONSENT_RECORDS,
                ArtifactValidation.REJECTED,
                "the registry covers no participant in this shipment",
                {"reason_code": ReasonCode.EVIDENCE_UNMATCHED},
            )
        return (
            ArtifactType.CONSENT_RECORDS,
            ArtifactValidation.VALID,
            f"consent registry admitted for {len(covered)} participant(s)",
            {"records": len(records), "participants": covered},
        )

    def _classify(
        self, incoming: IncomingArtifact, ctx: CaseContext, participants: set[str]
    ) -> tuple[ArtifactType, ArtifactValidation, str, dict[str, object]]:
        if incoming.mime_type == "application/json":
            # Three different structured documents arrive as JSON. Discriminate on the document's own
            # shape rather than on its filename, which the sender chooses.
            try:
                shape = json.loads(incoming.content)
            except Exception as e:
                return (
                    ArtifactType.SHIPMENT_NOTICE,
                    ArtifactValidation.REJECTED,
                    f"not valid JSON: {e}",
                    {"reason_code": ReasonCode.EVIDENCE_UNMATCHED},
                )
            if isinstance(shape, dict) and "events" in shape:
                return self._classify_custody_log(incoming, ctx)
            if isinstance(shape, dict) and "records" in shape:
                return self._classify_consent_registry(incoming, ctx, participants)
            try:
                doc = consent_svc.parse_consent_addendum(incoming.content)
            except Exception as e:
                return (
                    ArtifactType.CONSENT_ADDENDUM,
                    ArtifactValidation.REJECTED,
                    f"not a valid consent addendum: {e}",
                    {"reason_code": ReasonCode.EVIDENCE_UNMATCHED},
                )
            rule = ctx.policy.consent
            if doc.protocol_id != ctx.case.protocol_id or doc.scope != rule.required_scope:
                return (
                    ArtifactType.CONSENT_ADDENDUM,
                    ArtifactValidation.REJECTED,
                    "addendum protocol/scope contradicts the shipment",
                    {"reason_code": ReasonCode.EVIDENCE_CONTRADICTORY},
                )
            if doc.site_id != ctx.case.sender_site_id:
                return (
                    ArtifactType.CONSENT_ADDENDUM,
                    ArtifactValidation.REJECTED,
                    "addendum site does not match sender site",
                    {"reason_code": ReasonCode.EVIDENCE_CONTRADICTORY},
                )
            if doc.version < rule.min_version:
                return (
                    ArtifactType.CONSENT_ADDENDUM,
                    ArtifactValidation.REJECTED,
                    f"addendum v{doc.version} is below required v{rule.min_version}",
                    {"reason_code": ReasonCode.EVIDENCE_CONTRADICTORY},
                )
            covered = sorted(set(doc.participants) & participants)
            if not covered:
                return (
                    ArtifactType.CONSENT_ADDENDUM,
                    ArtifactValidation.REJECTED,
                    "addendum covers no participant in this shipment",
                    {"reason_code": ReasonCode.EVIDENCE_UNMATCHED},
                )
            meta: dict[str, object] = {
                "version": doc.version,
                "participants": covered,
                "signed_date": str(doc.signed_date),
            }
            if doc.notes:
                meta["untrusted_text"] = doc.notes  # stored as data; never interpreted as instructions
            return ArtifactType.CONSENT_ADDENDUM, ArtifactValidation.VALID, "consent addendum admitted", meta
        # Any other allowed type is stored as supporting material but satisfies nothing on its own.
        return (
            ArtifactType.SHIPMENT_NOTICE,
            ArtifactValidation.REJECTED,
            "unstructured document cannot satisfy a requirement",
            {"reason_code": ReasonCode.EVIDENCE_UNMATCHED},
        )

    def _admit_correction(
        self,
        case: ShipmentCase,
        ctx: CaseContext,
        request: EvidenceRequest,
        contact: SiteContact,
        corr: ProposedCorrection,
        samples: Mapping[str, Sample],
        now: datetime,
    ) -> EvidenceArtifact | Rejection:
        """Admit a sender's manifest correction.

        Confirming: the corrected value equals the label that was tentatively linked to that row.
        Refuting: the sender states the row is NOT that label (corrected value is something else or
        empty). A refutation is admitted as evidence that breaks the tentative association, every
        result that depended on it is invalidated; nothing is accepted on the strength of it.
        """
        name = f"attestation-row{corr.manifest_row}.json"
        row = ctx.rows_by_number.get(corr.manifest_row)
        if row is None or row.sample_id != corr.manifest_value:
            return Rejection(
                filename=name,
                reason_code=ReasonCode.EVIDENCE_CONTRADICTORY,
                detail="manifest row/value does not match the manifest on file",
            )
        link = next((lk for lk in ctx.links.values() if lk.manifest_row == corr.manifest_row), None)
        if link is None or link.exact:
            return Rejection(
                filename=name,
                reason_code=ReasonCode.EVIDENCE_CONTRADICTORY,
                detail="that row is not an unresolved near-match",
            )
        if corr.manifest_row in ctx.refuted_rows or corr.manifest_row in ctx.attestations:
            return Rejection(
                filename=name,
                reason_code=ReasonCode.EVIDENCE_CONTRADICTORY,
                detail="an attestation for that row is already on file",
            )
        if not any(
            r.requirement_type is RequirementType.MANIFEST_CORRECTION and r.sample_id == link.sample_id
            for r in request.requirements
        ):
            return Rejection(
                filename=name,
                reason_code=ReasonCode.EVIDENCE_UNMATCHED,
                detail="no manifest-correction requirement in this request for that row",
            )
        current = self._repo.checks_by_category(link.sample_id, case.case_id)
        ident = current.get(CheckCategory.IDENTITY_MATCH)
        if ident is None or ident.status is not CheckStatus.AMBIGUOUS:
            return Rejection(
                filename=name,
                reason_code=ReasonCode.EVIDENCE_CONTRADICTORY,
                detail="identity for that sample is not awaiting confirmation",
            )

        refutes = corr.corrected_value != link.sample_id
        if not refutes and any(
            r.sample_id == corr.corrected_value for r in ctx.manifest_rows if r.row != corr.manifest_row
        ):
            return Rejection(
                filename=name,
                reason_code=ReasonCode.EVIDENCE_CONTRADICTORY,
                detail="correction would collide with another manifest row",
            )
        if refutes and corr.corrected_value in samples:
            return Rejection(
                filename=name,
                reason_code=ReasonCode.EVIDENCE_CONTRADICTORY,
                detail="a refutation may not re-assign the row to another sample in this shipment",
            )

        payload = {
            "document": "SENDER_ATTESTATION",
            "request_id": request.request_id,
            "contact_id": contact.contact_id,
            "manifest_row": corr.manifest_row,
            "manifest_value": corr.manifest_value,
            "corrected_value": link.sample_id if not refutes else corr.corrected_value,
            "tentative_sample_id": link.sample_id,
            "refutes": refutes,
            "statement": corr.sender_statement,
        }
        content = json.dumps(payload, sort_keys=True).encode()
        uri, digest = self._storage.put(case.case_id, name, content)
        art = EvidenceArtifact(
            artifact_id=self._repo.next_id("ART"),
            case_id=case.case_id,
            artifact_type=ArtifactType.SENDER_ATTESTATION,
            storage_uri=uri,
            sha256=digest,
            mime_type="application/json",
            source="sender_upload",
            original_filename=name,
            received_at=now,
            validation_status=ArtifactValidation.VALID,
            request_id=request.request_id,
            submitted_by_contact_id=contact.contact_id,
            metadata={k: v for k, v in payload.items() if k != "document"},
        )
        self._repo.save_artifact(art)
        return art
