"""Typed domain errors. Every error carries a ReasonCode so audit events stay machine-readable."""

from __future__ import annotations

from .enums import ReasonCode


class BioIntakeError(Exception):
    code: ReasonCode = ReasonCode.CHECK_ERROR

    def __init__(self, message: str, *, code: ReasonCode | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message


class NotFoundError(BioIntakeError):
    code = ReasonCode.NOT_FOUND


class InvalidTransitionError(BioIntakeError):
    code = ReasonCode.INVALID_STATE_TRANSITION


class CaseFinalizedError(BioIntakeError):
    code = ReasonCode.CASE_FINALIZED


class DuplicateOperationError(BioIntakeError):
    code = ReasonCode.DUPLICATE_OPERATION


class VersionConflictError(BioIntakeError):
    code = ReasonCode.VERSION_CONFLICT


class PolicyDeniedError(BioIntakeError):
    """Raised when a requested action is not permitted by the deterministic policy engine."""


class UnauthorizedError(BioIntakeError):
    code = ReasonCode.INSUFFICIENT_ROLE


class EvidenceRejectedError(BioIntakeError):
    code = ReasonCode.EVIDENCE_UNMATCHED


class RecipientNotVerifiedError(BioIntakeError):
    code = ReasonCode.RECIPIENT_NOT_VERIFIED


class LimsWriteRefusedError(BioIntakeError):
    code = ReasonCode.HUMAN_AUTHORITY_REQUIRED


class StoredRecordUnreadableError(BioIntakeError):
    """A persisted row cannot be validated under the model as it now stands.

    Almost always a record written before a model gained a required field. It is separate from
    NotFoundError because the record is there: discarding it would lose data, and treating it as
    absent would quietly recreate it with different contents.
    """
