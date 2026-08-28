"""Demonstration LIMS adapter.

Clearly NOT a production LIMS. It exists to make two things real: reconciliation against
pre-existing records (including a barcode collision) and writes that are refused unless a stored,
ALLOWED policy evaluation accompanies them. Identities are never overwritten.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from ..domain.enums import CheckStatus, Disposition, PolicyDecision, ReasonCode
from ..domain.errors import LimsWriteRefusedError
from ..domain.models import LimsRecord, PolicyEvaluation, Sample

if TYPE_CHECKING:
    from ..repositories.interfaces import Repository

# A rejected specimen is still written. The LIMS is the record of what the lab did with the material, and
# "we received this and rejected it" is exactly the fact a site, an auditor or a monitor comes back asking
# about. Silence would read as never having arrived.
_STATUS_FOR = {
    Disposition.ACCEPT: "ACCEPTED",
    Disposition.ACCEPT_WITH_EXCEPTION: "ACCEPTED_WITH_EXCEPTION",
    Disposition.QUARANTINE: "QUARANTINED",
    Disposition.REJECT: "REJECTED",
}


class LimsReconciliation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: CheckStatus
    reason_codes: tuple[ReasonCode, ...]
    observed: str
    expected: str
    record_ids: tuple[str, ...]
    summary: str


def parse_lims_records(data: bytes) -> list[LimsRecord]:
    return [LimsRecord.model_validate(r) for r in json.loads(data)["records"]]


class LimsStore(ABC):
    @abstractmethod
    def all(self) -> Sequence[LimsRecord]: ...
    @abstractmethod
    def get(self, record_id: str) -> LimsRecord | None: ...
    @abstractmethod
    def put(self, record: LimsRecord) -> None: ...


class InMemoryLimsStore(LimsStore):
    def __init__(self, records: Sequence[LimsRecord] = ()) -> None:
        self._records: dict[str, LimsRecord] = {r.record_id: r for r in records}

    def all(self) -> Sequence[LimsRecord]:
        return sorted(self._records.values(), key=lambda r: r.record_id)

    def get(self, record_id: str) -> LimsRecord | None:
        return self._records.get(record_id)

    def put(self, record: LimsRecord) -> None:
        self._records[record.record_id] = record


class RepositoryLimsStore(LimsStore):
    """LIMS records persisted through the Repository (DynamoDB in deployment), read-through cached
    for the lifetime of one process/invocation (writes go straight through and refresh the cache)."""

    def __init__(self, repo: Repository) -> None:
        self._repo = repo
        self._cache: dict[str, LimsRecord] | None = None

    def _load(self) -> dict[str, LimsRecord]:
        if self._cache is None:
            self._cache = {r.record_id: r for r in self._repo.list_lims_records()}
        return self._cache

    def all(self) -> Sequence[LimsRecord]:
        return sorted(self._load().values(), key=lambda r: r.record_id)

    def get(self, record_id: str) -> LimsRecord | None:
        return self._load().get(record_id)

    def put(self, record: LimsRecord) -> None:
        self._repo.save_lims_record(record)
        self._load()[record.record_id] = record


class DemoLims:
    def __init__(self, records: Sequence[LimsRecord] = (), store: LimsStore | None = None) -> None:
        self._store: LimsStore = store if store is not None else InMemoryLimsStore(records)
        self.write_count = 0

    @classmethod
    def from_store(cls, store: LimsStore) -> DemoLims:
        return cls(store=store)

    def seed(self, records: Sequence[LimsRecord]) -> None:
        for r in records:
            if self._store.get(r.record_id) is None:
                self._store.put(r)

    # -- reads -----------------------------------------------------------------------------------
    def records(self) -> list[LimsRecord]:
        return sorted(self._store.all(), key=lambda r: r.record_id)

    def get(self, record_id: str) -> LimsRecord | None:
        return self._store.get(record_id)

    def find_by_sample(self, sample_id: str) -> LimsRecord | None:
        return next((r for r in self._store.all() if r.sample_id == sample_id), None)

    def find_by_barcode(self, barcode: str) -> list[LimsRecord]:
        return [r for r in self._store.all() if r.barcode == barcode]

    def reconcile(self, sample: Sample, protocol_id: str) -> LimsReconciliation:
        expected_record = self.find_by_sample(sample.sample_id)
        colliding = [r for r in self.find_by_barcode(sample.barcode) if r.sample_id != sample.sample_id]
        if colliding:
            other = colliding[0]
            return LimsReconciliation(
                status=CheckStatus.FAIL,
                reason_codes=(ReasonCode.BARCODE_COLLISION,),
                observed=f"barcode {sample.barcode} already belongs to {other.record_id} ({other.sample_id}, {other.status})",
                expected=f"barcode {sample.barcode} unique to {sample.sample_id}",
                record_ids=tuple(r.record_id for r in colliding)
                + ((expected_record.record_id,) if expected_record else ()),
                summary=f"Barcode collision with existing record {other.record_id}; identity must not be overwritten.",
            )
        if expected_record is None:
            return LimsReconciliation(
                status=CheckStatus.UNAVAILABLE,
                reason_codes=(ReasonCode.LIMS_RECORD_MISSING,),
                observed="no expected record",
                expected=f"pre-registered record for {sample.sample_id}",
                record_ids=(),
                summary=f"No LIMS record pre-registered for {sample.sample_id}.",
            )
        if (
            expected_record.protocol_id != protocol_id
            or expected_record.specimen_type != sample.specimen_type
        ):
            return LimsReconciliation(
                status=CheckStatus.FAIL,
                reason_codes=(ReasonCode.PROTOCOL_MISMATCH,),
                observed=f"{expected_record.protocol_id}/{expected_record.specimen_type}",
                expected=f"{protocol_id}/{sample.specimen_type}",
                record_ids=(expected_record.record_id,),
                summary="LIMS record protocol or specimen type differs from intake.",
            )
        return LimsReconciliation(
            status=CheckStatus.PASS,
            reason_codes=(),
            observed=f"{expected_record.record_id} ({expected_record.status})",
            expected=f"pre-registered record for {sample.sample_id}",
            record_ids=(expected_record.record_id,),
            summary=f"Matches pre-registered record {expected_record.record_id}.",
        )

    # -- writes ----------------------------------------------------------------------------------
    def write_disposition(
        self,
        sample: Sample,
        evaluation: PolicyEvaluation,
        operation_id: str,
        lookup_evaluation: Callable[[str], PolicyEvaluation | None],
        freshness_check: Callable[[PolicyEvaluation], tuple[bool, str]] | None = None,
    ) -> LimsRecord:
        stored = lookup_evaluation(evaluation.evaluation_id)
        if stored is None or stored != evaluation:
            raise LimsWriteRefusedError("policy evaluation is not on record; write refused")
        if evaluation.consumed_by_operation_id not in (None, operation_id):
            raise LimsWriteRefusedError(
                f"policy evaluation already consumed by {evaluation.consumed_by_operation_id}; write refused",
                code=ReasonCode.EVALUATION_CONSUMED,
            )
        if freshness_check is not None:
            fresh, why = freshness_check(evaluation)
            if not fresh:
                raise LimsWriteRefusedError(
                    f"stale policy evaluation: {why}", code=ReasonCode.STALE_POLICY_EVALUATION
                )
        if evaluation.decision is not PolicyDecision.ALLOWED:
            raise LimsWriteRefusedError(f"policy decision is {evaluation.decision.value}; write refused")
        if evaluation.sample_id != sample.sample_id:
            raise LimsWriteRefusedError("policy evaluation is for a different sample; write refused")
        if evaluation.requested_disposition not in _STATUS_FOR:
            raise LimsWriteRefusedError("disposition not writable")
        record = self.find_by_sample(sample.sample_id)
        if record is None:
            raise LimsWriteRefusedError(
                f"no LIMS record for {sample.sample_id}; identity creation is not permitted here"
            )
        if record.last_operation_id == operation_id:
            return record  # idempotent replay
        if any(r.sample_id != sample.sample_id for r in self.find_by_barcode(sample.barcode)) and (
            evaluation.requested_disposition not in (Disposition.QUARANTINE, Disposition.REJECT)
        ):
            raise LimsWriteRefusedError("barcode belongs to another identity; overwrite refused")
        updated = record.model_copy(
            update={
                "status": _STATUS_FOR[evaluation.requested_disposition],
                "disposition": evaluation.requested_disposition,
                "policy_evaluation_id": evaluation.evaluation_id,
                "last_operation_id": operation_id,
                "history": record.history
                + (f"{operation_id}:{_STATUS_FOR[evaluation.requested_disposition]}",),
            }
        )
        self._store.put(updated)
        self.write_count += 1
        return updated
