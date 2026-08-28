"""DynamoDB repository, single table, every item is the JSON of a frozen domain model.

Keys:  pk = "<TYPE>#<id>", sk = "<TYPE>"          (lookup by id)
       gsi1pk = case_id, gsi1sk = "<TYPE>#<sort>"  (listing by case; GSI name: gsi1)
Counters use atomic ADD; leases use conditional writes; case saves are last-writer-wins guarded by
the command-level expected_case_version + the execution lease.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Any, TypeVar

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from pydantic import ValidationError

from ..clock import Clock, utc_now
from ..domain.enums import ArtifactType, AuditEventType, AuditKind, EvidenceRequestStatus, ReasonCode
from ..domain.errors import NotFoundError, StoredRecordUnreadableError
from ..domain.models import (
    ActorContext,
    AuditEvent,
    CheckResult,
    DomainModel,
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

GSI = "gsi1"
M = TypeVar("M", bound=DomainModel)


def _dump(model: DomainModel) -> str:
    return model.model_dump_json()


class DynamoDBRepository(Repository):
    def __init__(
        self, table_name: str, *, session: boto3.Session | None = None, clock: Clock | None = None
    ) -> None:
        self._clock: Clock = clock or utc_now
        self._table = (session or boto3.Session()).resource("dynamodb").Table(table_name)

    # ------------------------------------------------------------------------------------------
    def _put(
        self,
        pk: str,
        sk: str,
        model: DomainModel,
        case_id: str | None,
        gsi_sort: str | None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        item: dict[str, Any] = {"pk": pk, "sk": sk, "type": sk.split("#")[0], "body": _dump(model)}
        if case_id is not None and gsi_sort is not None:
            item["gsi1pk"] = case_id
            item["gsi1sk"] = gsi_sort
        if extra:
            item.update(extra)
        self._table.put_item(Item=item)

    def _get(self, pk: str, sk: str, cls: type[M]) -> M | None:
        r = self._table.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True)
        item = r.get("Item")
        return self._parse(item, cls, pk, sk) if item else None

    @staticmethod
    def _parse(item: dict[str, Any], cls: type[M], pk: str, sk: str) -> M:
        """Read one stored row, and say which row it was if it cannot be read.

        A row written before a model gained a required field cannot be validated under the model as
        it now stands. Left alone that surfaces as a bare pydantic error from wherever the read
        happened, at start-up, that is a container that exits with a traceback naming a field and
        not the record. Naming the record is the difference between a five-minute fix and an
        afternoon.
        """
        try:
            return cls.model_validate_json(item["body"])
        except ValidationError as e:
            raise StoredRecordUnreadableError(
                f"{pk} / {sk} cannot be read as {cls.__name__}: {e.errors()[0].get('msg')} "
                f"(field {'.'.join(str(p) for p in e.errors()[0].get('loc', ()))}). It was written "
                "under an older shape of this model and needs migrating or removing."
            ) from e

    def _query_case(self, case_id: str, prefix: str, cls: type[M]) -> list[M]:
        out: list[Any] = []
        kwargs: dict[str, Any] = {
            "IndexName": GSI,
            "KeyConditionExpression": Key("gsi1pk").eq(case_id) & Key("gsi1sk").begins_with(prefix),
        }
        while True:
            r = self._table.query(**kwargs)
            out.extend(self._parse(i, cls, i["pk"], i["sk"]) for i in r.get("Items", []))
            if "LastEvaluatedKey" not in r:
                return out
            kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    def _scan_type(self, type_name: str, cls: type[M]) -> list[M]:
        out: list[Any] = []
        kwargs: dict[str, Any] = {"FilterExpression": Key("type").eq(type_name)}
        while True:
            r = self._table.scan(**kwargs)
            out.extend(self._parse(i, cls, i["pk"], i["sk"]) for i in r.get("Items", []))
            if "LastEvaluatedKey" not in r:
                return out
            kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    # ids ----------------------------------------------------------------------------------------
    def next_id(self, prefix: str) -> str:
        r = self._table.update_item(
            Key={"pk": f"COUNTER#{prefix}", "sk": "COUNTER"},
            UpdateExpression="ADD n :one",
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        return f"{prefix}-{int(r['Attributes']['n']):04d}"

    def next_ids(self, prefix: str, count: int) -> list[str]:
        if count <= 0:
            return []
        r = self._table.update_item(
            Key={"pk": f"COUNTER#{prefix}", "sk": "COUNTER"},
            UpdateExpression="ADD n :c",
            ExpressionAttributeValues={":c": count},
            ReturnValues="UPDATED_NEW",
        )
        last = int(r["Attributes"]["n"])
        return [f"{prefix}-{i:04d}" for i in range(last - count + 1, last + 1)]

    def save_checks(self, checks: Sequence[CheckResult]) -> None:
        with self._table.batch_writer(overwrite_by_pkeys=["pk", "sk"]) as bw:
            for check in checks:
                for pk, sk, gsi in (
                    (
                        f"CHECKCUR#{check.sample_id}#{check.category.value}",
                        "CHECKCUR",
                        f"CHECKCUR#{check.sample_id}#{check.category.value}",
                    ),
                    (
                        f"CHECKHIST#{check.check_id}",
                        "CHECKHIST",
                        f"CHECKHIST#{check.evaluated_at.isoformat()}#{check.check_id}",
                    ),
                ):
                    bw.put_item(
                        Item={
                            "pk": pk,
                            "sk": sk,
                            "type": sk,
                            "body": _dump(check),
                            "gsi1pk": check.case_id,
                            "gsi1sk": gsi,
                        }
                    )

    # cases --------------------------------------------------------------------------------------
    def save_case(self, case: ShipmentCase) -> None:
        self._put(f"CASE#{case.case_id}", "CASE", case, case.case_id, "CASE")

    def get_case(self, case_id: str) -> ShipmentCase:
        c = self._get(f"CASE#{case_id}", "CASE", ShipmentCase)
        if c is None:
            raise NotFoundError(f"case {case_id} not found")
        return c

    def list_cases(self) -> Sequence[ShipmentCase]:
        return sorted(self._scan_type("CASE", ShipmentCase), key=lambda c: c.created_at)

    # samples ------------------------------------------------------------------------------------
    def save_sample(self, sample: Sample) -> None:
        self._put(
            f"SAMPLE#{sample.sample_id}", "SAMPLE", sample, sample.case_id, f"SAMPLE#{sample.sample_id}"
        )

    def get_sample(self, sample_id: str) -> Sample:
        s = self._get(f"SAMPLE#{sample_id}", "SAMPLE", Sample)
        if s is None:
            raise NotFoundError(f"sample {sample_id} not found")
        return s

    def list_samples(self, case_id: str) -> Sequence[Sample]:
        return sorted(self._query_case(case_id, "SAMPLE#", Sample), key=lambda s: s.sample_id)

    # checks -------------------------------------------------------------------------------------
    def save_check(self, check: CheckResult) -> None:
        self._put(
            f"CHECKCUR#{check.sample_id}#{check.category.value}",
            "CHECKCUR",
            check,
            check.case_id,
            f"CHECKCUR#{check.sample_id}#{check.category.value}",
        )
        self._put(
            f"CHECKHIST#{check.check_id}",
            "CHECKHIST",
            check,
            check.case_id,
            f"CHECKHIST#{check.evaluated_at.isoformat()}#{check.check_id}",
        )

    def current_checks(self, case_id: str, sample_id: str | None = None) -> Sequence[CheckResult]:
        prefix = f"CHECKCUR#{sample_id}#" if sample_id else "CHECKCUR#"
        return self._query_case(case_id, prefix, CheckResult)

    def check_history(self, case_id: str) -> Sequence[CheckResult]:
        return self._query_case(case_id, "CHECKHIST#", CheckResult)

    # artifacts ----------------------------------------------------------------------------------
    def save_artifact(self, artifact: EvidenceArtifact) -> None:
        self._put(
            f"ART#{artifact.artifact_id}", "ART", artifact, artifact.case_id, f"ART#{artifact.artifact_id}"
        )

    def get_artifact(self, artifact_id: str) -> EvidenceArtifact:
        a = self._get(f"ART#{artifact_id}", "ART", EvidenceArtifact)
        if a is None:
            raise NotFoundError(f"artifact {artifact_id} not found")
        return a

    def list_artifacts(
        self, case_id: str, artifact_type: ArtifactType | None = None
    ) -> Sequence[EvidenceArtifact]:
        arts = sorted(self._query_case(case_id, "ART#", EvidenceArtifact), key=lambda a: a.artifact_id)
        return [a for a in arts if artifact_type is None or a.artifact_type is artifact_type]

    # requests -----------------------------------------------------------------------------------
    def save_request(self, request: EvidenceRequest) -> None:
        self._put(f"REQ#{request.request_id}", "REQ", request, request.case_id, f"REQ#{request.request_id}")

    def get_request(self, request_id: str) -> EvidenceRequest:
        r = self._get(f"REQ#{request_id}", "REQ", EvidenceRequest)
        if r is None:
            raise NotFoundError(f"evidence request {request_id} not found")
        return r

    def list_requests(
        self, case_id: str, status: EvidenceRequestStatus | None = None
    ) -> Sequence[EvidenceRequest]:
        reqs = sorted(self._query_case(case_id, "REQ#", EvidenceRequest), key=lambda r: r.request_id)
        return [r for r in reqs if status is None or r.status is status]

    # pending / decisions ------------------------------------------------------------------------
    def save_pending_decision(self, pending: PendingDecision) -> None:
        self._put(
            f"PENDING#{pending.issue_id}", "PENDING", pending, pending.case_id, f"PENDING#{pending.issue_id}"
        )

    def get_pending_decision(self, issue_id: str) -> PendingDecision | None:
        return self._get(f"PENDING#{issue_id}", "PENDING", PendingDecision)

    def list_pending_decisions(self, case_id: str, unresolved_only: bool = True) -> Sequence[PendingDecision]:
        return [
            p
            for p in self._query_case(case_id, "PENDING#", PendingDecision)
            if not unresolved_only or p.resolved_decision_id is None
        ]

    def save_decision(self, decision: HumanDecision) -> None:
        self._put(
            f"HD#{decision.decision_id}", "HD", decision, decision.case_id, f"HD#{decision.decision_id}"
        )

    def get_decision(self, decision_id: str) -> HumanDecision:
        d = self._get(f"HD#{decision_id}", "HD", HumanDecision)
        if d is None:
            raise NotFoundError(f"decision {decision_id} not found")
        return d

    def list_decisions(self, case_id: str) -> Sequence[HumanDecision]:
        return sorted(self._query_case(case_id, "HD#", HumanDecision), key=lambda d: d.decision_id)

    # plans / retries ----------------------------------------------------------------------------
    def save_plan(self, plan: InvalidationPlan) -> None:
        self._put(f"PLAN#{plan.plan_id}", "PLAN", plan, plan.case_id, f"PLAN#{plan.plan_id}")

    def get_plan(self, plan_id: str) -> InvalidationPlan | None:
        return self._get(f"PLAN#{plan_id}", "PLAN", InvalidationPlan)

    def retry_count(self, sample_id: str) -> int:
        r = self._table.get_item(Key={"pk": f"RETRY#{sample_id}", "sk": "RETRY"}, ConsistentRead=True)
        return int(r.get("Item", {}).get("n", 0))

    def increment_retry(self, sample_id: str) -> int:
        r = self._table.update_item(
            Key={"pk": f"RETRY#{sample_id}", "sk": "RETRY"},
            UpdateExpression="ADD n :one",
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        return int(r["Attributes"]["n"])

    # evaluations --------------------------------------------------------------------------------
    def save_evaluation(self, evaluation: PolicyEvaluation) -> None:
        self._put(
            f"PE#{evaluation.evaluation_id}",
            "PE",
            evaluation,
            evaluation.case_id,
            f"PE#{evaluation.evaluation_id}",
        )

    def get_evaluation(self, evaluation_id: str) -> PolicyEvaluation | None:
        return self._get(f"PE#{evaluation_id}", "PE", PolicyEvaluation)

    # audit --------------------------------------------------------------------------------------
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
        r = self._table.update_item(
            Key={"pk": f"COUNTER#AUDIT#{case_id}", "sk": "COUNTER"},
            UpdateExpression="ADD n :one",
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        seq = int(r["Attributes"]["n"])
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
            metadata=json.loads(json.dumps(metadata or {}, default=str)),
        )
        self._put(f"AUDIT#{event.audit_event_id}", "AUDIT", event, case_id, f"AUDIT#{seq:06d}")
        return event

    def list_audit(self, case_id: str) -> Sequence[AuditEvent]:
        return sorted(self._query_case(case_id, "AUDIT#", AuditEvent), key=lambda a: a.sequence)

    # operations ---------------------------------------------------------------------------------
    def get_operation(self, operation_id: str) -> OperationRecord | None:
        return self._get(f"OP#{operation_id}", "OP", OperationRecord)

    def save_operation(self, record: OperationRecord) -> None:
        self._put(
            f"OP#{record.operation_id}",
            "OP",
            record,
            record.case_id or None,
            f"OP#{record.operation_id}" if record.case_id else None,
        )

    # studies ------------------------------------------------------------------------------------
    def save_study(self, study: Study) -> None:
        self._put(f"STUDY#{study.study_id}", "STUDY", study, "STUDIES", f"STUDY#{study.study_id}")

    def get_study(self, study_id: str) -> Study | None:
        return self._get(f"STUDY#{study_id}", "STUDY", Study)

    def list_studies(self) -> Sequence[Study]:
        return sorted(self._query_case("STUDIES", "STUDY#", Study), key=lambda s: s.study_id)

    # lab users ----------------------------------------------------------------------------------
    def save_user(self, user: LabUser) -> None:
        self._put(f"USER#{user.user_id}", "USER", user, "USERS", f"USER#{user.user_id}")

    def get_user(self, user_id: str) -> LabUser | None:
        return self._get(f"USER#{user_id}", "USER", LabUser)

    def list_users(self) -> Sequence[LabUser]:
        return sorted(self._query_case("USERS", "USER#", LabUser), key=lambda u: u.user_id)

    # intake ramp --------------------------------------------------------------------------------
    def save_announcement(self, announcement: ShipmentAnnouncement) -> None:
        self._put(f"ANN#{announcement.case_id}", "ANN", announcement, announcement.case_id, "ANN")

    def get_announcement(self, case_id: str) -> ShipmentAnnouncement | None:
        return self._get(f"ANN#{case_id}", "ANN", ShipmentAnnouncement)

    def save_receipt(self, receipt: ReceiptRecord) -> None:
        self._put(f"RCPT#{receipt.case_id}", "RCPT", receipt, receipt.case_id, "RCPT")

    def get_receipt(self, case_id: str) -> ReceiptRecord | None:
        return self._get(f"RCPT#{case_id}", "RCPT", ReceiptRecord)

    def save_batch(self, batch: StagingBatch) -> None:
        self._put(f"BATCH#{batch.batch_id}", "BATCH", batch, batch.case_id, f"BATCH#{batch.batch_id}")

    def get_batch(self, batch_id: str) -> StagingBatch | None:
        return self._get(f"BATCH#{batch_id}", "BATCH", StagingBatch)

    def open_batch(self, case_id: str) -> StagingBatch | None:
        batches = sorted(self._query_case(case_id, "BATCH#", StagingBatch), key=lambda b: b.opened_at)
        return next((b for b in batches if b.committed_at is None), None)

    def latest_batch(self, case_id: str) -> StagingBatch | None:
        batches = sorted(self._query_case(case_id, "BATCH#", StagingBatch), key=lambda b: b.opened_at)
        return batches[-1] if batches else None

    def save_scan(self, scan: ScanRecord) -> None:
        self._put(f"SCAN#{scan.scan_id}", "SCAN", scan, scan.case_id, f"SCAN#{scan.batch_id}#{scan.scan_id}")

    def list_scans(self, batch_id: str) -> Sequence[ScanRecord]:
        # A batch belongs to one case and holds at most a few hundred scans, so filtering the type is
        # cheaper than carrying a second index for it.
        scans = [s for s in self._scan_type("SCAN", ScanRecord) if s.batch_id == batch_id]
        return sorted(scans, key=lambda s: s.scanned_at)

    # lease --------------------------------------------------------------------------------------
    def acquire_lease(self, case_id: str, owner: str, ttl_seconds: int) -> bool:
        now = int(time.time())
        try:
            self._table.update_item(
                Key={"pk": f"LEASE#{case_id}", "sk": "LEASE"},
                UpdateExpression="SET lease_owner = :o, lease_expires_at = :e",
                ConditionExpression="attribute_not_exists(lease_owner) OR lease_owner = :o OR lease_expires_at < :now",
                ExpressionAttributeValues={":o": owner, ":e": now + ttl_seconds, ":now": now},
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def release_lease(self, case_id: str, owner: str) -> None:
        try:
            self._table.delete_item(
                Key={"pk": f"LEASE#{case_id}", "sk": "LEASE"},
                ConditionExpression="lease_owner = :o",
                ExpressionAttributeValues={":o": owner},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    # lims ---------------------------------------------------------------------------------------
    def save_lims_record(self, record: LimsRecord) -> None:
        self._put(f"LIMS#{record.record_id}", "LIMS", record, "LIMS", f"LIMS#{record.record_id}")

    def get_lims_record(self, record_id: str) -> LimsRecord | None:
        return self._get(f"LIMS#{record_id}", "LIMS", LimsRecord)

    def list_lims_records(self) -> Sequence[LimsRecord]:
        return sorted(self._query_case("LIMS", "LIMS#", LimsRecord), key=lambda r: r.record_id)

    # contacts -----------------------------------------------------------------------------------
    def save_contact(self, contact: SiteContact) -> None:
        self._put(
            f"CONTACT#{contact.contact_id}", "CONTACT", contact, "CONTACTS", f"CONTACT#{contact.contact_id}"
        )

    def get_contact(self, contact_id: str) -> SiteContact | None:
        return self._get(f"CONTACT#{contact_id}", "CONTACT", SiteContact)

    def list_contacts(self, shipment_id: str | None = None) -> Sequence[SiteContact]:
        return [
            c
            for c in self._query_case("CONTACTS", "CONTACT#", SiteContact)
            if shipment_id is None or shipment_id in c.shipment_ids
        ]

    # maintenance --------------------------------------------------------------------------------
    def purge_counters(self) -> int:
        """Reset every id counter (demo reset only) so a fresh demo run produces the same ids."""
        n = 0
        kwargs: dict[str, Any] = {
            "FilterExpression": Attr("pk").begins_with("COUNTER#"),
            "ProjectionExpression": "pk, sk",
        }
        while True:
            r = self._table.scan(**kwargs)
            with self._table.batch_writer() as bw:
                for i in r.get("Items", []):
                    bw.delete_item(Key={"pk": i["pk"], "sk": i["sk"]})
                    n += 1
            if "LastEvaluatedKey" not in r:
                return n
            kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    def purge_case(self, case_id: str) -> int:
        """Delete every item belonging to a case (demo reset). Returns number of items deleted."""
        n = 0
        kwargs: dict[str, Any] = {
            "IndexName": GSI,
            "KeyConditionExpression": Key("gsi1pk").eq(case_id),
            "ProjectionExpression": "pk, sk",
        }
        while True:
            r = self._table.query(**kwargs)
            with self._table.batch_writer() as bw:
                for i in r.get("Items", []):
                    bw.delete_item(Key={"pk": i["pk"], "sk": i["sk"]})
                    n += 1
            if "LastEvaluatedKey" not in r:
                break
            kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]
        for key in (f"COUNTER#AUDIT#{case_id}", f"LEASE#{case_id}"):
            self._table.delete_item(Key={"pk": key, "sk": key.split("#")[0]})
            n += 1
        return n


def ensure_table(table_name: str, session: boto3.Session | None = None) -> str:
    """Idempotently create the single table (PAY_PER_REQUEST) with the case GSI. Returns status."""
    client = (session or boto3.Session()).client("dynamodb")
    try:
        client.describe_table(TableName=table_name)
        return "exists"
    except client.exceptions.ResourceNotFoundException:
        pass
    client.create_table(
        TableName=table_name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "gsi1pk", "AttributeType": "S"},
            {"AttributeName": "gsi1sk", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": GSI,
                "KeySchema": [
                    {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                    {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    client.get_waiter("table_exists").wait(TableName=table_name)
    return "created"
