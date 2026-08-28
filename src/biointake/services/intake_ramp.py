"""The intake ramp: how a shipment actually enters the system.

Four steps precede the agent, and each is a real event with its own record (ADR 0004):

    ANNOUNCED ──► RECEIVED ──► (scans into a staging batch) ──► VERIFYING
       │             │                                             ▲
       │             │                                             └── committing the batch creates the samples
       │             └── receipt record: condition, package count, refrigerant, logger files
       └── advance notification: manifest validated against the study before the courier is booked

Everything here is a file, a form or a scan. A small biobank has email, a spreadsheet and a handheld scanner;
it does not have an integration budget, so nothing in this path requires one.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..clock import Clock
from ..domain.enums import (
    ActorRole,
    ArtifactType,
    ArtifactValidation,
    AuditEventType,
    CaseState,
    PackageCondition,
    ReasonCode,
    ReceivedQuality,
    ScanOutcome,
)
from ..domain.errors import NotFoundError, PolicyDeniedError
from ..domain.models import (
    ActorContext,
    EvidenceArtifact,
    ManifestLine,
    ReceiptRecord,
    Sample,
    ScanRecord,
    ShipmentAnnouncement,
    ShipmentCase,
    StagingBatch,
    Study,
)
from ..domain.policies import ProtocolPolicy
from ..domain.state_machine import TransitionService
from ..repositories.interfaces import Repository
from ..services.manifest import REQUIRED_MANIFEST_COLUMNS, ManifestParseError, is_near_match, parse_manifest
from ..storage.interfaces import ArtifactStorage


class ManifestValidation(BaseModel):
    """The result of checking a site's manifest against the study, BEFORE anything ships."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    accepted: bool
    lines: tuple[ManifestLine, ...]
    problems: tuple[str, ...]
    """What must be fixed before the manifest can be accepted."""
    reason_codes: tuple[ReasonCode, ...]
    warnings: tuple[str, ...] = ()
    """Worth telling the site, but not grounds for rejection. A file the lab can work with is accepted."""

    @property
    def summary(self) -> str:
        if self.accepted:
            return f"{len(self.lines)} specimens declared; manifest matches the study"
        return "; ".join(self.problems)


class ExpectedRow(BaseModel):
    """One row of the receiving grid. The manifest defines these; the scanner fills the barcode column."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    row: int
    sample_id: str
    participant_reference: str
    specimen_type: str
    container_id: str
    notes: str
    scanned_value: str | None = None
    outcome: ScanOutcome | None = None
    received_quality: ReceivedQuality | None = None
    encoded_barcode: str | None = None
    scanned_at: datetime | None = None


class ScanResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    outcome: ScanOutcome
    scanned_value: str
    matched_row: int | None
    matched_sample_id: str | None
    message: str


class IntakeRampService:
    def __init__(self, repo: Repository, storage: ArtifactStorage, clock: Clock) -> None:
        self._repo = repo
        self._storage = storage
        self._clock = clock
        self._transitions = TransitionService(repo, clock)

    def _store_artifact(
        self,
        case_id: str,
        artifact_type: ArtifactType,
        filename: str,
        mime_type: str,
        data: bytes,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> EvidenceArtifact:
        uri, digest = self._storage.put(case_id, filename, data)
        art = EvidenceArtifact(
            artifact_id=self._repo.next_id("ART"),
            case_id=case_id,
            artifact_type=artifact_type,
            storage_uri=uri,
            sha256=digest,
            mime_type=mime_type,
            source=source,
            original_filename=filename,
            received_at=self._clock(),
            validation_status=ArtifactValidation.VALID,
            metadata=metadata or {},
        )
        self._repo.save_artifact(art)
        return art

    # ==========================================================================================
    # Studies, configuration a lab owns, replacing the hardcoded policy
    # ==========================================================================================
    def save_study(self, study: Study, actor: ActorContext) -> Study:
        """Record a study, and record who wrote it.

        Authoring the acceptance criteria is the most consequential configuration act in this system: every
        later decision is judged against them, so a change here is worth more scrutiny than any single
        disposition. The audit log is keyed by case, and a study belongs to no case, so it is filed under
        the study's own id and shows up in any export that walks the log.
        """
        self._repo.save_study(study)
        self._repo.append_audit(
            case_id=f"study:{study.study_id}",
            event_type=AuditEventType.STUDY_SAVED,
            actor=actor,
            summary=f"{actor.actor_id} ({actor.role.value}) saved study {study.study_id} "
            f"with policy {study.policy.policy_id}@{study.policy.version}",
            metadata={
                "study_id": study.study_id,
                "policy_id": study.policy.policy_id,
                "policy_version": study.policy.version,
                "allowed_specimen_types": list(study.policy.allowed_specimen_types),
                "temperature": f"{study.policy.temperature.min_c}-{study.policy.temperature.max_c} C",
            },
        )
        return study

    def ensure_default_study(self, policy: ProtocolPolicy) -> Study:
        """Seed a study from the packaged policy so a fresh install has something to receive against."""
        existing = self._repo.get_study(policy.protocol_id)
        if existing is not None:
            return existing
        now = self._clock()
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
        # Recorded like any other study. An auditor asking where a study came from should find an
        # answer for the seeded one too, rather than a study that appears from nowhere.
        self.save_study(study, ActorContext.system("lab-configuration"))
        return study

    # ==========================================================================================
    # 1. Advance notification
    # ==========================================================================================
    def validate_manifest(self, manifest_csv: bytes, policy: ProtocolPolicy) -> ManifestValidation:
        """Check a site's manifest against the study before the courier is booked.

        Catching a wrong specimen type or a malformed file here costs an email; catching it after the box
        has shipped costs a cold-chain excursion.
        """
        problems: list[str] = []
        codes: list[ReasonCode] = []
        try:
            parsed = parse_manifest(manifest_csv)
            rows, row_problems = list(parsed.rows), list(parsed.problems)
        except ManifestParseError as e:
            return ManifestValidation(
                accepted=False,
                lines=(),
                problems=(str(e), f"required columns: {', '.join(REQUIRED_MANIFEST_COLUMNS)}"),
                reason_codes=(ReasonCode.MANIFEST_REJECTED,),
            )
        problems.extend(row_problems)
        warnings: list[str] = []
        if parsed.ignored_columns:
            # Reported, not rejected: the file is usable, and a site that ships an extra column should be
            # told we did not read it rather than have it silently vanish or have the file bounced.
            warnings.append(
                f"columns not read: {', '.join(parsed.ignored_columns)}. Anything recorded there will not reach the lab."
            )
        if row_problems:
            codes.append(ReasonCode.MANIFEST_REJECTED)
        if not rows:
            return ManifestValidation(
                accepted=False,
                lines=(),
                problems=("the manifest declares no specimens",),
                reason_codes=(ReasonCode.MANIFEST_REJECTED,),
            )
        allowed = {t.upper() for t in policy.allowed_specimen_types}
        # Name the rows, the way the duplicate-identifier problem below already does. "SERUM is not
        # allowed" leaves the site to find it in twelve rows; "SERUM on row 7" is a thing they can fix.
        bad_rows: dict[str, list[int]] = {}
        for r in rows:
            if r.specimen_type.upper() not in allowed:
                bad_rows.setdefault(r.specimen_type, []).append(r.row)
        if bad_rows:
            named = ", ".join(
                f"{t} on {'rows' if len(nums) > 1 else 'row'} {', '.join(str(n) for n in nums)}"
                for t, nums in sorted(bad_rows.items())
            )
            problems.append(
                f"specimen types not permitted by {policy.protocol_id}: {named} (allowed: {', '.join(sorted(allowed))})"
            )
            codes.append(ReasonCode.SPECIMEN_TYPE_NOT_IN_STUDY)
        seen: dict[str, int] = {}
        for r in rows:
            if r.sample_id in seen:
                problems.append(f"duplicate identifier {r.sample_id} on rows {seen[r.sample_id]} and {r.row}")
                codes.append(ReasonCode.LABEL_DUPLICATE)
            seen[r.sample_id] = r.row
        lines = tuple(
            ManifestLine(
                row=r.row,
                sample_id=r.sample_id,
                participant_reference=r.participant_reference,
                specimen_type=r.specimen_type,
                container_id=r.container_id,
                collection_timestamp=r.collection_timestamp,
                notes=r.notes,
            )
            for r in rows
        )
        return ManifestValidation(
            accepted=not codes,
            lines=lines,
            problems=tuple(problems),
            reason_codes=tuple(dict.fromkeys(codes)),
            warnings=tuple(warnings),
        )

    def announce(
        self,
        *,
        case_id: str,
        shipment_id: str,
        study: Study,
        policy: ProtocolPolicy,
        sender_site_id: str,
        announced_by_contact_id: str,
        manifest_csv: bytes,
        courier: str = "",
        tracking_reference: str = "",
        shipped_at: datetime | None = None,
        expected_arrival: datetime | None = None,
        container_count: int = 1,
        logger_ids: tuple[str, ...] = (),
        shipping_condition: str = "",
        custody_log: bytes | None = None,
        consent_records: bytes | None = None,
        actor: ActorContext | None = None,
    ) -> tuple[ShipmentCase, ShipmentAnnouncement, ManifestValidation]:
        actor = actor or ActorContext.system("intake-ramp")
        validation = self.validate_manifest(manifest_csv, policy)
        if not validation.accepted:
            raise PolicyDeniedError(
                f"manifest rejected: {validation.summary}", code=ReasonCode.MANIFEST_REJECTED
            )
        try:
            self._repo.get_case(case_id)
            raise PolicyDeniedError(
                f"shipment {shipment_id} has already been announced",
                code=ReasonCode.SHIPMENT_ALREADY_ANNOUNCED,
            )
        except NotFoundError:
            pass

        # The contact who announces a shipment is the contact for it. Without this the shipment arrives
        # with nobody the agent is allowed to write to, and every request for missing evidence is denied
        # for want of a recipient. The directory still gates who may be named: an unknown or deactivated
        # contact is refused here, and the agent still cannot supply an address of its own.
        announcer = self._repo.get_contact(announced_by_contact_id)
        if announcer is None or not announcer.active:
            raise PolicyDeniedError(
                f"{announced_by_contact_id} is not a verified site contact",
                code=ReasonCode.RECIPIENT_NOT_VERIFIED,
            )
        if shipment_id not in announcer.shipment_ids:
            self._repo.save_contact(
                announcer.model_copy(update={"shipment_ids": (*announcer.shipment_ids, shipment_id)})
            )

        now = self._clock()
        import uuid

        case = ShipmentCase(
            case_id=case_id,
            shipment_id=shipment_id,
            protocol_id=study.protocol_id,
            protocol_version=study.policy_version,
            sender_site_id=sender_site_id,
            received_at=expected_arrival or now,
            agent_session_id=f"{case_id}-{uuid.uuid4()}"[:96],
            study_id=study.study_id,
            expected_sample_count=len(validation.lines),
            created_at=now,
            updated_at=now,
        )
        self._repo.save_case(case)
        uri, digest = self._storage.put(case_id, "manifest.csv", manifest_csv)

        artifact = EvidenceArtifact(
            artifact_id=self._repo.next_id("ART"),
            case_id=case_id,
            artifact_type=ArtifactType.MANIFEST,
            storage_uri=uri,
            sha256=digest,
            mime_type="text/csv",
            source="sender_upload",
            original_filename="manifest.csv",
            received_at=now,
            validation_status=ArtifactValidation.VALID,
            metadata={"announced_by": announced_by_contact_id},
        )

        self._repo.save_artifact(artifact)

        # Three documents the checks need, arriving from where they actually come from.
        #
        # The protocol is the lab's own acceptance criteria, so it is rendered from the study rather than
        # uploaded: the document and the rules that judge the specimens are then the same thing, hashed
        # together, and a check can cite the exact version it was evaluated against.
        self._store_artifact(
            case_id,
            ArtifactType.PROTOCOL,
            f"{study.protocol_id}.json",
            "application/json",
            study.policy.to_json().encode(),
            source="study_configuration",
            metadata={"study_id": study.study_id, "policy_version": study.policy.version},
        )
        # The custody log and the consent evidence belong to the site. Custody travels with the box, and
        # consent is held by whoever enrolled the participant, so both arrive with the announcement.
        if custody_log:
            self._store_artifact(
                case_id,
                ArtifactType.CUSTODY_LOG,
                "chain-of-custody.json",
                "application/json",
                custody_log,
                source="sender_upload",
                metadata={"announced_by": announced_by_contact_id},
            )
        if consent_records:
            self._store_artifact(
                case_id,
                ArtifactType.CONSENT_RECORDS,
                "consent-records.json",
                "application/json",
                consent_records,
                source="sender_upload",
                metadata={"announced_by": announced_by_contact_id},
            )

        announcement = ShipmentAnnouncement(
            announcement_id=self._repo.next_id("ANN"),
            case_id=case_id,
            shipment_id=shipment_id,
            study_id=study.study_id,
            sender_site_id=sender_site_id,
            announced_by_contact_id=announced_by_contact_id,
            courier=courier,
            tracking_reference=tracking_reference,
            shipped_at=shipped_at,
            expected_arrival=expected_arrival,
            container_count=container_count,
            logger_ids=logger_ids,
            shipping_condition=shipping_condition,
            expected_lines=validation.lines,
            manifest_artifact_id=artifact.artifact_id,
            announced_at=now,
        )
        self._repo.save_announcement(announcement)
        self._repo.append_audit(
            case_id=case_id,
            event_type=AuditEventType.SHIPMENT_ANNOUNCED,
            actor=actor,
            summary=f"{sender_site_id} announced {shipment_id}: {len(validation.lines)} specimens, "
            f"{container_count} container(s), {shipping_condition or 'condition unstated'}"
            + (f", logger(s) {', '.join(logger_ids)}" if logger_ids else ""),
            metadata={
                "announcement_id": announcement.announcement_id,
                "courier": courier,
                "tracking_reference": tracking_reference,
                "manifest_artifact_id": artifact.artifact_id,
            },
        )
        case = self._transitions.transition_case(
            case_id,
            CaseState.ANNOUNCED,
            actor,
            ReasonCode.ALL_CHECKS_PASS,
            summary="Advance notification accepted",
        )
        return case, announcement, validation

    # ==========================================================================================
    # 2. Physical receipt
    # ==========================================================================================
    def record_receipt(
        self,
        *,
        case_id: str,
        actor: ActorContext,
        package_condition: PackageCondition = PackageCondition.ACCEPTABLE,
        condition_notes: str = "",
        package_count_received: int = 1,
        refrigerant_condition: str = "",
        temperature_at_reception_c: float | None = None,
        seal_intact: bool = True,
        logger_files: tuple[tuple[str, bytes], ...] = (),
    ) -> ReceiptRecord:
        case = self._repo.get_case(case_id)
        if case.state is not CaseState.ANNOUNCED:
            raise PolicyDeniedError(
                f"case {case_id} is {case.state.value}; receipt is recorded once, on arrival",
                code=ReasonCode.INVALID_STATE_TRANSITION,
            )
        announcement = self._repo.get_announcement(case_id)
        now = self._clock()
        logger_ids: list[str] = []

        for filename, data in logger_files:
            uri, digest = self._storage.put(case_id, filename, data)
            logger_id = filename.rsplit(".", 1)[0]
            art = EvidenceArtifact(
                artifact_id=self._repo.next_id("ART"),
                case_id=case_id,
                artifact_type=ArtifactType.TEMPERATURE_LOG,
                storage_uri=uri,
                sha256=digest,
                mime_type="text/csv",
                source="receiving",
                original_filename=filename,
                received_at=now,
                validation_status=ArtifactValidation.VALID,
                metadata={"logger_id": logger_id},
            )
            self._repo.save_artifact(art)
            logger_ids.append(art.artifact_id)

        receipt = ReceiptRecord(
            receipt_id=self._repo.next_id("RCPT"),
            case_id=case_id,
            received_at=now,
            received_by_actor_id=actor.actor_id,
            received_by_role=actor.role,
            package_condition=package_condition,
            condition_notes=condition_notes,
            package_count_received=package_count_received,
            package_count_expected=announcement.container_count if announcement else 1,
            refrigerant_condition=refrigerant_condition,
            temperature_at_reception_c=temperature_at_reception_c,
            seal_intact=seal_intact,
            logger_artifact_ids=tuple(logger_ids),
            recorded_at=now,
        )
        self._repo.save_receipt(receipt)
        codes: list[ReasonCode] = []
        if package_condition is not PackageCondition.ACCEPTABLE:
            codes.append(ReasonCode.PACKAGE_DAMAGED)
        self._repo.append_audit(
            case_id=case_id,
            event_type=AuditEventType.SHIPMENT_RECEIVED,
            actor=actor,
            summary=f"{actor.actor_id} took custody: {package_count_received} of {receipt.package_count_expected} container(s), "
            f"condition {package_condition.value.replace('_', ' ').lower()}, seal {'intact' if seal_intact else 'BROKEN'}"
            + (f", {refrigerant_condition}" if refrigerant_condition else "")
            + (f", {len(logger_ids)} logger file(s)" if logger_ids else ""),
            reason_codes=tuple(codes),
            metadata={"receipt_id": receipt.receipt_id, "logger_artifact_ids": logger_ids},
        )
        self._repo.save_case(case.model_copy(update={"received_at": now, "updated_at": now}))
        self._transitions.transition_case(
            case_id, CaseState.RECEIVED, actor, ReasonCode.ALL_CHECKS_PASS, summary="Physical custody taken"
        )
        return receipt

    # ==========================================================================================
    # 3. Scanning against the manifest
    # ==========================================================================================
    def open_batch(self, case_id: str, actor: ActorContext) -> StagingBatch:
        existing = self._repo.open_batch(case_id)
        if existing is not None:
            return existing
        batch = StagingBatch(
            batch_id=self._repo.next_id("BATCH"),
            case_id=case_id,
            opened_at=self._clock(),
            opened_by_actor_id=actor.actor_id,
        )
        self._repo.save_batch(batch)
        return batch

    def expected_rows(self, case_id: str) -> list[ExpectedRow]:
        """The receiving grid: one row per expected specimen, annotated with any scan against it."""
        announcement = self._repo.get_announcement(case_id)
        if announcement is None:
            return []
        batch = self._repo.latest_batch(case_id)
        scans = {
            s.matched_row: s
            for s in (self._repo.list_scans(batch.batch_id) if batch else [])
            if s.matched_row is not None
        }
        return [
            ExpectedRow(
                row=line.row,
                sample_id=line.sample_id,
                participant_reference=line.participant_reference,
                specimen_type=line.specimen_type,
                container_id=line.container_id,
                notes=line.notes,
                scanned_value=scans[line.row].scanned_value if line.row in scans else None,
                outcome=scans[line.row].outcome if line.row in scans else None,
                received_quality=scans[line.row].received_quality if line.row in scans else None,
                encoded_barcode=scans[line.row].encoded_barcode if line.row in scans else None,
                scanned_at=scans[line.row].scanned_at if line.row in scans else None,
            )
            for line in announcement.expected_lines
        ]

    def scan(
        self,
        case_id: str,
        value: str,
        actor: ActorContext,
        container_id: str = "",
        encoded_barcode: str = "",
        received_quality: ReceivedQuality = ReceivedQuality.ACCEPTABLE,
    ) -> ScanResult:
        """Record one scan. A near-match is recorded as such rather than silently corrected: an identifier that
        differs only by a confusable glyph is exactly the case the agent must escalate to the sender."""
        announcement = self._repo.get_announcement(case_id)
        if announcement is None:
            raise NotFoundError(f"case {case_id} has no announcement to scan against")
        case = self._repo.get_case(case_id)
        if case.state is not CaseState.RECEIVED:
            raise PolicyDeniedError(
                f"case {case_id} is {case.state.value}; scanning happens after receipt is recorded",
                code=ReasonCode.INVALID_STATE_TRANSITION,
            )
        batch = self.open_batch(case_id, actor)
        value = value.strip()
        already = {s.matched_row for s in self._repo.list_scans(batch.batch_id) if s.matched_row is not None}

        exact = next((line for line in announcement.expected_lines if line.sample_id == value), None)
        near = next(
            (
                line
                for line in announcement.expected_lines
                if line.row not in already and is_near_match(line.sample_id, value)
            ),
            None,
        )
        if exact is not None and exact.row in already:
            outcome, row, sid, message = (
                ScanOutcome.DUPLICATE,
                exact.row,
                exact.sample_id,
                f"{value} was already scanned",
            )
        elif exact is not None:
            outcome, row, sid, message = (
                ScanOutcome.MATCHED,
                exact.row,
                exact.sample_id,
                f"row {exact.row} matched",
            )
        elif near is not None:
            outcome, row, sid, message = (
                ScanOutcome.NEAR_MATCH,
                near.row,
                value,
                f"row {near.row} reads '{near.sample_id}' but the tube reads '{value}', needs sender confirmation",
            )
        else:
            outcome, row, sid, message = (
                ScanOutcome.UNEXPECTED,
                None,
                value,
                f"{value} is not on the manifest",
            )

        scan = ScanRecord(
            scan_id=self._repo.next_id("SCAN"),
            case_id=case_id,
            batch_id=batch.batch_id,
            scanned_value=value,
            encoded_barcode=encoded_barcode.strip(),
            received_quality=received_quality,
            matched_row=row,
            matched_sample_id=sid,
            outcome=outcome,
            container_id=container_id,
            scanned_by_actor_id=actor.actor_id,
            scanned_at=self._clock(),
        )
        self._repo.save_scan(scan)
        codes = {
            ScanOutcome.UNEXPECTED: (ReasonCode.SCAN_UNEXPECTED_SPECIMEN,),
            ScanOutcome.DUPLICATE: (ReasonCode.SCAN_DUPLICATE,),
            ScanOutcome.NEAR_MATCH: (ReasonCode.MANIFEST_IDENTIFIER_NEAR_MATCH,),
        }.get(outcome, ())
        self._repo.append_audit(
            case_id=case_id,
            event_type=AuditEventType.SCAN_RECORDED,
            actor=actor,
            summary=f"scanned {value}: {message}",
            reason_codes=codes,
            metadata={"batch_id": batch.batch_id, "outcome": outcome.value, "row": row},
        )
        return ScanResult(
            outcome=outcome, scanned_value=value, matched_row=row, matched_sample_id=sid, message=message
        )

    def scan_many(
        self,
        case_id: str,
        values: Sequence[str],
        actor: ActorContext,
        container_id: str = "",
    ) -> list[ScanResult]:
        """Record a whole column of identifiers at once.

        Nobody receives four hundred tubes one form at a time, and the rack scanners labs already own do
        not talk to us; they produce a CSV that a technician copies out of the vendor's client software.
        So the fastest real path into this system is a paste, and it goes through exactly the same
        per-scan reconciliation as a handheld read: no bulk shortcut around the matching rules.
        """
        results: list[ScanResult] = []
        for value in values:
            cleaned = value.strip()
            if not cleaned:
                continue
            results.append(self.scan(case_id, cleaned, actor, container_id=container_id))
        return results

    def amend_quality(
        self, case_id: str, row: int, quality: ReceivedQuality, actor: ActorContext
    ) -> ScanRecord:
        """Record what a tube actually looked like, after it was scanned.

        Separate from the scan because it is a separate observation: the barcode is read at the moment the
        tube comes out of the box, and the thaw or the clot is noticed when someone looks at it. Forcing
        both into one interaction is how a mandatory quality field ends up defaulted to 'acceptable' on
        four hundred specimens.
        """
        batch = self._repo.open_batch(case_id)
        if batch is None:
            raise PolicyDeniedError(
                "no open staging batch; a committed batch can no longer be amended",
                code=ReasonCode.INVALID_STATE_TRANSITION,
            )
        match = next(
            (s for s in self._repo.list_scans(batch.batch_id) if s.matched_row == row),
            None,
        )
        if match is None:
            raise NotFoundError(f"row {row} has not been scanned in this batch")
        updated = match.model_copy(update={"received_quality": quality})
        self._repo.save_scan(updated)
        self._repo.append_audit(
            case_id=case_id,
            event_type=AuditEventType.SPECIMEN_QUALITY_RECORDED,
            actor=actor,
            summary=f"{match.scanned_value} received quality: {quality.value.replace('_', ' ').lower()}",
            reason_codes=(
                () if quality is ReceivedQuality.ACCEPTABLE else (ReasonCode.SPECIMEN_QUALITY_NOT_ACCEPTABLE,)
            ),
            metadata={"batch_id": batch.batch_id, "row": row, "received_quality": quality.value},
        )
        return updated

    def attach_accession(
        self, case_id: str, row: int, encoded_barcode: str, actor: ActorContext
    ) -> ScanRecord:
        """Attach the site's own accession to a row that has already been scanned.

        Specimen labels routinely carry two codes: a linear barcode holding the tube identifier and a 2D
        code holding the site's longer accession. They are not interchangeable, a LIMS deduplicates on the
        accession, and scanning the wrong one is a documented way to file a specimen silently wrong.

        So the bench does not guess which code was just read. The technician says which they are scanning,
        and an accession is attached to a named row rather than matched against the manifest, because the
        manifest does not contain accessions and never did.
        """
        batch = self._repo.open_batch(case_id)
        if batch is None:
            raise PolicyDeniedError(
                "no open staging batch; a committed batch can no longer be amended",
                code=ReasonCode.INVALID_STATE_TRANSITION,
            )
        value = encoded_barcode.strip()
        if not value:
            raise PolicyDeniedError("nothing was scanned", code=ReasonCode.INVALID_STATE_TRANSITION)
        scans = list(self._repo.list_scans(batch.batch_id))
        match = next((s for s in scans if s.matched_row == row), None)
        if match is None:
            raise NotFoundError(f"row {row} has not been scanned in this batch")
        clash = next(
            (s for s in scans if s.encoded_barcode == value and s.matched_row != row),
            None,
        )
        if clash is not None:
            raise PolicyDeniedError(
                f"accession {value} is already on row {clash.matched_row}; two tubes cannot share one",
                code=ReasonCode.LABEL_DUPLICATE,
            )
        updated = match.model_copy(update={"encoded_barcode": value})
        self._repo.save_scan(updated)
        self._repo.append_audit(
            case_id=case_id,
            event_type=AuditEventType.SCAN_RECORDED,
            actor=actor,
            summary=f"row {row} ({match.scanned_value}) carries site accession {value}",
            metadata={"batch_id": batch.batch_id, "row": row, "encoded_barcode": value},
        )
        return updated

    def batch_summary(self, case_id: str) -> dict[str, Any]:
        announcement = self._repo.get_announcement(case_id)
        batch = self._repo.latest_batch(case_id)
        rows = self.expected_rows(case_id)
        scans = list(self._repo.list_scans(batch.batch_id)) if batch else []
        unexpected = [s for s in scans if s.outcome is ScanOutcome.UNEXPECTED]
        duplicates = [s for s in scans if s.outcome is ScanOutcome.DUPLICATE]
        return {
            "batch_id": batch.batch_id if batch else None,
            "committed_at": batch.committed_at.isoformat() if batch and batch.committed_at else None,
            "expected": len(announcement.expected_lines) if announcement else 0,
            "scanned": sum(1 for r in rows if r.scanned_value),
            "matched": sum(1 for r in rows if r.outcome is ScanOutcome.MATCHED),
            "near_matches": sum(1 for r in rows if r.outcome is ScanOutcome.NEAR_MATCH),
            "not_scanned": [r.sample_id for r in rows if not r.scanned_value],
            "unexpected": [s.scanned_value for s in unexpected],
            "duplicates": [s.scanned_value for s in duplicates],
            "rows": [r.model_dump(mode="json") for r in rows],
        }

    def commit_batch(
        self, case_id: str, actor: ActorContext, accept_partial: bool = False
    ) -> tuple[list[Sample], dict[str, Any]]:
        """Commit the staging batch: this is what creates the samples the agent then reconciles.

        Nothing is written to inventory before this point (BSI's staging pattern). A partial receipt is a real,
        separately-modelled state; it must be chosen deliberately, not fallen into.
        """
        case = self._repo.get_case(case_id)
        if case.state is not CaseState.RECEIVED:
            raise PolicyDeniedError(
                f"case {case_id} is {case.state.value}; nothing to commit",
                code=ReasonCode.INVALID_STATE_TRANSITION,
            )
        announcement = self._repo.get_announcement(case_id)
        if announcement is None:
            raise NotFoundError(f"case {case_id} has no announcement")
        batch = self._repo.open_batch(case_id)
        if batch is None:
            raise PolicyDeniedError(
                "no open staging batch; scan at least one specimen first",
                code=ReasonCode.INVALID_STATE_TRANSITION,
            )
        summary = self.batch_summary(case_id)
        if summary["not_scanned"] and not accept_partial:
            raise PolicyDeniedError(
                f"{len(summary['not_scanned'])} declared specimen(s) were not scanned "
                f"({', '.join(summary['not_scanned'][:5])}{'…' if len(summary['not_scanned']) > 5 else ''}); "
                "commit as a partial receipt to proceed",
                code=ReasonCode.PARTIAL_RECEIPT,
            )

        now = self._clock()
        by_row = {line.row: line for line in announcement.expected_lines}
        scans = [
            s
            for s in self._repo.list_scans(batch.batch_id)
            if s.outcome in (ScanOutcome.MATCHED, ScanOutcome.NEAR_MATCH)
        ]
        logger_for_container = {}
        receipt = self._repo.get_receipt(case_id)
        logger_ids = [
            self._repo.get_artifact(a).metadata.get("logger_id")
            for a in (receipt.logger_artifact_ids if receipt else ())
        ]
        containers = sorted({line.container_id for line in announcement.expected_lines})
        for i, container in enumerate(containers):
            if i < len(logger_ids):
                logger_for_container[container] = logger_ids[i]

        # The scans become an artifact at commit. Until now the bench recorded them and the verifier read a
        # scanner export that only the fixture produced, so a real shipment scanned at a real bench had no
        # evidence of having been scanned at all. Committing is the right moment to freeze it: the batch is
        # closed, the content is final, and it gets a hash like every other piece of evidence.
        export = {
            "scanner_id": f"bench:{batch.batch_id}",
            "exported_at": now.isoformat(),
            "scans": [
                {
                    "barcode": s.encoded_barcode or s.scanned_value,
                    "sample_id": s.scanned_value,
                    "container_id": s.container_id,
                    "scanned_at": s.scanned_at.isoformat(),
                    "readable": True,
                }
                for s in scans
            ],
        }
        self._store_artifact(
            case_id,
            ArtifactType.SCANNER_EXPORT,
            f"{batch.batch_id}-scans.json",
            "application/json",
            json.dumps(export, indent=2).encode(),
            source="receiving_bench",
            metadata={"batch_id": batch.batch_id, "scan_count": len(scans)},
        )

        samples: list[Sample] = []
        for scan in scans:
            line = by_row[scan.matched_row] if scan.matched_row is not None else None
            if line is None:
                continue
            sample_id = scan.matched_sample_id or line.sample_id
            samples.append(
                Sample(
                    sample_id=sample_id,
                    case_id=case_id,
                    barcode=scan.encoded_barcode or scan.scanned_value,
                    received_quality=scan.received_quality,
                    specimen_type=line.specimen_type.upper(),
                    container_id=scan.container_id or line.container_id,
                    logger_id=logger_for_container.get(scan.container_id or line.container_id),
                    manifest_row=line.row,
                    participant_reference=line.participant_reference,
                    collection_timestamp=line.collection_timestamp,
                    expected_protocol_id=case.protocol_id,
                    updated_at=now,
                )
            )
        for sample in samples:
            self._repo.save_sample(sample)

        committed = batch.model_copy(
            update={
                "committed_at": now,
                "committed_by_actor_id": actor.actor_id,
                "committed_sample_ids": tuple(s.sample_id for s in samples),
            }
        )
        self._repo.save_batch(committed)
        partial = bool(summary["not_scanned"])
        self._repo.append_audit(
            case_id=case_id,
            event_type=AuditEventType.STAGING_BATCH_COMMITTED,
            actor=actor,
            summary=f"committed {len(samples)} of {summary['expected']} declared specimen(s)"
            + (f"; {len(summary['not_scanned'])} not received" if partial else "")
            + (f"; {len(summary['unexpected'])} unexpected scan(s)" if summary["unexpected"] else ""),
            reason_codes=(ReasonCode.PARTIAL_RECEIPT,) if partial else (),
            sample_ids=tuple(s.sample_id for s in samples),
            metadata={
                "batch_id": batch.batch_id,
                "not_scanned": summary["not_scanned"],
                "unexpected": summary["unexpected"],
            },
        )
        self._repo.save_case(
            self._repo.get_case(case_id).model_copy(
                update={"observed_sample_count": len(samples), "updated_at": now}
            )
        )
        self._transitions.transition_case(
            case_id,
            CaseState.VERIFYING,
            actor,
            ReasonCode.PARTIAL_RECEIPT if partial else ReasonCode.ALL_CHECKS_PASS,
            summary="Staging batch committed; verification may begin",
        )
        return samples, summary


def manifest_csv_from_lines(lines: list[ManifestLine]) -> bytes:
    """Render manifest lines back to the CSV a site would upload (used by the demo and by tests)."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(REQUIRED_MANIFEST_COLUMNS)
    for line in lines:
        w.writerow(
            [
                line.row,
                line.sample_id,
                line.participant_reference,
                line.specimen_type,
                line.container_id,
                line.collection_timestamp.isoformat() if line.collection_timestamp else "",
                line.notes,
            ]
        )
    return out.getvalue().encode()
