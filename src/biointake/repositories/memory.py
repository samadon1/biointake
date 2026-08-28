"""In-memory repository. Deterministic ids (per-prefix counters) so demo output is reproducible."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from ..clock import Clock, utc_now
from ..domain.enums import ArtifactType, AuditEventType, AuditKind, EvidenceRequestStatus, ReasonCode
from ..domain.errors import NotFoundError
from ..domain.models import (
    ActorContext,
    AuditEvent,
    CheckResult,
    EvidenceArtifact,
    EvidenceRequest,
    HumanDecision,
    InvalidationPlan,
    LabUser,
    LimsRecord,
    OperationRecord,
    PendingDecision,
    PolicyEvaluation,
    ReceiptRecord,
    Sample,
    ScanRecord,
    ShipmentAnnouncement,
    ShipmentCase,
    SiteContact,
    StagingBatch,
    Study,
)
from .interfaces import Repository


class InMemoryRepository(Repository):
    def __init__(self, clock: Clock | None = None) -> None:
        self._clock: Clock = clock or utc_now
        self._counters: dict[str, int] = defaultdict(int)
        self._cases: dict[str, ShipmentCase] = {}
        self._samples: dict[str, Sample] = {}
        self._checks_current: dict[tuple[str, str], CheckResult] = {}
        self._checks_history: list[CheckResult] = []
        self._artifacts: dict[str, EvidenceArtifact] = {}
        self._requests: dict[str, EvidenceRequest] = {}
        self._pending: dict[str, PendingDecision] = {}
        self._decisions: dict[str, HumanDecision] = {}
        self._evaluations: dict[str, PolicyEvaluation] = {}
        self._audit: dict[str, list[AuditEvent]] = defaultdict(list)
        self._operations: dict[str, OperationRecord] = {}
        self._contacts: dict[str, SiteContact] = {}
        self._plans: dict[str, InvalidationPlan] = {}
        self._retries: dict[str, int] = defaultdict(int)
        self._leases: dict[str, tuple[str, float]] = {}
        self._lims: dict[str, LimsRecord] = {}
        self._studies: dict[str, Study] = {}
        self._users: dict[str, LabUser] = {}
        self._announcements: dict[str, ShipmentAnnouncement] = {}
        self._receipts: dict[str, ReceiptRecord] = {}
        self._batches: dict[str, StagingBatch] = {}
        self._scans: dict[str, list[ScanRecord]] = defaultdict(list)

    # studies
    def save_study(self, study: Study) -> None:
        self._studies[study.study_id] = study

    def get_study(self, study_id: str) -> Study | None:
        return self._studies.get(study_id)

    def list_studies(self) -> Sequence[Study]:
        return sorted(self._studies.values(), key=lambda s: s.study_id)

    # lab users
    def save_user(self, user: LabUser) -> None:
        self._users[user.user_id] = user

    def get_user(self, user_id: str) -> LabUser | None:
        return self._users.get(user_id)

    def list_users(self) -> Sequence[LabUser]:
        return sorted(self._users.values(), key=lambda u: u.user_id)

    # intake ramp
    def save_announcement(self, announcement: ShipmentAnnouncement) -> None:
        self._announcements[announcement.case_id] = announcement

    def get_announcement(self, case_id: str) -> ShipmentAnnouncement | None:
        return self._announcements.get(case_id)

    def save_receipt(self, receipt: ReceiptRecord) -> None:
        self._receipts[receipt.case_id] = receipt

    def get_receipt(self, case_id: str) -> ReceiptRecord | None:
        return self._receipts.get(case_id)

    def save_batch(self, batch: StagingBatch) -> None:
        self._batches[batch.batch_id] = batch

    def get_batch(self, batch_id: str) -> StagingBatch | None:
        return self._batches.get(batch_id)

    def open_batch(self, case_id: str) -> StagingBatch | None:
        return next(
            (b for b in self._batches.values() if b.case_id == case_id and b.committed_at is None), None
        )

    def latest_batch(self, case_id: str) -> StagingBatch | None:
        batches = sorted(
            (b for b in self._batches.values() if b.case_id == case_id), key=lambda b: b.opened_at
        )
        return batches[-1] if batches else None

    def save_scan(self, scan: ScanRecord) -> None:
        # Upsert by scan_id, matching DynamoDB's put. A scan is amendable (its received quality is noticed
        # after the barcode is read), and appending a second copy would double-count the batch.
        existing = self._scans[scan.batch_id]
        for i, s in enumerate(existing):
            if s.scan_id == scan.scan_id:
                existing[i] = scan
                return
        existing.append(scan)

    def list_scans(self, batch_id: str) -> Sequence[ScanRecord]:
        return list(self._scans[batch_id])

    # lease
    def acquire_lease(self, case_id: str, owner: str, ttl_seconds: int) -> bool:
        now = self._clock().timestamp()
        held = self._leases.get(case_id)
        if held is not None and held[0] != owner and held[1] > now:
            return False
        self._leases[case_id] = (owner, now + ttl_seconds)
        return True

    def release_lease(self, case_id: str, owner: str) -> None:
        held = self._leases.get(case_id)
        if held is not None and held[0] == owner:
            del self._leases[case_id]

    # lims records
    def save_lims_record(self, record: LimsRecord) -> None:
        self._lims[record.record_id] = record

    def get_lims_record(self, record_id: str) -> LimsRecord | None:
        return self._lims.get(record_id)

    def list_lims_records(self) -> Sequence[LimsRecord]:
        return sorted(self._lims.values(), key=lambda r: r.record_id)

    # plans / retries
    def save_plan(self, plan: InvalidationPlan) -> None:
        self._plans[plan.plan_id] = plan

    def get_plan(self, plan_id: str) -> InvalidationPlan | None:
        return self._plans.get(plan_id)

    def retry_count(self, sample_id: str) -> int:
        return self._retries[sample_id]

    def increment_retry(self, sample_id: str) -> int:
        self._retries[sample_id] += 1
        return self._retries[sample_id]

    def next_id(self, prefix: str) -> str:
        self._counters[prefix] += 1
        return f"{prefix}-{self._counters[prefix]:04d}"

    # cases
    def save_case(self, case: ShipmentCase) -> None:
        self._cases[case.case_id] = case

    def get_case(self, case_id: str) -> ShipmentCase:
        try:
            return self._cases[case_id]
        except KeyError as e:
            raise NotFoundError(f"case {case_id} not found") from e

    def list_cases(self) -> Sequence[ShipmentCase]:
        return list(self._cases.values())

    # samples
    def save_sample(self, sample: Sample) -> None:
        self._samples[sample.sample_id] = sample

    def get_sample(self, sample_id: str) -> Sample:
        try:
            return self._samples[sample_id]
        except KeyError as e:
            raise NotFoundError(f"sample {sample_id} not found") from e

    def list_samples(self, case_id: str) -> Sequence[Sample]:
        return sorted((s for s in self._samples.values() if s.case_id == case_id), key=lambda s: s.sample_id)

    # checks
    def save_check(self, check: CheckResult) -> None:
        self._checks_current[(check.sample_id, check.category.value)] = check
        self._checks_history.append(check)

    def current_checks(self, case_id: str, sample_id: str | None = None) -> Sequence[CheckResult]:
        return [
            c
            for c in self._checks_current.values()
            if c.case_id == case_id and (sample_id is None or c.sample_id == sample_id)
        ]

    def check_history(self, case_id: str) -> Sequence[CheckResult]:
        return [c for c in self._checks_history if c.case_id == case_id]

    # artifacts
    def save_artifact(self, artifact: EvidenceArtifact) -> None:
        self._artifacts[artifact.artifact_id] = artifact

    def get_artifact(self, artifact_id: str) -> EvidenceArtifact:
        try:
            return self._artifacts[artifact_id]
        except KeyError as e:
            raise NotFoundError(f"artifact {artifact_id} not found") from e

    def list_artifacts(
        self, case_id: str, artifact_type: ArtifactType | None = None
    ) -> Sequence[EvidenceArtifact]:
        return [
            a
            for a in self._artifacts.values()
            if a.case_id == case_id and (artifact_type is None or a.artifact_type is artifact_type)
        ]

    # requests
    def save_request(self, request: EvidenceRequest) -> None:
        self._requests[request.request_id] = request

    def get_request(self, request_id: str) -> EvidenceRequest:
        try:
            return self._requests[request_id]
        except KeyError as e:
            raise NotFoundError(f"evidence request {request_id} not found") from e

    def list_requests(
        self, case_id: str, status: EvidenceRequestStatus | None = None
    ) -> Sequence[EvidenceRequest]:
        return [
            r
            for r in self._requests.values()
            if r.case_id == case_id and (status is None or r.status is status)
        ]

    # pending / decisions
    def save_pending_decision(self, pending: PendingDecision) -> None:
        self._pending[pending.issue_id] = pending

    def get_pending_decision(self, issue_id: str) -> PendingDecision | None:
        return self._pending.get(issue_id)

    def list_pending_decisions(self, case_id: str, unresolved_only: bool = True) -> Sequence[PendingDecision]:
        return [
            p
            for p in self._pending.values()
            if p.case_id == case_id and (not unresolved_only or p.resolved_decision_id is None)
        ]

    def save_decision(self, decision: HumanDecision) -> None:
        self._decisions[decision.decision_id] = decision

    def get_decision(self, decision_id: str) -> HumanDecision:
        try:
            return self._decisions[decision_id]
        except KeyError as e:
            raise NotFoundError(f"decision {decision_id} not found") from e

    def list_decisions(self, case_id: str) -> Sequence[HumanDecision]:
        return [d for d in self._decisions.values() if d.case_id == case_id]

    # evaluations
    def save_evaluation(self, evaluation: PolicyEvaluation) -> None:
        self._evaluations[evaluation.evaluation_id] = evaluation

    def get_evaluation(self, evaluation_id: str) -> PolicyEvaluation | None:
        return self._evaluations.get(evaluation_id)

    # audit
    def append_audit(
        self,
        *,
        case_id: str,
        event_type: AuditEventType,
        actor: ActorContext,
        summary: str,
        reason_codes: tuple[ReasonCode, ...] = (),
        sample_ids: tuple[str, ...] = (),
        tool_name: str | None = None,
        operation_id: str | None = None,
        input_digest: str | None = None,
        output_status: str = "ok",
        metadata: dict[str, Any] | None = None,
        kind: AuditKind = AuditKind.DOMAIN_EFFECT,
    ) -> AuditEvent:
        seq = len(self._audit[case_id]) + 1
        event = AuditEvent(
            audit_event_id=f"AUD-{case_id}-{seq:04d}",
            sequence=seq,
            case_id=case_id,
            event_type=event_type,
            kind=kind,
            actor_type=actor.actor_type,
            actor_id=actor.actor_id,
            summary=summary,
            tool_name=tool_name,
            operation_id=operation_id,
            input_digest=input_digest,
            output_status=output_status,
            reason_codes=reason_codes,
            sample_ids=sample_ids,
            timestamp=self._clock(),
            metadata=metadata or {},
        )
        self._audit[case_id].append(event)
        return event

    def list_audit(self, case_id: str) -> Sequence[AuditEvent]:
        return list(self._audit[case_id])

    # operations
    def get_operation(self, operation_id: str) -> OperationRecord | None:
        return self._operations.get(operation_id)

    def save_operation(self, record: OperationRecord) -> None:
        self._operations[record.operation_id] = record

    # contacts
    def save_contact(self, contact: SiteContact) -> None:
        self._contacts[contact.contact_id] = contact

    def get_contact(self, contact_id: str) -> SiteContact | None:
        return self._contacts.get(contact_id)

    def list_contacts(self, shipment_id: str | None = None) -> Sequence[SiteContact]:
        return [c for c in self._contacts.values() if shipment_id is None or shipment_id in c.shipment_ids]
