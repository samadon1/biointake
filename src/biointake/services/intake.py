"""Intake orchestration: every mutating operation the agent tools call lives here.

Each command passes through the idempotency guard, the deterministic disposition engine, the
demo LIMS (which refuses writes without a stored, fresh, unconsumed ALLOWED evaluation) and the
transition service. Re-verification after new evidence follows a stored InvalidationPlan.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from ..clock import Clock
from ..domain.commands import (
    ApplyInvalidationPlanCommand,
    CreateEvidenceRequestCommand,
    FinalizeCaseCommand,
    IdempotencyGuard,
    OpenQuarantineReviewCommand,
    RaisePendingDecisionCommand,
    ReceiveEvidenceCommand,
    RecordHumanDecisionCommand,
    RequestDispositionCommand,
    RetryRequestedCommand,
)
from ..domain.disposition import DispositionEngine
from ..domain.enums import (
    TERMINAL_CASE_STATES,
    TERMINAL_SAMPLE_STATES,
    ActorRole,
    ArtifactType,
    ArtifactValidation,
    AuditEventType,
    AuditKind,
    CaseState,
    CheckCategory,
    CheckStatus,
    Disposition,
    EvidenceRequestStatus,
    HumanOption,
    PolicyDecision,
    ReasonCode,
    RequirementType,
    SampleState,
)
from ..domain.errors import CaseFinalizedError, NotFoundError, PolicyDeniedError, UnauthorizedError
from ..domain.models import (
    ActorContext,
    CheckResult,
    CommandResult,
    DecisionOption,
    EvidenceArtifact,
    EvidenceRequest,
    EvidenceRequirement,
    HumanDecision,
    PendingDecision,
    PolicyEvaluation,
    Sample,
    ShipmentCase,
    Study,
)
from ..domain.policies import ProtocolPolicy
from ..domain.state_machine import TransitionService
from ..fixtures import ShipmentPackage
from ..repositories.interfaces import Repository
from ..storage.interfaces import ArtifactStorage
from . import manifest as manifest_svc
from .contacts import ContactDirectory
from .delivery import MessageDelivery, RecordedDelivery
from .dependencies import EvidenceDependencyService
from .evidence import EvidenceService
from .lims_demo import DemoLims
from .verification import VerificationService

MAX_RETRIES_PER_SAMPLE = 2
RETRYABLE_ERROR_CODES = frozenset({ReasonCode.CHECK_ERROR, ReasonCode.TOOL_FAILURE_TRANSIENT})

_STATE_FOR_DISPOSITION = {
    Disposition.ACCEPT: SampleState.ACCEPTED,
    Disposition.ACCEPT_WITH_EXCEPTION: SampleState.ACCEPTED_WITH_EXCEPTION,
    Disposition.QUARANTINE: SampleState.QUARANTINED,
    Disposition.REJECT: SampleState.REJECTED,
}


def _digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:32]


# A check can be UNAVAILABLE because a whole document never arrived. That is recoverable, the site
# has the document, so it becomes a requirement addressed to the shipment rather than to a sample.
SHIPMENT_DOCUMENTS: dict[CheckCategory, RequirementType] = {
    CheckCategory.CONSENT_VALIDITY: RequirementType.CONSENT_REGISTRY,
    CheckCategory.CHAIN_OF_CUSTODY: RequirementType.CUSTODY_LOG,
}

SHIPMENT_DOCUMENT_DESCRIPTIONS: dict[RequirementType, str] = {
    RequirementType.CONSENT_REGISTRY: (
        "Consent registry for this shipment: the {protocol_id} consent record for every participant "
        "whose specimen is in the box"
    ),
    RequirementType.CUSTODY_LOG: (
        "Chain-of-custody log for this shipment: collection through packing for every specimen in the box"
    ),
}


class IntakeService:
    def __init__(
        self,
        repo: Repository,
        storage: ArtifactStorage,
        policy: ProtocolPolicy,
        clock: Clock,
        *,
        lims: DemoLims | None = None,
        token_factory: Any = None,
        delivery: MessageDelivery | None = None,
        portal_base_url: str = "",
    ) -> None:
        self.repo = repo
        self.storage = storage
        self.policy = policy
        self.clock = clock
        self.lims: DemoLims = lims if lims is not None else DemoLims([])
        self.delivery: MessageDelivery = delivery if delivery is not None else RecordedDelivery()
        self.portal_base_url = portal_base_url.rstrip("/")
        self.transitions = TransitionService(repo, clock)
        self.guard = IdempotencyGuard(repo, clock)
        self.contacts = ContactDirectory(repo)
        self.engine = DispositionEngine(policy)
        self.verification = VerificationService(repo, storage, policy, self.lims, clock)
        self.evidence = EvidenceService(repo, storage, self.contacts, clock, token_factory)
        self.dependencies = EvidenceDependencyService(repo, clock)

    # ==========================================================================================
    # Case creation
    # ==========================================================================================
    def create_case(
        self, package: ShipmentPackage, actor: ActorContext, case_id: str | None = None
    ) -> ShipmentCase:
        info = package.shipment
        case_id = case_id or f"CASE-{info.shipment_id}"
        now = self.clock()
        study = self._ensure_study_for(package.policy)
        case = ShipmentCase(
            case_id=case_id,
            study_id=study.study_id,
            shipment_id=info.shipment_id,
            protocol_id=info.protocol_id,
            protocol_version=info.protocol_version,
            sender_site_id=info.sender_site_id,
            received_at=info.received_at,
            agent_session_id=f"{case_id}-{uuid.uuid4()}"[:96],  # fresh per case; ≥33 chars for AgentCore
            expected_sample_count=info.expected_sample_count,
            created_at=now,
            updated_at=now,
        )
        self.repo.save_case(case)
        for contact in package.contacts:
            self.repo.save_contact(contact)
        self.lims.seed(package.lims_records)

        self._store(case_id, ArtifactType.MANIFEST, "manifest.csv", "text/csv", package.manifest_csv)
        self._store(
            case_id,
            ArtifactType.SCANNER_EXPORT,
            "scanner-export.json",
            "application/json",
            package.scanner_export_json,
        )
        for logger_id, data in sorted(package.temperature_logs.items()):
            self._store(
                case_id,
                ArtifactType.TEMPERATURE_LOG,
                f"{logger_id}.csv",
                "text/csv",
                data,
                {"logger_id": logger_id},
            )
        self._store(
            case_id,
            ArtifactType.PROTOCOL,
            f"{info.protocol_id}.json",
            "application/json",
            package.protocol_json,
        )
        self._store(
            case_id,
            ArtifactType.CONSENT_RECORDS,
            "consent-records.json",
            "application/json",
            package.consent_records_json,
        )
        self._store(
            case_id,
            ArtifactType.CUSTODY_LOG,
            "chain-of-custody.json",
            "application/json",
            package.custody_log_json,
        )

        rows = list(manifest_svc.parse_manifest(package.manifest_csv).rows)
        export = manifest_svc.parse_scanner_export(package.scanner_export_json)
        by_row = manifest_svc.manifest_row_lookup(rows)
        logger_for = {c.container_id: c.logger_id for c in info.containers}
        scans = {s.sample_id: s for s in export.scans}
        for link in manifest_svc.link_labels_to_manifest(rows, export):
            row = by_row.get(link.manifest_row) if link.manifest_row is not None else None
            scan = scans[link.sample_id]
            self.repo.save_sample(
                Sample(
                    sample_id=link.sample_id,
                    case_id=case_id,
                    barcode=link.barcode,
                    specimen_type=(row.specimen_type.upper() if row else "UNKNOWN"),
                    container_id=scan.container_id,
                    logger_id=logger_for.get(scan.container_id),
                    manifest_row=link.manifest_row,
                    participant_reference=row.participant_reference if row else None,
                    collection_timestamp=row.collection_timestamp if row else None,
                    expected_protocol_id=info.protocol_id,
                    updated_at=now,
                )
            )
        case = case.model_copy(update={"observed_sample_count": len(export.scans)})
        self.repo.save_case(case)
        self.repo.append_audit(
            case_id=case_id,
            event_type=AuditEventType.CASE_CREATED,
            actor=actor,
            summary=f"Case created for {info.shipment_id}: {len(export.scans)} labels scanned, "
            f"{len(rows)} manifest rows, {len(package.temperature_logs)} loggers",
            metadata={"expected": info.expected_sample_count, "observed": len(export.scans)},
        )
        return case

    def _store(
        self,
        case_id: str,
        artifact_type: ArtifactType,
        filename: str,
        mime: str,
        data: bytes,
        metadata: dict[str, Any] | None = None,
    ) -> EvidenceArtifact:
        uri, digest = self.storage.put(case_id, filename, data)
        art = EvidenceArtifact(
            artifact_id=self.repo.next_id("ART"),
            case_id=case_id,
            artifact_type=artifact_type,
            storage_uri=uri,
            sha256=digest,
            mime_type=mime,
            source="intake_package",
            original_filename=filename,
            received_at=self.clock(),
            validation_status=ArtifactValidation.VALID,
            metadata=metadata or {},
        )
        self.repo.save_artifact(art)
        return art

    # ==========================================================================================
    # Verification
    # ==========================================================================================
    def begin_verification(self, case_id: str, actor: ActorContext) -> ShipmentCase:
        return self.transitions.transition_case(
            case_id, CaseState.VERIFYING, actor, ReasonCode.ALL_CHECKS_PASS, summary="Verification started"
        )

    def verify(
        self,
        case_id: str,
        actor: ActorContext,
        *,
        sample_ids: tuple[str, ...] | None = None,
        categories: tuple[CheckCategory, ...] | None = None,
        tool_name: str = "verification",
    ) -> list[CheckResult]:
        self._require_open(case_id)
        return self.verification.run(
            case_id, actor, sample_ids=sample_ids, categories=categories, tool_name=tool_name
        )

    def _ensure_study_for(self, policy: ProtocolPolicy) -> Study:
        """The study a fixture-loaded case belongs to, created from its own policy if absent."""
        existing = self.repo.get_study(policy.protocol_id)
        if existing is not None:
            return existing
        now = self.clock()
        study = Study(
            study_id=policy.protocol_id,
            name=policy.title,
            protocol_id=policy.protocol_id,
            policy_version=policy.version,
            policy=policy,
            exception_approval_role=(
                policy.temperature.exception_roles or (ActorRole.PRINCIPAL_INVESTIGATOR,)
            )[0],
            created_at=now,
            updated_at=now,
        )
        self.repo.save_study(study)
        return study

    def policy_for(self, case_id: str) -> ProtocolPolicy:
        """The rules this case is judged by: its study's, not the process's.

        A service instance serves every case, so holding one policy on it was correct only while there was
        one study. The moment a lab authors a second, a shipment announced against it would be judged by
        somebody else's protocol, which is the failure this product exists to prevent.
        """
        return self.verification.policy_for(self.repo.get_case(case_id))

    def engine_for(self, case_id: str) -> DispositionEngine:
        policy = self.policy_for(case_id)
        return self.engine if policy is self.policy else DispositionEngine(policy)

    def total_check_slots(self, case_id: str) -> int:
        return len(self.repo.list_samples(case_id)) * len(self.policy_for(case_id).required_checks)

    # ==========================================================================================
    # Policy evaluation with freshness binding
    # ==========================================================================================
    def _check_set_digest(self, sample_id: str, case_id: str) -> str:
        checks = self.repo.checks_by_category(sample_id, case_id)
        return _digest(
            [
                (c.value, checks[c].check_id, checks[c].status.value, checks[c].input_fingerprint)
                for c in self.policy_for(case_id).required_checks
                if c in checks
            ]
        )

    def _evidence_snapshot_digest(self, sample_id: str, case_id: str) -> str:
        arts = sorted(
            (a.artifact_id, a.sha256)
            for a in self.repo.list_artifacts(case_id)
            if a.validation_status is ArtifactValidation.VALID
            and a.artifact_type is not ArtifactType.AUDIT_REPORT
        )
        decisions = sorted(
            d.decision_id for d in self.repo.list_decisions(case_id) if d.sample_id == sample_id
        )
        return _digest({"artifacts": arts, "decisions": decisions})

    def _evaluate(
        self, sample: Sample, requested: Disposition, decision: HumanDecision | None
    ) -> PolicyEvaluation:
        case = self.repo.get_case(sample.case_id)
        checks = self.repo.checks_by_category(sample.sample_id, sample.case_id)
        evaluation = self.engine_for(sample.case_id).evaluate(
            evaluation_id=self.repo.next_id("PE"),
            case_id=sample.case_id,
            sample_id=sample.sample_id,
            checks=checks,
            requested=requested,
            human_decision=decision,
            now=self.clock(),
        )
        evaluation = evaluation.model_copy(
            update={
                "case_version": case.case_version,
                "sample_version": sample.sample_version,
                "check_set_digest": self._check_set_digest(sample.sample_id, sample.case_id),
                "evidence_snapshot_digest": self._evidence_snapshot_digest(sample.sample_id, sample.case_id),
            }
        )
        self.repo.save_evaluation(evaluation)
        return evaluation

    def evaluation_freshness(self, evaluation: PolicyEvaluation) -> tuple[bool, str]:
        """True only if every bound version/digest still matches current authoritative state."""
        case = self.repo.get_case(evaluation.case_id)
        sample = self.repo.get_sample(evaluation.sample_id)
        problems = []
        policy = self.policy_for(evaluation.case_id)
        if evaluation.policy_id != policy.policy_id or evaluation.policy_version != policy.version:
            problems.append(
                f"policy {evaluation.policy_id}@{evaluation.policy_version} ≠ {policy.policy_id}@{policy.version}"
            )
        if evaluation.case_version != case.case_version:
            problems.append(f"case version {evaluation.case_version} ≠ {case.case_version}")
        if evaluation.sample_version != sample.sample_version:
            problems.append(f"sample version {evaluation.sample_version} ≠ {sample.sample_version}")
        if evaluation.check_set_digest != self._check_set_digest(sample.sample_id, case.case_id):
            problems.append("check set changed")
        if evaluation.evidence_snapshot_digest != self._evidence_snapshot_digest(
            sample.sample_id, case.case_id
        ):
            problems.append("evidence snapshot changed")
        if evaluation.human_decision_id is not None:
            try:
                d = self.repo.get_decision(evaluation.human_decision_id)
                if d.sample_id != sample.sample_id:
                    problems.append("human decision belongs to another sample")
            except NotFoundError:
                problems.append("human decision missing")
        return (not problems, "; ".join(problems))

    # ==========================================================================================
    # Dispositions
    # ==========================================================================================
    def request_disposition(self, cmd: RequestDispositionCommand) -> CommandResult:
        return self.guard.run(cmd, lambda: self._request_disposition(cmd))

    def _request_disposition(self, cmd: RequestDispositionCommand) -> CommandResult:
        self._require_open(cmd.case_id)
        sample = self.repo.get_sample(cmd.sample_id)
        if sample.case_id != cmd.case_id:
            raise NotFoundError(f"sample {cmd.sample_id} is not in case {cmd.case_id}")
        if sample.state in TERMINAL_SAMPLE_STATES:
            return CommandResult(
                operation_id=cmd.operation_id,
                status="denied",
                summary=f"{sample.sample_id} is already {sample.state.value}",
                reason_codes=(ReasonCode.INVALID_STATE_TRANSITION,),
                data={"sample_id": sample.sample_id, "state": sample.state.value},
            )
        decision: HumanDecision | None = None
        if cmd.human_decision_id:
            decision = self.repo.get_decision(cmd.human_decision_id)
            if decision.sample_id != sample.sample_id or decision.case_id != cmd.case_id:
                raise UnauthorizedError("human decision does not belong to this sample")
        checks = self.repo.checks_by_category(sample.sample_id, cmd.case_id)
        evaluation = self._evaluate(sample, cmd.requested, decision)
        audit = self.repo.append_audit(
            case_id=cmd.case_id,
            event_type=AuditEventType.POLICY_EVALUATED,
            actor=cmd.actor,
            tool_name="disposition_engine",
            operation_id=cmd.operation_id,
            summary=f"{sample.sample_id}: requested {cmd.requested.value} → {evaluation.decision.value}"
            + (
                f" (blocked by {', '.join(c.value for c in evaluation.blocking_checks)})"
                if evaluation.blocking_checks
                else ""
            ),
            reason_codes=evaluation.reason_codes,
            sample_ids=(sample.sample_id,),
            output_status=evaluation.decision.value.lower(),
            metadata={
                "evaluation_id": evaluation.evaluation_id,
                "policy": f"{evaluation.policy_id}@{evaluation.policy_version}",
                "check_set_digest": evaluation.check_set_digest,
            },
        )
        audit_ids = [audit.audit_event_id]
        data: dict[str, Any] = {
            "sample_id": sample.sample_id,
            "evaluation_id": evaluation.evaluation_id,
            "decision": evaluation.decision.value,
            "blocking_checks": [c.value for c in evaluation.blocking_checks],
        }

        if evaluation.decision is PolicyDecision.ALLOWED:
            updated, record, more = self._commit_allowed(sample, evaluation, cmd, checks, decision)
            audit_ids.extend(more)
            data.update({"lims_record_id": record.record_id, "state": updated.state.value})
            return CommandResult(
                operation_id=cmd.operation_id,
                status="ok",
                summary=f"{sample.sample_id} {updated.state.value}",
                reason_codes=evaluation.reason_codes,
                data=data,
                audit_event_ids=tuple(audit_ids),
            )

        if evaluation.decision is PolicyDecision.WAITING_FOR_EVIDENCE:
            if sample.state is SampleState.PENDING:
                self.transitions.transition_sample(
                    sample.sample_id,
                    SampleState.WAITING_FOR_EVIDENCE,
                    cmd.actor,
                    evaluation.reason_codes[0],
                    operation_id=cmd.operation_id,
                )
            data["recoverable_requirements"] = [
                r.model_dump(mode="json") for r in self.recoverable_requirements(sample, checks)
            ]
            data["state"] = SampleState.WAITING_FOR_EVIDENCE.value
            return CommandResult(
                operation_id=cmd.operation_id,
                status="waiting",
                summary=f"{sample.sample_id} needs evidence: {', '.join(c.value for c in evaluation.blocking_checks)}",
                reason_codes=evaluation.reason_codes,
                data=data,
                audit_event_ids=tuple(audit_ids),
            )

        if evaluation.decision is PolicyDecision.HUMAN_DECISION_REQUIRED:
            if sample.state in (SampleState.PENDING, SampleState.WAITING_FOR_EVIDENCE):
                self.transitions.transition_sample(
                    sample.sample_id,
                    SampleState.NEEDS_HUMAN_DECISION,
                    cmd.actor,
                    ReasonCode.HUMAN_AUTHORITY_REQUIRED,
                    operation_id=cmd.operation_id,
                )
            data["state"] = SampleState.NEEDS_HUMAN_DECISION.value
            return CommandResult(
                operation_id=cmd.operation_id,
                status="human_required",
                summary=f"{sample.sample_id} requires human disposition",
                reason_codes=evaluation.reason_codes,
                data=data,
                audit_event_ids=tuple(audit_ids),
            )

        if evaluation.decision is PolicyDecision.SYSTEM_ERROR:
            if sample.state is not SampleState.ERROR:
                self.transitions.transition_sample(
                    sample.sample_id,
                    SampleState.ERROR,
                    cmd.actor,
                    ReasonCode.CHECK_ERROR,
                    operation_id=cmd.operation_id,
                )
            data["state"] = SampleState.ERROR.value
            data["retryable"] = bool(RETRYABLE_ERROR_CODES & set(evaluation.reason_codes))
            return CommandResult(
                operation_id=cmd.operation_id,
                status="error",
                summary=f"{sample.sample_id} has evaluator errors",
                reason_codes=evaluation.reason_codes,
                data=data,
                audit_event_ids=tuple(audit_ids),
            )

        data["state"] = sample.state.value
        return CommandResult(
            operation_id=cmd.operation_id,
            status="denied",
            summary=f"{sample.sample_id}: {cmd.requested.value} denied ({', '.join(c.value for c in evaluation.reason_codes)})",
            reason_codes=evaluation.reason_codes,
            data=data,
            audit_event_ids=tuple(audit_ids),
        )

    def _commit_allowed(
        self,
        sample: Sample,
        evaluation: PolicyEvaluation,
        cmd: RequestDispositionCommand,
        checks: dict[CheckCategory, CheckResult],
        decision: HumanDecision | None,
    ) -> tuple[Sample, Any, list[str]]:
        record = self.lims.write_disposition(
            sample, evaluation, cmd.operation_id, self.repo.get_evaluation, self.evaluation_freshness
        )
        self.repo.save_evaluation(
            evaluation.model_copy(update={"consumed_by_operation_id": cmd.operation_id})
        )
        ids = [
            self.repo.append_audit(
                case_id=cmd.case_id,
                event_type=AuditEventType.LIMS_WRITE,
                actor=cmd.actor,
                tool_name="demo_lims",
                operation_id=cmd.operation_id,
                summary=f"LIMS {record.record_id} → {record.status} (evaluation {evaluation.evaluation_id})",
                sample_ids=(sample.sample_id,),
                metadata={"record_id": record.record_id, "status": record.status},
            ).audit_event_id
        ]
        evidence_refs = tuple(
            dict.fromkeys(cmd.evidence_refs + tuple(r for c in checks.values() for r in c.evidence_refs))
        )
        updated = self.transitions.transition_sample(
            sample.sample_id,
            _STATE_FOR_DISPOSITION[cmd.requested],
            cmd.actor,
            evaluation.reason_codes[0] if evaluation.reason_codes else ReasonCode.ALL_CHECKS_PASS,
            evidence_refs=evidence_refs,
            policy_evaluation_id=evaluation.evaluation_id,
            operation_id=cmd.operation_id,
        )
        updated = updated.model_copy(update={"lims_record_id": record.record_id})
        self.repo.save_sample(updated)
        if decision is not None:
            ids.append(
                self.repo.append_audit(
                    case_id=cmd.case_id,
                    event_type=AuditEventType.HUMAN_DECISION_APPLIED,
                    actor=cmd.actor,
                    operation_id=cmd.operation_id,
                    summary=f"Human decision {decision.decision_id} ({decision.selected_option.value}) applied to {sample.sample_id}",
                    reason_codes=(ReasonCode.HUMAN_DECISION_APPLIED,),
                    sample_ids=(sample.sample_id,),
                    metadata={"decision_id": decision.decision_id, "evaluation_id": evaluation.evaluation_id},
                ).audit_event_id
            )
        return updated, record, ids

    def recoverable_requirements(
        self, sample: Sample, checks: dict[CheckCategory, CheckResult]
    ) -> list[EvidenceRequirement]:
        reqs: list[EvidenceRequirement] = []
        seen: set[str] = set()
        policy = self.policy_for(sample.case_id)
        for cat, check in checks.items():
            if check.status not in (CheckStatus.UNAVAILABLE, CheckStatus.AMBIGUOUS):
                continue
            rtype: RequirementType | None = None
            desc = ""
            sample_key = sample.sample_id
            if (
                cat is CheckCategory.CONSENT_VALIDITY
                and ReasonCode.CONSENT_ADDENDUM_MISSING in check.reason_codes
            ):
                rtype = RequirementType.CONSENT_ADDENDUM
                desc = (
                    f"{policy.protocol_id} consent addendum v{policy.consent.min_version} or later "
                    f"for participant {sample.participant_reference}"
                )
            elif (
                cat in (CheckCategory.IDENTITY_MATCH, CheckCategory.MANIFEST_MATCH)
                and ReasonCode.MANIFEST_IDENTIFIER_NEAR_MATCH in check.reason_codes
            ):
                rtype = RequirementType.MANIFEST_CORRECTION
                desc = f"Confirm manifest identifier: {check.observed_value}"
            elif (
                cat is CheckCategory.CHAIN_OF_CUSTODY
                and ReasonCode.CUSTODY_EVENT_MISSING in check.reason_codes
            ):
                rtype = RequirementType.CUSTODY_RECORD
                desc = f"Chain-of-custody record: {check.summary}"
            elif ReasonCode.REQUIRED_EVIDENCE_NOT_SUPPLIED in check.reason_codes:
                # A whole document is missing, so it is missing for every sample at once. Asking for it
                # is the only thing that moves the shipment; without this the samples sit in
                # WAITING_FOR_EVIDENCE and nobody is ever told what to send.
                rtype = SHIPMENT_DOCUMENTS.get(cat)
                if rtype is None:
                    continue
                desc = SHIPMENT_DOCUMENT_DESCRIPTIONS[rtype].format(protocol_id=policy.protocol_id)
                sample_key = ""
            if rtype is None:
                continue
            req = EvidenceRequirement(requirement_type=rtype, sample_id=sample_key, description=desc)
            if req.key() not in seen:
                seen.add(req.key())
                reqs.append(req)
        return reqs

    def unresolved_requirements(self, case_id: str) -> list[EvidenceRequirement]:
        out: dict[str, EvidenceRequirement] = {}
        for s in self.repo.list_samples(case_id):
            if s.state in TERMINAL_SAMPLE_STATES:
                continue
            for req in self.recoverable_requirements(s, self.repo.checks_by_category(s.sample_id, case_id)):
                out.setdefault(req.key(), req)  # a shipment document is asked for once
        return list(out.values())

    # ==========================================================================================
    # Evidence requests / receipt / invalidation plans
    # ==========================================================================================
    def portal_url(self, request: EvidenceRequest) -> str:
        """Where the recipient goes. Relative while no base URL is configured, because a link into
        `localhost` in an email that did leave the building would be worse than no link."""
        path = f"/portal/{request.request_id}?token={request.upload_token}"
        return f"{self.portal_base_url}{path}" if self.portal_base_url else path

    def _with_portal_link(self, request: EvidenceRequest) -> str:
        if not self.portal_base_url:
            return request.body
        return request.body.replace(
            "Please upload the requested items using the secure case link. "
            "The link is valid for this request only.",
            "Please upload the requested items here. The link is valid for this request only.\n\n"
            f"  {self.portal_url(request)}",
            1,
        )

    def create_evidence_request(self, cmd: CreateEvidenceRequestCommand) -> CommandResult:
        return self.guard.run(cmd, lambda: self._create_evidence_request(cmd))

    def _create_evidence_request(self, cmd: CreateEvidenceRequestCommand) -> CommandResult:
        case = self._require_open(cmd.case_id)
        request, contact = self.evidence.build_request(
            case, cmd.recipient_contact_id, cmd.requirements, cmd.note_for_recipient
        )
        body = self._with_portal_link(request)
        outcome = self.delivery.send(to=contact.destination, subject=request.subject, body=body)
        request = request.model_copy(
            update={
                "body": body,
                "delivered": outcome.delivered,
                "delivery_channel": outcome.channel,
                "delivery_detail": outcome.detail,
            }
        )
        self.repo.save_request(request)
        verb = "sent to" if outcome.delivered else "prepared for"
        audit = self.repo.append_audit(
            case_id=cmd.case_id,
            event_type=AuditEventType.EVIDENCE_REQUEST_SENT,
            actor=cmd.actor,
            tool_name="communication",
            operation_id=cmd.operation_id,
            summary=f"Evidence request {request.request_id} {verb} {contact.display_name} "
            f"({contact.contact_id}) for {len(request.requirements)} item(s) affecting "
            f"{', '.join(request.affected_sample_ids)}: {outcome.detail}",
            output_status="ok" if outcome.delivered else "recorded",
            sample_ids=request.affected_sample_ids,
            metadata={
                "request_id": request.request_id,
                "subject": request.subject,
                "destination": contact.destination,
                "fingerprint": request.fingerprint,
                **outcome.audit_metadata(),
            },
        )
        for sid in request.affected_sample_ids:
            s = self.repo.get_sample(sid)
            if s.state is SampleState.PENDING:
                self.transitions.transition_sample(
                    sid,
                    SampleState.WAITING_FOR_EVIDENCE,
                    cmd.actor,
                    ReasonCode.EVIDENCE_RECOVERY_IN_PROGRESS,
                    operation_id=cmd.operation_id,
                )
        return CommandResult(
            operation_id=cmd.operation_id,
            status="ok",
            summary=f"{'Sent' if outcome.delivered else 'Prepared'} {request.request_id} "
            f"for {contact.contact_id}: {outcome.detail}",
            data={
                "request_id": request.request_id,
                "recipient_contact_id": contact.contact_id,
                "affected_sample_ids": list(request.affected_sample_ids),
                "subject": request.subject,
                "body": request.body,
                "delivered": outcome.delivered,
                "delivery_detail": outcome.detail,
            },
            audit_event_ids=(audit.audit_event_id,),
        )

    def receive_evidence(self, cmd: ReceiveEvidenceCommand) -> CommandResult:
        return self.guard.run(cmd, lambda: self._receive_evidence(cmd))

    def _receive_evidence(self, cmd: ReceiveEvidenceCommand) -> CommandResult:
        case = self._require_open(cmd.case_id)
        request = self.repo.get_request(cmd.request_id)
        ctx = self.verification.build_context(cmd.case_id)
        outcome = self.evidence.receive(
            case,
            ctx,
            request,
            cmd.upload_token,
            cmd.submitted_by_contact_id,
            cmd.artifacts,
            cmd.proposed_corrections,
        )
        audit_ids: list[str] = []
        if outcome.admitted_artifact_ids:
            audit_ids.append(
                self.repo.append_audit(
                    case_id=cmd.case_id,
                    event_type=AuditEventType.EVIDENCE_RECEIVED,
                    actor=cmd.actor,
                    tool_name="evidence",
                    operation_id=cmd.operation_id,
                    summary=f"Admitted {len(outcome.admitted_artifact_ids)} artifact(s) for {request.request_id}; "
                    f"satisfied {', '.join(outcome.satisfied_requirement_keys) or 'nothing'}",
                    sample_ids=tuple(dict.fromkeys(s for s, _ in outcome.affected_checks)),
                    metadata={"artifacts": list(outcome.admitted_artifact_ids)},
                ).audit_event_id
            )
        for rej in outcome.rejections:
            audit_ids.append(
                self.repo.append_audit(
                    case_id=cmd.case_id,
                    event_type=AuditEventType.EVIDENCE_REJECTED,
                    actor=cmd.actor,
                    tool_name="evidence",
                    operation_id=cmd.operation_id,
                    summary=f"Rejected {rej.filename}: {rej.detail}",
                    reason_codes=(rej.reason_code,),
                    output_status="rejected",
                ).audit_event_id
            )
        if outcome.request_status is EvidenceRequestStatus.SATISFIED:
            audit_ids.append(
                self.repo.append_audit(
                    case_id=cmd.case_id,
                    event_type=AuditEventType.EVIDENCE_REQUEST_SATISFIED,
                    actor=cmd.actor,
                    operation_id=cmd.operation_id,
                    summary=f"Evidence request {request.request_id} fully satisfied",
                    sample_ids=request.affected_sample_ids,
                ).audit_event_id
            )
        plan_id: str | None = None
        invalidated: list[str] = []
        if outcome.admitted_artifact_ids:
            if case.state is CaseState.WAITING_FOR_EVIDENCE:
                self.transitions.transition_case(
                    cmd.case_id,
                    CaseState.VERIFYING,
                    cmd.actor,
                    ReasonCode.EVIDENCE_RECOVERY_IN_PROGRESS,
                    operation_id=cmd.operation_id,
                    summary="Evidence received; resuming verification",
                )
            plan = self.dependencies.compute_invalidation_plan(
                cmd.case_id, list(outcome.admitted_artifact_ids), cmd.actor
            )
            plan_id, invalidated = plan.plan_id, list(plan.invalidated_check_ids)
        status = "ok" if outcome.admitted_artifact_ids else "denied"
        return CommandResult(
            operation_id=cmd.operation_id,
            status=status,
            summary=f"{len(outcome.admitted_artifact_ids)} admitted, {len(outcome.rejections)} rejected; request {outcome.request_status.value}"
            + (f"; plan {plan_id} invalidates {len(invalidated)} check(s)" if plan_id else ""),
            reason_codes=tuple(r.reason_code for r in outcome.rejections),
            data={
                **outcome.model_dump(mode="json", exclude={"affected_checks"}),
                "plan_id": plan_id,
                "invalidated_check_ids": invalidated,
            },
            audit_event_ids=tuple(audit_ids),
        )

    def apply_invalidation_plan(self, cmd: ApplyInvalidationPlanCommand) -> CommandResult:
        return self.guard.run(cmd, lambda: self._apply_invalidation_plan(cmd))

    def _apply_invalidation_plan(self, cmd: ApplyInvalidationPlanCommand) -> CommandResult:
        self._require_open(cmd.case_id)
        plan = self.repo.get_plan(cmd.plan_id)
        if plan is None or plan.case_id != cmd.case_id:
            raise PolicyDeniedError(
                f"plan {cmd.plan_id} is not a plan of case {cmd.case_id}",
                code=ReasonCode.INVALIDATION_PLAN_INVALID,
            )
        applied, results = self.dependencies.apply_plan(
            cmd.plan_id, cmd.actor, self.verification, cmd.operation_id
        )
        return CommandResult(
            operation_id=cmd.operation_id,
            status="ok",
            summary=f"Re-ran {len(results)} of {self.total_check_slots(cmd.case_id)} checks per plan {cmd.plan_id}",
            data={
                "plan_id": cmd.plan_id,
                "produced_check_ids": list(applied.produced_check_ids),
                "reverified": [
                    {"sample_id": r.sample_id, "category": r.category.value, "status": r.status.value}
                    for r in results
                ],
                "total_check_slots": self.total_check_slots(cmd.case_id),
            },
        )

    # ==========================================================================================
    # Human decisions
    # ==========================================================================================
    def raise_pending_decision(self, cmd: RaisePendingDecisionCommand) -> CommandResult:
        return self.guard.run(cmd, lambda: self._raise_pending_decision(cmd))

    def _raise_pending_decision(self, cmd: RaisePendingDecisionCommand) -> CommandResult:
        self._require_open(cmd.case_id)
        sample = self.repo.get_sample(cmd.sample_id)
        if sample.state is not SampleState.NEEDS_HUMAN_DECISION:
            raise PolicyDeniedError(
                f"{sample.sample_id} is {sample.state.value}; no human decision is required",
                code=ReasonCode.INVALID_STATE_TRANSITION,
            )
        active = self.repo.list_requests(cmd.case_id, EvidenceRequestStatus.ACTIVE)
        if active:
            raise PolicyDeniedError(
                f"evidence recovery still in progress ({', '.join(r.request_id for r in active)}); human decision deferred",
                code=ReasonCode.EVIDENCE_RECOVERY_IN_PROGRESS,
            )
        checks = self.repo.checks_by_category(sample.sample_id, cmd.case_id)
        policy = self.policy_for(cmd.case_id)
        blocked = [c for c in policy.required_checks if checks[c].status is not CheckStatus.PASS]
        passed = [c for c in policy.required_checks if checks[c].status is CheckStatus.PASS]
        primary = checks[blocked[0]]
        issue_type = primary.reason_codes[0] if primary.reason_codes else ReasonCode.HUMAN_AUTHORITY_REQUIRED
        issue_id = f"{cmd.case_id}:{sample.sample_id}:{issue_type.value}"
        existing = self.repo.get_pending_decision(issue_id)
        if existing is not None and existing.resolved_decision_id is None:
            return CommandResult(
                operation_id=cmd.operation_id,
                status="ok",
                summary=f"Decision {issue_id} already pending",
                data={"issue_id": issue_id, "created": False, "card": existing.model_dump(mode="json")},
            )
        t = policy.temperature
        options: tuple[DecisionOption, ...] = (
            DecisionOption(
                option=HumanOption.QUARANTINE,
                required_roles=policy.quarantine_roles,
                consequence="Sample is held and is not usable for research. The hold can be reopened later "
                "for re-verification once the missing evidence exists.",
            ),
            DecisionOption(
                option=HumanOption.REJECT,
                required_roles=policy.reject_roles,
                consequence="Sample is rejected outright and will not be stored. This is irreversible, "
                "choose a hold instead if the question might still be answerable.",
            ),
        )
        if issue_type is ReasonCode.TEMPERATURE_EXCURSION and t.exception_allowed:
            options += (
                DecisionOption(
                    option=HumanOption.APPROVE_EXCEPTION,
                    required_roles=t.exception_roles,
                    consequence="Sample is accepted with a documented protocol exception; the excursion is recorded in the LIMS.",
                ),
            )
        pending = PendingDecision(
            issue_id=issue_id,
            case_id=cmd.case_id,
            sample_id=sample.sample_id,
            issue_type=issue_type,
            observed_value=primary.observed_value or "",
            expected_value=primary.expected_value or "",
            policy_clause=t.clause
            if issue_type is ReasonCode.TEMPERATURE_EXCURSION
            else "Protocol policy requires a documented human disposition.",
            evidence_refs=tuple(dict.fromkeys(r for c in checks.values() for r in c.evidence_refs)),
            passed_checks=tuple(passed),
            blocked_checks=tuple(blocked),
            options=options,
            created_at=self.clock(),
        )
        self.repo.save_pending_decision(pending)
        audit = self.repo.append_audit(
            case_id=cmd.case_id,
            event_type=AuditEventType.PENDING_DECISION_CREATED,
            actor=cmd.actor,
            tool_name="human_decision",
            operation_id=cmd.operation_id,
            summary=f"Human disposition requested for {sample.sample_id}: {issue_type.value} ({primary.observed_value})",
            reason_codes=(issue_type,),
            sample_ids=(sample.sample_id,),
            metadata={"issue_id": issue_id, "options": [o.option.value for o in options]},
        )
        return CommandResult(
            operation_id=cmd.operation_id,
            status="human_required",
            summary=f"Decision card raised for {sample.sample_id}",
            reason_codes=(issue_type,),
            data={"issue_id": issue_id, "created": True, "card": pending.model_dump(mode="json")},
            audit_event_ids=(audit.audit_event_id,),
        )

    def record_human_decision(self, cmd: RecordHumanDecisionCommand) -> CommandResult:
        return self.guard.run(cmd, lambda: self._record_human_decision(cmd))

    def _record_human_decision(self, cmd: RecordHumanDecisionCommand) -> CommandResult:
        self._require_open(cmd.case_id)
        pending = self.repo.get_pending_decision(cmd.issue_id)
        if pending is None or pending.case_id != cmd.case_id:
            raise NotFoundError(f"no pending decision {cmd.issue_id} in case {cmd.case_id}")
        if pending.resolved_decision_id is not None:
            raise PolicyDeniedError(
                f"decision {cmd.issue_id} was already resolved by {pending.resolved_decision_id}",
                code=ReasonCode.DUPLICATE_OPERATION,
            )
        option = next((o for o in pending.options if o.option is cmd.selected_option), None)
        if option is None:
            raise PolicyDeniedError(
                f"{cmd.selected_option.value} is not an offered option",
                code=ReasonCode.HUMAN_AUTHORITY_REQUIRED,
            )
        allowed_roles = set(option.required_roles) & set(
            self.policy_for(cmd.case_id).roles_for(cmd.selected_option)
        )
        ignored_client_role = cmd.client_payload.get("actor_role") or cmd.client_payload.get("role")
        if cmd.actor.role not in allowed_roles:
            self.repo.append_audit(
                case_id=cmd.case_id,
                event_type=AuditEventType.OPERATION_REJECTED,
                actor=cmd.actor,
                operation_id=cmd.operation_id,
                summary=f"{cmd.actor.actor_id} ({cmd.actor.role.value}) may not {cmd.selected_option.value} {pending.sample_id}",
                reason_codes=(ReasonCode.INSUFFICIENT_ROLE,),
                output_status="rejected",
                metadata={"client_supplied_role_ignored": ignored_client_role},
                kind=AuditKind.TOOL_ATTEMPT,
            )
            raise UnauthorizedError(
                f"role {cmd.actor.role.value} may not {cmd.selected_option.value}; requires {sorted(r.value for r in allowed_roles)}"
            )
        decision = HumanDecision(
            decision_id=self.repo.next_id("HD"),
            case_id=cmd.case_id,
            issue_id=cmd.issue_id,
            sample_id=pending.sample_id,
            actor_id=cmd.actor.actor_id,
            actor_role=cmd.actor.role,
            selected_option=cmd.selected_option,
            comment=cmd.comment,
            operation_id=cmd.operation_id,
            created_at=self.clock(),
        )
        self.repo.save_decision(decision)
        self.repo.save_pending_decision(
            pending.model_copy(update={"resolved_decision_id": decision.decision_id})
        )
        audit = self.repo.append_audit(
            case_id=cmd.case_id,
            event_type=AuditEventType.HUMAN_DECISION_RECORDED,
            actor=cmd.actor,
            tool_name="human_decision",
            operation_id=cmd.operation_id,
            summary=f"{cmd.actor.actor_id} ({cmd.actor.role.value}) chose {cmd.selected_option.value} for {pending.sample_id}",
            reason_codes=(ReasonCode.HUMAN_DECISION_APPLIED,),
            sample_ids=(pending.sample_id,),
            metadata={
                "decision_id": decision.decision_id,
                "client_supplied_role_ignored": ignored_client_role,
            },
        )
        requested = {
            HumanOption.QUARANTINE: Disposition.QUARANTINE,
            HumanOption.REJECT: Disposition.REJECT,
            HumanOption.APPROVE_EXCEPTION: Disposition.ACCEPT_WITH_EXCEPTION,
        }[cmd.selected_option]
        applied = self._request_disposition(
            RequestDispositionCommand(
                operation_id=cmd.operation_id,
                case_id=cmd.case_id,
                actor=cmd.actor,
                sample_id=pending.sample_id,
                requested=requested,
                human_decision_id=decision.decision_id,
            )
        )
        return CommandResult(
            operation_id=cmd.operation_id,
            status=applied.status,
            summary=f"Decision {decision.decision_id} recorded; {applied.summary}",
            reason_codes=applied.reason_codes,
            data={"decision_id": decision.decision_id, **applied.data},
            audit_event_ids=(audit.audit_event_id, *applied.audit_event_ids),
        )

    # ==========================================================================================
    # Trusted retry event
    # ==========================================================================================
    def retry_requested(self, cmd: RetryRequestedCommand) -> CommandResult:
        return self.guard.run(cmd, lambda: self._retry_requested(cmd))

    def _retry_requested(self, cmd: RetryRequestedCommand) -> CommandResult:
        case = self.repo.get_case(cmd.case_id)
        if case.state is CaseState.COMPLETED:
            raise CaseFinalizedError(f"case {cmd.case_id} is COMPLETED")
        sample = self.repo.get_sample(cmd.sample_id)
        checks = self.repo.checks_by_category(sample.sample_id, cmd.case_id)
        error_checks = [c for c in checks.values() if c.status is CheckStatus.ERROR]
        refusal: tuple[ReasonCode, str] | None = None
        if sample.state is not SampleState.ERROR:
            refusal = (
                ReasonCode.RETRY_NOT_PERMITTED,
                f"{sample.sample_id} is {sample.state.value}, not ERROR",
            )
        elif not error_checks or not all(RETRYABLE_ERROR_CODES & set(c.reason_codes) for c in error_checks):
            refusal = (ReasonCode.RETRY_NOT_PERMITTED, "failure is not a retryable evaluator error")
        elif any(c.status is CheckStatus.FAIL for c in checks.values()):
            refusal = (ReasonCode.RETRY_NOT_PERMITTED, "deterministic failures are never retried")
        elif self.repo.retry_count(sample.sample_id) >= MAX_RETRIES_PER_SAMPLE:
            refusal = (
                ReasonCode.RETRY_BUDGET_EXHAUSTED,
                f"retry budget of {MAX_RETRIES_PER_SAMPLE} exhausted",
            )
        if refusal is not None:
            code, why = refusal
            self.repo.append_audit(
                case_id=cmd.case_id,
                event_type=AuditEventType.RETRY_REFUSED,
                actor=cmd.actor,
                operation_id=cmd.operation_id,
                summary=f"Retry refused for {sample.sample_id}: {why}",
                reason_codes=(code,),
                sample_ids=(sample.sample_id,),
                output_status="rejected",
            )
            raise PolicyDeniedError(why, code=code)
        attempt = self.repo.increment_retry(sample.sample_id)
        attempt_id = f"{sample.sample_id}-attempt-{attempt + 1}"
        self.repo.append_audit(
            case_id=cmd.case_id,
            event_type=AuditEventType.RETRY_REQUESTED,
            actor=cmd.actor,
            operation_id=cmd.operation_id,
            summary=f"Retry {attempt}/{MAX_RETRIES_PER_SAMPLE} for {sample.sample_id} ({attempt_id}); previous errors: "
            + "; ".join(f"{c.category.value}: {c.observed_value}" for c in error_checks),
            reason_codes=tuple(dict.fromkeys(rc for c in error_checks for rc in c.reason_codes)),
            sample_ids=(sample.sample_id,),
            metadata={
                "attempt_id": attempt_id,
                "previous_check_ids": [c.check_id for c in error_checks],
                "reason": cmd.attempt_reason,
            },
        )
        if case.state is CaseState.FAILED:
            self.transitions.transition_case(
                cmd.case_id,
                CaseState.VERIFYING,
                cmd.actor,
                ReasonCode.CHECK_ERROR,
                operation_id=cmd.operation_id,
                summary="Case reopened by trusted RETRY_REQUESTED",
                reopen=True,
            )
        self.transitions.transition_sample(
            sample.sample_id,
            SampleState.PENDING,
            cmd.actor,
            ReasonCode.CHECK_ERROR,
            operation_id=cmd.operation_id,
            summary=f"{sample.sample_id}: ERROR → PENDING ({attempt_id})",
        )
        results = self.verification.run(
            cmd.case_id,
            cmd.actor,
            sample_ids=(sample.sample_id,),
            categories=tuple(c.category for c in error_checks),
            tool_name="retry",
        )
        return CommandResult(
            operation_id=cmd.operation_id,
            status="ok",
            summary=f"Retry {attempt} for {sample.sample_id}: {', '.join(f'{r.category.value}={r.status.value}' for r in results)}",
            data={
                "attempt_id": attempt_id,
                "attempt": attempt,
                "results": {r.category.value: r.status.value for r in results},
            },
        )

    # ==========================================================================================
    # Quarantine review, resolving a hold
    # ==========================================================================================
    def open_quarantine_review(self, cmd: OpenQuarantineReviewCommand) -> CommandResult:
        return self.guard.run(cmd, lambda: self._open_quarantine_review(cmd))

    def _open_quarantine_review(self, cmd: OpenQuarantineReviewCommand) -> CommandResult:
        """Return a quarantined specimen to verification so the engine can decide again.

        Deliberately NOT an acceptance. The reviewer's authority extends to reopening the question, not to
        answering it: the checks are re-derived and the policy engine reaches its own conclusion, which may
        well be quarantine again. That is the point, a hold is lifted by evidence, not by seniority.
        """
        case = self.repo.get_case(cmd.case_id)
        sample = self.repo.get_sample(cmd.sample_id)
        if sample.case_id != cmd.case_id:
            raise NotFoundError(f"sample {cmd.sample_id} is not in case {cmd.case_id}")
        if sample.state is not SampleState.QUARANTINED:
            raise PolicyDeniedError(
                f"{sample.sample_id} is {sample.state.value}, not quarantined; there is no hold to review",
                code=ReasonCode.INVALID_STATE_TRANSITION,
            )
        allowed_roles = self.policy_for(cmd.case_id).quarantine_roles
        if cmd.actor.role not in allowed_roles:
            self.repo.append_audit(
                case_id=cmd.case_id,
                event_type=AuditEventType.OPERATION_REJECTED,
                actor=cmd.actor,
                operation_id=cmd.operation_id,
                summary=f"{cmd.actor.actor_id} ({cmd.actor.role.value}) may not review the quarantine on {sample.sample_id}",
                reason_codes=(ReasonCode.INSUFFICIENT_ROLE,),
                output_status="rejected",
                kind=AuditKind.TOOL_ATTEMPT,
            )
            raise UnauthorizedError(
                f"role {cmd.actor.role.value} may not review a quarantine; "
                f"requires {sorted(r.value for r in allowed_roles)}"
            )
        if not cmd.reason.strip():
            raise PolicyDeniedError(
                "a quarantine review needs a stated reason; it is the record of why the hold was reopened",
                code=ReasonCode.HUMAN_AUTHORITY_REQUIRED,
            )

        self.repo.append_audit(
            case_id=cmd.case_id,
            event_type=AuditEventType.QUARANTINE_REVIEW_OPENED,
            actor=cmd.actor,
            operation_id=cmd.operation_id,
            summary=f"{cmd.actor.actor_id} ({cmd.actor.role.value}) reopened the quarantine on "
            f"{sample.sample_id}: {cmd.reason}",
            reason_codes=(ReasonCode.QUARANTINE_REVIEW_OPENED,),
            sample_ids=(sample.sample_id,),
            metadata={"reason": cmd.reason, "prior_disposition": Disposition.QUARANTINE.value},
        )
        if case.state is CaseState.COMPLETED:
            self.transitions.transition_case(
                cmd.case_id,
                CaseState.VERIFYING,
                cmd.actor,
                ReasonCode.QUARANTINE_REVIEW_OPENED,
                operation_id=cmd.operation_id,
                summary=f"Case reopened for the quarantine review of {sample.sample_id}",
                reopen=True,
            )
        self.transitions.transition_sample(
            sample.sample_id,
            SampleState.PENDING,
            cmd.actor,
            ReasonCode.QUARANTINE_REVIEW_OPENED,
            operation_id=cmd.operation_id,
            summary=f"{sample.sample_id}: QUARANTINED → PENDING for re-verification",
        )
        results = self.verification.run(
            cmd.case_id,
            cmd.actor,
            sample_ids=(sample.sample_id,),
            tool_name="quarantine_review",
        )
        # Ask the engine the question again straight away. Leaving the specimen in PENDING would be a
        # third state meaning "nobody has looked", which is untrue, verification has just run. This is
        # the same deterministic pipeline the agent would drive, and its answer is the engine's, not ours:
        # ALLOWED accepts, a recoverable gap goes back to waiting for evidence, and anything reserved to a
        # person returns to NEEDS_HUMAN_DECISION for a fresh decision card.
        attempted = self._request_disposition(
            RequestDispositionCommand(
                operation_id=f"{cmd.operation_id}-redecide",
                case_id=cmd.case_id,
                actor=cmd.actor,
                sample_id=sample.sample_id,
                requested=Disposition.ACCEPT,
            )
        )
        settled = self.repo.get_sample(sample.sample_id)
        if settled.state is SampleState.PENDING:
            # The engine refused acceptance outright, a hard conflict such as an accession that belongs to
            # another identity, which no evidence from the sender can undo. Put it straight back on hold
            # rather than leaving it in PENDING: the honest answer to "can this be released?" is no, and it
            # is an answer the engine reaches on its own, with no human authority involved.
            attempted = self._request_disposition(
                RequestDispositionCommand(
                    operation_id=f"{cmd.operation_id}-rehold",
                    case_id=cmd.case_id,
                    actor=cmd.actor,
                    sample_id=sample.sample_id,
                    requested=Disposition.QUARANTINE,
                )
            )
            settled = self.repo.get_sample(sample.sample_id)
        return CommandResult(
            operation_id=cmd.operation_id,
            status="ok",
            summary=f"Quarantine on {sample.sample_id} reopened; re-verified "
            + ", ".join(f"{r.category.value}={r.status.value}" for r in results)
            + f"; now {settled.state.value}",
            reason_codes=(ReasonCode.QUARANTINE_REVIEW_OPENED,),
            data={
                "sample_id": sample.sample_id,
                "results": {r.category.value: r.status.value for r in results},
                "state": settled.state.value,
                "policy_decision": attempted.data.get("decision"),
            },
        )

    # ==========================================================================================
    # Case-level stable state (priority: FAILED > WAITING_FOR_EVIDENCE > NEEDS_HUMAN_DECISION)
    # ==========================================================================================
    def recompute_case_state(self, case_id: str, actor: ActorContext) -> ShipmentCase:
        case = self.repo.get_case(case_id)
        if case.state in TERMINAL_CASE_STATES:
            return case
        samples = self.repo.list_samples(case_id)
        target: CaseState
        reason: ReasonCode
        if any(s.state is SampleState.ERROR for s in samples):
            target, reason = CaseState.FAILED, ReasonCode.CHECK_ERROR
        elif self.repo.list_requests(case_id, EvidenceRequestStatus.ACTIVE):
            target, reason = CaseState.WAITING_FOR_EVIDENCE, ReasonCode.EVIDENCE_RECOVERY_IN_PROGRESS
        elif self.repo.list_pending_decisions(case_id):
            target, reason = CaseState.NEEDS_HUMAN_DECISION, ReasonCode.HUMAN_AUTHORITY_REQUIRED
        else:
            target, reason = CaseState.VERIFYING, ReasonCode.ALL_CHECKS_PASS
        if case.state is target:
            return case
        if case.state is CaseState.CREATED:
            case = self.transitions.transition_case(
                case_id, CaseState.VERIFYING, actor, ReasonCode.ALL_CHECKS_PASS
            )
        if target is not CaseState.VERIFYING and case.state is not CaseState.VERIFYING:
            case = self.transitions.transition_case(
                case_id, CaseState.VERIFYING, actor, reason, summary="Resuming verification"
            )
        if case.state is not target:
            case = self.transitions.transition_case(case_id, target, actor, reason)
        return case

    def is_stable(self, case_id: str) -> bool:
        case = self.repo.get_case(case_id)
        return case.state in (
            CaseState.WAITING_FOR_EVIDENCE,
            CaseState.NEEDS_HUMAN_DECISION,
            CaseState.COMPLETED,
            CaseState.FAILED,
        )

    # ==========================================================================================
    # Finalization / report
    # ==========================================================================================
    def finalize(self, cmd: FinalizeCaseCommand) -> CommandResult:
        return self.guard.run(cmd, lambda: self._finalize(cmd))

    def _finalize(self, cmd: FinalizeCaseCommand) -> CommandResult:
        self._require_open(cmd.case_id)
        samples = self.repo.list_samples(cmd.case_id)
        open_samples = [s.sample_id for s in samples if s.state not in TERMINAL_SAMPLE_STATES]
        if open_samples:
            raise PolicyDeniedError(
                f"cannot finalize; samples not terminal: {', '.join(open_samples)}",
                code=ReasonCode.INVALID_STATE_TRANSITION,
            )
        if self.repo.list_requests(cmd.case_id, EvidenceRequestStatus.ACTIVE):
            raise PolicyDeniedError(
                "cannot finalize; evidence request still active",
                code=ReasonCode.EVIDENCE_RECOVERY_IN_PROGRESS,
            )
        if self.repo.list_pending_decisions(cmd.case_id):
            raise PolicyDeniedError(
                "cannot finalize; human decision still pending", code=ReasonCode.HUMAN_AUTHORITY_REQUIRED
            )
        self.recompute_case_state(cmd.case_id, cmd.actor)
        report = self.build_report(cmd.case_id)
        art = self._store(
            cmd.case_id,
            ArtifactType.AUDIT_REPORT,
            "intake-report.json",
            "application/json",
            json.dumps(report, indent=2, sort_keys=True, default=str).encode(),
        )
        self.transitions.transition_case(
            cmd.case_id,
            CaseState.COMPLETED,
            cmd.actor,
            ReasonCode.ALL_CHECKS_PASS,
            operation_id=cmd.operation_id,
            summary="Case completed",
        )
        audit = self.repo.append_audit(
            case_id=cmd.case_id,
            event_type=AuditEventType.CASE_FINALIZED,
            actor=cmd.actor,
            tool_name="finalization",
            operation_id=cmd.operation_id,
            summary=f"Final: {report['counts']['ACCEPTED']} accepted, {report['counts']['ACCEPTED_WITH_EXCEPTION']} accepted with exception, {report['counts']['QUARANTINED']} quarantined; report {art.artifact_id}",
            metadata={"report_artifact_id": art.artifact_id, "counts": report["counts"]},
        )
        return CommandResult(
            operation_id=cmd.operation_id,
            status="ok",
            summary=audit.summary,
            data={"report_artifact_id": art.artifact_id, "counts": report["counts"]},
            audit_event_ids=(audit.audit_event_id,),
        )

    def build_report(self, case_id: str) -> dict[str, Any]:
        case = self.repo.get_case(case_id)
        report_policy = self.policy_for(case_id)
        samples = self.repo.list_samples(case_id)
        counts = {s.value: 0 for s in SampleState}
        for s in samples:
            counts[s.state.value] += 1
        per_sample = []
        unauthorized = 0
        for s in samples:
            checks = self.repo.checks_by_category(s.sample_id, case_id)
            if s.state is SampleState.ACCEPTED and any(
                c.status is not CheckStatus.PASS for c in checks.values()
            ):
                unauthorized += 1
            lims = self.lims.find_by_sample(s.sample_id)
            per_sample.append(
                {
                    "sample_id": s.sample_id,
                    "state": s.state.value,
                    "disposition": s.disposition.value if s.disposition else None,
                    "checks": {
                        c.value: (checks[c].status.value if c in checks else None)
                        for c in report_policy.required_checks
                    },
                    "provisional_checks": [
                        c.value
                        for c in report_policy.required_checks
                        if c in checks and checks[c].provisional
                    ],
                    "evidence_refs": sorted({r for c in checks.values() for r in c.evidence_refs}),
                    "lims": {
                        "record_id": lims.record_id,
                        "status": lims.status,
                        "policy_evaluation_id": lims.policy_evaluation_id,
                    }
                    if lims
                    else None,
                }
            )
        audit = self.repo.list_audit(case_id)
        by_kind = {k.value: 0 for k in AuditKind}
        for a in audit:
            by_kind[a.kind.value] += 1
        return {
            "case_id": case.case_id,
            "shipment_id": case.shipment_id,
            "protocol": f"{case.protocol_id}@{case.protocol_version}",
            "policy": f"{report_policy.policy_id}@{report_policy.version}",
            "case_state": case.state.value,
            "received_at": case.received_at.isoformat(),
            "created_at": case.created_at.isoformat(),
            "counts": counts,
            "unauthorized_acceptances": unauthorized,
            "samples": per_sample,
            "evidence_requests": [
                {
                    "request_id": r.request_id,
                    "recipient": r.recipient_contact_id,
                    "status": r.status.value,
                    "requirements": [q.key() for q in r.requirements],
                }
                for r in self.repo.list_requests(case_id)
            ],
            "human_decisions": [d.model_dump(mode="json") for d in self.repo.list_decisions(case_id)],
            "policy_evaluations": len([a for a in audit if a.event_type is AuditEventType.POLICY_EVALUATED]),
            "audit_counts_by_kind": by_kind,
            "audit_events": [
                {
                    "seq": a.sequence,
                    "kind": a.kind.value,
                    "type": a.event_type.value,
                    "actor": a.actor_id,
                    "summary": a.summary,
                    "reason_codes": [c.value for c in a.reason_codes],
                }
                for a in audit
            ],
            "data_classification": "SYNTHETIC",
        }

    def snapshot(self, case_id: str) -> dict[str, Any]:
        """Compact authoritative view, the `get_case_snapshot` tool returns this."""
        case = self.repo.get_case(case_id)
        snapshot_policy = self.policy_for(case_id)
        samples = []
        for s in self.repo.list_samples(case_id):
            checks = self.repo.checks_by_category(s.sample_id, case_id)
            samples.append(
                {
                    "sample_id": s.sample_id,
                    "state": s.state.value,
                    "checks": {
                        c.value: checks[c].status.value
                        for c in snapshot_policy.required_checks
                        if c in checks
                    },
                    "blockers": [
                        {
                            "category": c.value,
                            "status": checks[c].status.value,
                            "reason_codes": [r.value for r in checks[c].reason_codes],
                            "observed": checks[c].observed_value,
                        }
                        for c in snapshot_policy.required_checks
                        if c in checks and checks[c].status is not CheckStatus.PASS
                    ],
                }
            )
        return {
            "case_id": case.case_id,
            "shipment_id": case.shipment_id,  # the agent writes to people about a shipment, not a case
            "state": case.state.value,
            "case_version": case.case_version,
            "samples": samples,
            "active_requests": [
                r.request_id for r in self.repo.list_requests(case_id, EvidenceRequestStatus.ACTIVE)
            ],
            "pending_decisions": [p.issue_id for p in self.repo.list_pending_decisions(case_id)],
            "unresolved_requirements": [
                r.model_dump(mode="json") for r in self.unresolved_requirements(case_id)
            ],
        }

    # ------------------------------------------------------------------------------------------
    def _require_open(self, case_id: str) -> ShipmentCase:
        case = self.repo.get_case(case_id)
        if case.state in TERMINAL_CASE_STATES:
            raise CaseFinalizedError(f"case {case_id} is {case.state.value}")
        return case
