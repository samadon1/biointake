"""Mutating commands, trusted operation-id derivation, and the idempotency guard.

Operation ids are never chosen by a model. They are derived in trusted code from the case, the
triggering event, the command type and the canonical *semantic* payload (reserved authority fields
stripped). Same id + same payload → the original result; same id + different payload → rejected;
same id under another case or command type → rejected.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from ..clock import Clock
from .enums import AuditEventType, AuditKind, Disposition, HumanOption, ReasonCode
from .errors import DuplicateOperationError, VersionConflictError
from .models import ActorContext, CommandResult, EvidenceRequirement, OperationRecord

OPERATION_NAMESPACE = uuid.UUID("2c1f6b0e-4b8a-4a7e-9d3c-5e6f7a8b9c0d")

# Fields a model must never control. Stripped before hashing; rejected by the tool layer.
RESERVED_AUTHORITY_FIELDS: frozenset[str] = frozenset(
    {
        "operation_id",
        "actor",
        "actor_id",
        "actor_role",
        "role",
        "tenant_id",
        "account_id",
        "case_id",
        "case_version",
        "expected_case_version",
        "policy_evaluation_id",
        "evaluation_id",
        "destination",
        "email",
        "recipient_email",
        "upload_token",
        "human_decision_id",
    }
)


def canonical_semantic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop reserved authority fields (recursively) so they can never influence identity or effect."""

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in sorted(value.items()) if k not in RESERVED_AUTHORITY_FIELDS}
        if isinstance(value, list | tuple):
            return [scrub(v) for v in value]
        return value

    return cast(dict[str, Any], scrub(payload))


def semantic_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(canonical_semantic_payload(payload), sort_keys=True, default=str).encode()
    ).hexdigest()


def derive_operation_id(case_id: str, event_id: str, command_type: str, payload: dict[str, Any]) -> str:
    """UUIDv5 over trusted context + canonical semantic payload. Scoped by case and command type."""
    material = "|".join((case_id, event_id, command_type, semantic_hash(payload)))
    return f"OP-{uuid.uuid5(OPERATION_NAMESPACE, material)}"


class Command(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation_id: str
    case_id: str
    expected_case_version: int | None = None
    actor: ActorContext
    reason_code: ReasonCode | None = None

    @property
    def command_type(self) -> str:
        return type(self).__name__

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"operation_id", "expected_case_version"})

    def payload_hash(self) -> str:
        """Semantic payload digest. `expected_case_version` is a concurrency hint, not command
        semantics, so a retried/duplicated event with a newer version still replays cleanly."""
        return hashlib.sha256(
            json.dumps(self.semantic_payload(), sort_keys=True, default=str).encode()
        ).hexdigest()


class RequestDispositionCommand(Command):
    sample_id: str
    requested: Disposition
    evidence_refs: tuple[str, ...] = ()
    human_decision_id: str | None = None


class OpenQuarantineReviewCommand(Command):
    """Take a specimen out of quarantine and put it back through verification.

    Quarantine is a hold pending resolution; the resolution is a separate, later act. This command performs
    only the first half of it, returning the specimen to PENDING so its checks are re-derived from whatever
    evidence now exists. It cannot accept anything. Whether the specimen ends up accepted, held again or
    rejected is the policy engine's answer, not the reviewer's."""

    sample_id: str
    reason: str


class CreateEvidenceRequestCommand(Command):
    recipient_contact_id: str
    requirements: tuple[EvidenceRequirement, ...]
    note_for_recipient: str = ""


class IncomingArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    filename: str
    mime_type: str
    content: bytes
    declared_sha256: str | None = None


class ProposedCorrection(BaseModel):
    """What a model (or a structured sender form) *proposes*. Never authoritative on its own."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    manifest_row: int
    manifest_value: str
    corrected_value: str
    sender_statement: str = ""


class ReceiveEvidenceCommand(Command):
    request_id: str
    upload_token: str
    submitted_by_contact_id: str
    artifacts: tuple[IncomingArtifact, ...]
    proposed_corrections: tuple[ProposedCorrection, ...] = ()


class ApplyInvalidationPlanCommand(Command):
    plan_id: str


class RaisePendingDecisionCommand(Command):
    sample_id: str


class RecordHumanDecisionCommand(Command):
    issue_id: str
    selected_option: HumanOption
    comment: str = ""
    client_payload: dict[str, Any] = Field(default_factory=dict)  # untrusted; any role inside is ignored


class RetryRequestedCommand(Command):
    """Trusted event: the only way ERROR → PENDING (and FAILED → VERIFYING) can happen."""

    sample_id: str
    attempt_reason: str = ""


class FinalizeCaseCommand(Command):
    pass


class IdempotencyGuard:
    """Same operation_id + same payload → the original result. Same id + different payload → rejected."""

    def __init__(self, repo: Repository, clock: Clock) -> None:
        self._repo = repo
        self._clock = clock

    def run(self, command: Command, fn: Callable[[], CommandResult]) -> CommandResult:
        digest = command.payload_hash()
        existing = self._repo.get_operation(command.operation_id)
        if existing is not None:
            if existing.case_id != command.case_id or existing.command_type != command.command_type:
                self._reject(
                    command, ReasonCode.OPERATION_SCOPE_MISMATCH, "reused under another case or command type"
                )
                raise DuplicateOperationError(
                    f"operation {command.operation_id} belongs to {existing.command_type} on {existing.case_id}",
                    code=ReasonCode.OPERATION_SCOPE_MISMATCH,
                )
            if existing.payload_hash != digest:
                self._reject(command, ReasonCode.DUPLICATE_OPERATION, "reused with a different payload")
                raise DuplicateOperationError(
                    f"operation {command.operation_id} was already executed with a different payload"
                )
            self._repo.append_audit(
                case_id=command.case_id,
                event_type=AuditEventType.OPERATION_REPLAYED,
                actor=command.actor,
                summary=f"operation {command.operation_id} replayed; original result returned",
                operation_id=command.operation_id,
                output_status="replayed",
                kind=AuditKind.TOOL_ATTEMPT,
            )
            return CommandResult.model_validate(existing.result)

        if command.expected_case_version is not None:
            case = self._repo.get_case(command.case_id)
            if case.case_version != command.expected_case_version:
                raise VersionConflictError(
                    f"case {command.case_id} is at version {case.case_version}, "
                    f"command expected {command.expected_case_version}"
                )

        result = fn()
        self._repo.save_operation(
            OperationRecord(
                operation_id=command.operation_id,
                case_id=command.case_id,
                command_type=command.command_type,
                payload_hash=digest,
                result=result.model_dump(mode="json"),
                recorded_at=self._clock(),
            )
        )
        return result

    def _reject(self, command: Command, code: ReasonCode, detail: str) -> None:
        self._repo.append_audit(
            case_id=command.case_id,
            event_type=AuditEventType.OPERATION_REJECTED,
            actor=command.actor,
            summary=f"operation {command.operation_id} {detail}",
            reason_codes=(code,),
            operation_id=command.operation_id,
            output_status="rejected",
            kind=AuditKind.TOOL_ATTEMPT,
        )


from ..repositories.interfaces import Repository  # noqa: E402
