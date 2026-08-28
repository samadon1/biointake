"""Immutable domain records.

Every model is frozen: state can only change by producing a new record through the
transition service (see state_machine.py). Direct attribute assignment raises.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    ActorRole,
    ActorType,
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
    PackageCondition,
    PolicyDecision,
    ReasonCode,
    ReceivedQuality,
    RequirementType,
    SampleState,
    ScanOutcome,
)
from .policies import ProtocolPolicy


class DomainModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=False)


class ActorContext(DomainModel):
    """Trusted actor identity. Constructed by server code only, never from a client payload."""

    actor_type: ActorType
    actor_id: str
    role: ActorRole

    @classmethod
    def system(cls, actor_id: str = "biointake-system") -> ActorContext:
        return cls(actor_type=ActorType.SYSTEM, actor_id=actor_id, role=ActorRole.SYSTEM)

    @classmethod
    def agent(cls, actor_id: str = "biointake-agent") -> ActorContext:
        return cls(actor_type=ActorType.AGENT, actor_id=actor_id, role=ActorRole.AGENT)


class LabUser(DomainModel):
    """Someone who works at the receiving lab and may act on cases.

    Only the SHA-256 of the user's token is stored. A stolen database yields no usable credential,
    and the server cannot show anyone their token after it is issued.
    """

    user_id: str
    display_name: str
    role: ActorRole
    token_sha256: str
    active: bool = True
    created_at: datetime
    # A credential that never ends means a leaked one never ends either, and revoking by hand
    # requires somebody to notice. None is for a deployment that has chosen not to expire them.
    expires_at: datetime | None = None

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def context(self) -> ActorContext:
        return ActorContext(
            actor_type=ActorType.SYSTEM if self.role is ActorRole.SYSTEM else ActorType.HUMAN,
            actor_id=self.user_id,
            role=self.role,
        )


class SiteContact(DomainModel):
    """A verified communication destination. The only kind of recipient the system will message."""

    contact_id: str
    site_id: str
    display_name: str
    destination: str  # e.g. an email address; resolved server-side, never supplied by the model
    shipment_ids: tuple[str, ...]
    role: ActorRole = ActorRole.SITE_CONTACT
    active: bool = True


class Study(DomainModel):
    """A protocol a lab receives shipments against. This is what `default_policy()` used to hardcode:
    a lab configures it once, and every acceptance decision is evaluated against the version recorded here.

    Segment toggles come from ADR 0003, the rules genuinely fork between research and diagnostic receipt.
    """

    study_id: str
    name: str
    protocol_id: str
    policy_version: str
    policy: ProtocolPolicy
    """The acceptance criteria themselves, versioned and owned by the lab.

    ISO 20387 §7.3.2.2 requires a biobank to define the acceptance criteria for biological material and
    verify them on reception. Until now those criteria were a hardcoded default_policy(), which meant a
    real lab's specimens were judged against a protocol it had never seen, let alone written. They live
    here so that a study is a thing a lab authors."""
    site_ids: tuple[str, ...] = ()
    # INFERRED (ADR 0003): a research biobank may relabel with a documented reason (CAP BAP.03100);
    # diagnostic receipt forbids it after arrival (ISO/TS 20658 §17.3.2). Default is the research branch.
    relabelling_permitted: bool = True
    # INFERRED: academic-biobank branch, in a sponsored trial the sponsor decides usability (CAP BAP.01700).
    exception_approval_role: ActorRole = ActorRole.PRINCIPAL_INVESTIGATOR
    # Pattern A reconciles then stores; Pattern B (frozen shipments, IARC SOP 01 §5.6) stores immediately and
    # reconciles after 24h thermal stabilisation. Hard-wiring either is wrong for half the field.
    reconcile_before_storage: bool = True
    created_at: datetime
    updated_at: datetime


class ManifestLine(DomainModel):
    """One expected specimen, as declared by the sending site before the shipment leaves."""

    row: int
    sample_id: str
    participant_reference: str
    specimen_type: str
    container_id: str
    collection_timestamp: datetime | None = None
    notes: str = ""


class ShipmentAnnouncement(DomainModel):
    """Advance notification. Mandatory in practice, not a courtesy: the shipper notifies and the recipient
    confirms capacity and staffing before the courier is booked (ISBER L4.2; CAP BAP.13200)."""

    announcement_id: str
    case_id: str
    shipment_id: str
    study_id: str
    sender_site_id: str
    announced_by_contact_id: str
    courier: str = ""
    tracking_reference: str = ""
    shipped_at: datetime | None = None
    expected_arrival: datetime | None = None
    container_count: int = 1
    logger_ids: tuple[str, ...] = ()
    shipping_condition: str = ""  # e.g. "dry ice", "ambient", "LN2 dry shipper" (ISO 20387 Annex A.3)
    expected_lines: tuple[ManifestLine, ...] = ()
    manifest_artifact_id: str | None = None
    announced_at: datetime
    accepted: bool = True
    rejection_reasons: tuple[ReasonCode, ...] = ()


class ReceiptRecord(DomainModel):
    """The receipt event. ISO/TS 20658 §17.4 makes date+time of receipt and the identity of the receiver
    mandatory; ISBER §J6 adds package and refrigerant condition; ISO 20387 Annex A.3 adds the temperature at
    reception. Condition grading is the three-state form used by the NCI BPV checklist."""

    receipt_id: str
    case_id: str
    received_at: datetime
    received_by_actor_id: str
    received_by_role: ActorRole
    package_condition: PackageCondition = PackageCondition.ACCEPTABLE
    condition_notes: str = ""
    package_count_received: int = 1
    package_count_expected: int = 1
    refrigerant_condition: str = ""  # "dry ice remaining ~2kg", a sanctioned substitute for a logger reading
    temperature_at_reception_c: float | None = None
    seal_intact: bool = True
    logger_artifact_ids: tuple[str, ...] = ()
    recorded_at: datetime


class ScanRecord(DomainModel):
    """One scan against the manifest-derived expected rows. The manifest defines the rows and the scanner
    fills a single column (Nautilus), so discrepancy detection falls out of the interaction itself."""

    scan_id: str
    case_id: str
    batch_id: str
    scanned_value: str
    received_quality: ReceivedQuality = ReceivedQuality.ACCEPTABLE
    """How this tube looked on arrival. Defaults to acceptable because most tubes are, and a bench that
    demands a dropdown per specimen for 400 specimens will simply be filled in wrongly."""
    encoded_barcode: str = ""
    """The machine-readable accession on the label, when it differs from the human-readable identifier.

    Site labels frequently carry a 2D code holding a longer site-assigned accession while the eye-readable
    text is a short sample id. The two are not interchangeable: the accession is what a LIMS deduplicates on,
    so collapsing it into the sample id silently destroys duplicate-accession detection."""
    matched_row: int | None
    matched_sample_id: str | None
    outcome: ScanOutcome
    container_id: str = ""
    scanned_by_actor_id: str
    scanned_at: datetime


class StagingBatch(DomainModel):
    """Scans land here first and must be explicitly committed; nothing is written to inventory directly (BSI).
    Committing is what creates the samples the agent then reconciles."""

    batch_id: str
    case_id: str
    opened_at: datetime
    opened_by_actor_id: str
    committed_at: datetime | None = None
    committed_by_actor_id: str | None = None
    committed_sample_ids: tuple[str, ...] = ()


class ShipmentCase(DomainModel):
    case_id: str
    shipment_id: str
    protocol_id: str
    protocol_version: str
    sender_site_id: str
    received_at: datetime
    state: CaseState = CaseState.CREATED
    agent_session_id: str
    study_id: str = ""
    expected_sample_count: int
    observed_sample_count: int = 0
    case_version: int = 0
    execution_lease: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class Sample(DomainModel):
    sample_id: str
    case_id: str
    barcode: str
    specimen_type: str
    container_id: str
    logger_id: str | None = None
    received_quality: ReceivedQuality = ReceivedQuality.ACCEPTABLE
    """Carried from the scan and never recomputed: what the receiving technician saw is the fact of record,
    and a downstream researcher needs it even when every other check passed."""
    manifest_row: int | None = None
    participant_reference: str | None = None
    collection_timestamp: datetime | None = None
    expected_protocol_id: str
    state: SampleState = SampleState.PENDING
    disposition: Disposition | None = None
    lims_record_id: str | None = None
    sample_version: int = 0
    updated_at: datetime


class CheckResult(DomainModel):
    check_id: str
    case_id: str
    sample_id: str
    category: CheckCategory
    status: CheckStatus
    reason_codes: tuple[ReasonCode, ...] = ()
    observed_value: str | None = None
    expected_value: str | None = None
    evidence_refs: tuple[str, ...] = ()
    rule_version: str
    evaluator: str
    evaluated_at: datetime
    summary: str = ""
    # dependency metadata, proves why a result is (still) valid
    evidence_dependency_ids: tuple[str, ...] = ()
    source_record_versions: dict[str, str] = Field(default_factory=dict)
    input_fingerprint: str = ""
    policy_version: str = ""
    provisional: bool = False  # evaluated through a tentative (unconfirmed) row association


class EvidenceArtifact(DomainModel):
    artifact_id: str
    case_id: str
    artifact_type: ArtifactType
    storage_uri: str
    sha256: str
    mime_type: str
    source: str  # "intake_package" | "sender_upload" | "system"
    original_filename: str
    received_at: datetime
    validation_status: ArtifactValidation = ArtifactValidation.PENDING
    request_id: str | None = None
    submitted_by_contact_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceRequirement(DomainModel):
    requirement_type: RequirementType
    sample_id: str  # empty when the requirement is a document belonging to the whole shipment
    description: str

    @property
    def is_shipment_wide(self) -> bool:
        return not self.sample_id

    def key(self) -> str:
        return f"{self.requirement_type.value}:{self.sample_id}"


class EvidenceRequest(DomainModel):
    request_id: str
    case_id: str
    recipient_contact_id: str
    requirements: tuple[EvidenceRequirement, ...]
    affected_sample_ids: tuple[str, ...]
    status: EvidenceRequestStatus = EvidenceRequestStatus.ACTIVE
    fingerprint: str
    upload_token: str
    subject: str
    body: str
    sent_at: datetime
    satisfied_at: datetime | None = None
    expires_at: datetime
    satisfied_requirement_keys: tuple[str, ...] = ()
    # Whether the message reached the recipient, and what the channel said. A request that was
    # filed but never sent must not look the same as one that was delivered.
    delivered: bool = False
    delivery_channel: str = ""
    delivery_detail: str = ""


class DecisionOption(DomainModel):
    option: HumanOption
    required_roles: tuple[ActorRole, ...]
    consequence: str


class PendingDecision(DomainModel):
    """The human-facing decision card. Upserted idempotently on issue_id."""

    issue_id: str
    case_id: str
    sample_id: str
    issue_type: ReasonCode
    observed_value: str
    expected_value: str
    policy_clause: str
    evidence_refs: tuple[str, ...]
    passed_checks: tuple[CheckCategory, ...]
    blocked_checks: tuple[CheckCategory, ...]
    options: tuple[DecisionOption, ...]
    created_at: datetime
    interrupt_id: str | None = None
    resolved_decision_id: str | None = None


class HumanDecision(DomainModel):
    decision_id: str
    case_id: str
    issue_id: str
    sample_id: str
    actor_id: str
    actor_role: ActorRole
    selected_option: HumanOption
    comment: str = ""
    operation_id: str
    created_at: datetime


class PolicyEvaluation(DomainModel):
    evaluation_id: str
    policy_id: str
    policy_version: str
    case_id: str
    sample_id: str
    requested_disposition: Disposition
    decision: PolicyDecision
    blocking_checks: tuple[CheckCategory, ...] = ()
    reason_codes: tuple[ReasonCode, ...] = ()
    human_decision_id: str | None = None
    evaluated_at: datetime
    # freshness binding, a LIMS write is refused if any of these no longer match current state
    case_version: int = -1
    sample_version: int = -1
    check_set_digest: str = ""
    evidence_snapshot_digest: str = ""
    consumed_by_operation_id: str | None = None


class InvalidationPlan(DomainModel):
    """Deterministic list of check results invalidated by newly admitted evidence."""

    plan_id: str
    case_id: str
    evidence_ids: tuple[str, ...]
    invalidated_check_ids: tuple[str, ...]
    reasons_by_check: dict[str, str]
    retained_provisional_check_ids: tuple[str, ...] = ()
    created_at: datetime
    case_version: int
    digest: str = ""
    applied_operation_id: str | None = None
    produced_check_ids: tuple[str, ...] = ()


class AuditEvent(DomainModel):
    audit_event_id: str
    sequence: int
    case_id: str
    event_type: AuditEventType
    kind: AuditKind = AuditKind.DOMAIN_EFFECT
    actor_type: ActorType
    actor_id: str
    summary: str
    tool_name: str | None = None
    operation_id: str | None = None
    input_digest: str | None = None
    output_status: str = "ok"
    reason_codes: tuple[ReasonCode, ...] = ()
    sample_ids: tuple[str, ...] = ()
    trace_id: str | None = None
    timestamp: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class LimsRecord(DomainModel):
    record_id: str
    barcode: str
    sample_id: str | None
    protocol_id: str
    specimen_type: str
    status: str  # "EXPECTED" | "ACCEPTED" | "ACCEPTED_WITH_EXCEPTION" | "QUARANTINED" | "ARCHIVED"
    disposition: Disposition | None = None
    policy_evaluation_id: str | None = None
    last_operation_id: str | None = None
    history: tuple[str, ...] = ()


class OperationRecord(DomainModel):
    operation_id: str
    case_id: str = ""
    command_type: str = ""
    payload_hash: str
    result: dict[str, Any]
    recorded_at: datetime


class CommandResult(DomainModel):
    operation_id: str
    status: str  # "ok" | "replayed" | "denied" | "waiting" | "human_required" | "error"
    summary: str
    reason_codes: tuple[ReasonCode, ...] = ()
    data: dict[str, Any] = Field(default_factory=dict)
    audit_event_ids: tuple[str, ...] = ()
