"""Repository contract. Local = in-memory; deployed = DynamoDB (Phase 4). Tests never need AWS."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from ..domain.enums import (
    ArtifactType,
    AuditEventType,
    AuditKind,
    CheckCategory,
    EvidenceRequestStatus,
    ReasonCode,
)
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


class Repository(ABC):
    # ids
    @abstractmethod
    def next_id(self, prefix: str) -> str: ...

    def next_ids(self, prefix: str, count: int) -> list[str]:
        """Allocate `count` ids in one round trip where the backend supports it."""
        return [self.next_id(prefix) for _ in range(count)]

    def save_checks(self, checks: Sequence[CheckResult]) -> None:
        for c in checks:
            self.save_check(c)

    # cases
    @abstractmethod
    def save_case(self, case: ShipmentCase) -> None: ...
    @abstractmethod
    def get_case(self, case_id: str) -> ShipmentCase: ...
    @abstractmethod
    def list_cases(self) -> Sequence[ShipmentCase]: ...

    # samples
    @abstractmethod
    def save_sample(self, sample: Sample) -> None: ...
    @abstractmethod
    def get_sample(self, sample_id: str) -> Sample: ...
    @abstractmethod
    def list_samples(self, case_id: str) -> Sequence[Sample]: ...

    # checks (current + history)
    @abstractmethod
    def save_check(self, check: CheckResult) -> None: ...
    @abstractmethod
    def current_checks(self, case_id: str, sample_id: str | None = None) -> Sequence[CheckResult]: ...
    @abstractmethod
    def check_history(self, case_id: str) -> Sequence[CheckResult]: ...

    def checks_by_category(self, sample_id: str, case_id: str) -> dict[CheckCategory, CheckResult]:
        return {c.category: c for c in self.current_checks(case_id, sample_id)}

    # artifacts
    @abstractmethod
    def save_artifact(self, artifact: EvidenceArtifact) -> None: ...
    @abstractmethod
    def get_artifact(self, artifact_id: str) -> EvidenceArtifact: ...
    @abstractmethod
    def list_artifacts(
        self, case_id: str, artifact_type: ArtifactType | None = None
    ) -> Sequence[EvidenceArtifact]: ...

    # evidence requests
    @abstractmethod
    def save_request(self, request: EvidenceRequest) -> None: ...
    @abstractmethod
    def get_request(self, request_id: str) -> EvidenceRequest: ...
    @abstractmethod
    def list_requests(
        self, case_id: str, status: EvidenceRequestStatus | None = None
    ) -> Sequence[EvidenceRequest]: ...

    # pending decisions / human decisions
    @abstractmethod
    def save_pending_decision(self, pending: PendingDecision) -> None: ...
    @abstractmethod
    def get_pending_decision(self, issue_id: str) -> PendingDecision | None: ...
    @abstractmethod
    def list_pending_decisions(
        self, case_id: str, unresolved_only: bool = True
    ) -> Sequence[PendingDecision]: ...
    @abstractmethod
    def save_decision(self, decision: HumanDecision) -> None: ...
    @abstractmethod
    def get_decision(self, decision_id: str) -> HumanDecision: ...
    @abstractmethod
    def list_decisions(self, case_id: str) -> Sequence[HumanDecision]: ...

    # invalidation plans
    @abstractmethod
    def save_plan(self, plan: InvalidationPlan) -> None: ...
    @abstractmethod
    def get_plan(self, plan_id: str) -> InvalidationPlan | None: ...

    # retry ledger
    @abstractmethod
    def retry_count(self, sample_id: str) -> int: ...
    @abstractmethod
    def increment_retry(self, sample_id: str) -> int: ...

    # policy evaluations
    @abstractmethod
    def save_evaluation(self, evaluation: PolicyEvaluation) -> None: ...
    @abstractmethod
    def get_evaluation(self, evaluation_id: str) -> PolicyEvaluation | None: ...

    # audit
    @abstractmethod
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
    ) -> AuditEvent: ...
    @abstractmethod
    def list_audit(self, case_id: str) -> Sequence[AuditEvent]: ...

    # idempotency
    @abstractmethod
    def get_operation(self, operation_id: str) -> OperationRecord | None: ...
    @abstractmethod
    def save_operation(self, record: OperationRecord) -> None: ...

    # studies (protocol configuration a lab owns; replaces the hardcoded policy)
    @abstractmethod
    def save_study(self, study: Study) -> None: ...
    @abstractmethod
    def get_study(self, study_id: str) -> Study | None: ...
    @abstractmethod
    def list_studies(self) -> Sequence[Study]: ...

    # the intake ramp: announcement → receipt → scans → staging batch
    @abstractmethod
    def save_announcement(self, announcement: ShipmentAnnouncement) -> None: ...
    @abstractmethod
    def get_announcement(self, case_id: str) -> ShipmentAnnouncement | None: ...
    @abstractmethod
    def save_receipt(self, receipt: ReceiptRecord) -> None: ...
    @abstractmethod
    def get_receipt(self, case_id: str) -> ReceiptRecord | None: ...
    @abstractmethod
    def save_batch(self, batch: StagingBatch) -> None: ...
    @abstractmethod
    def get_batch(self, batch_id: str) -> StagingBatch | None: ...
    @abstractmethod
    def open_batch(self, case_id: str) -> StagingBatch | None: ...

    @abstractmethod
    def latest_batch(self, case_id: str) -> StagingBatch | None:
        """The most recent batch for a case, open or committed.

        `open_batch` answers "what can still be scanned into"; this answers "what happened", so the receiving
        bench can still show the grid after commit rather than going blank."""
        ...

    @abstractmethod
    def save_scan(self, scan: ScanRecord) -> None: ...
    @abstractmethod
    def list_scans(self, batch_id: str) -> Sequence[ScanRecord]: ...

    # execution lease (cross-process mutual exclusion per case)
    @abstractmethod
    def acquire_lease(self, case_id: str, owner: str, ttl_seconds: int) -> bool: ...
    @abstractmethod
    def release_lease(self, case_id: str, owner: str) -> None: ...

    # demo LIMS records (persisted alongside the case data so every process sees the same LIMS)
    @abstractmethod
    def save_lims_record(self, record: LimsRecord) -> None: ...
    @abstractmethod
    def get_lims_record(self, record_id: str) -> LimsRecord | None: ...
    @abstractmethod
    def list_lims_records(self) -> Sequence[LimsRecord]: ...

    # contacts
    @abstractmethod
    def save_user(self, user: LabUser) -> None: ...

    @abstractmethod
    def get_user(self, user_id: str) -> LabUser | None: ...

    @abstractmethod
    def list_users(self) -> Sequence[LabUser]: ...

    @abstractmethod
    def save_contact(self, contact: SiteContact) -> None: ...
    @abstractmethod
    def get_contact(self, contact_id: str) -> SiteContact | None: ...
    @abstractmethod
    def list_contacts(self, shipment_id: str | None = None) -> Sequence[SiteContact]: ...
