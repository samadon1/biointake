"""Runs the seven deterministic checks per sample and records CheckResults with dependency metadata.

Everything here is code. The future agent only *initiates* verification; which checks run after new
evidence is decided by the EvidenceDependencyService, never by the model.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ..clock import Clock
from ..domain.enums import (
    ArtifactType,
    ArtifactValidation,
    AuditEventType,
    CheckCategory,
    CheckStatus,
    ReasonCode,
)
from ..domain.models import ActorContext, CheckResult, EvidenceArtifact, Sample, ShipmentCase
from ..domain.policies import ProtocolPolicy
from ..repositories.interfaces import Repository
from ..storage.interfaces import ArtifactStorage
from . import consent as consent_svc
from . import custody as custody_svc
from . import manifest as manifest_svc
from . import temperature as temperature_svc
from .lims_demo import DemoLims


def association_key(sample_id: str, manifest_row: int) -> str:
    """Dependency id for a tentative (near-match) row association."""
    return f"assoc:{sample_id}:row{manifest_row}"


@dataclass
class Evaluated:
    status: CheckStatus
    codes: tuple[ReasonCode, ...]
    observed: str | None
    expected: str | None
    refs: tuple[str, ...]
    summary: str
    deps: tuple[str, ...] = ()
    versions: dict[str, str] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    provisional: bool = False


class MissingEvidenceError(RuntimeError):
    """Evidence a check needs was never supplied.

    Raised only for the two artifacts without which nothing at all can be evaluated. The per-check ones do
    not raise: their own check reports UNAVAILABLE and the rest of the run proceeds, because a lab missing
    one document should still learn the state of everything else.
    """

    def __init__(self, artifact_type: ArtifactType) -> None:
        self.artifact_type = artifact_type
        super().__init__(f"no {artifact_type.value} has been supplied for this shipment")


# Which check cannot be answered without which document.
EVIDENCE_FOR: dict[CheckCategory, ArtifactType] = {
    CheckCategory.PROTOCOL_ELIGIBILITY: ArtifactType.PROTOCOL,
    CheckCategory.CONSENT_VALIDITY: ArtifactType.CONSENT_RECORDS,
    CheckCategory.CHAIN_OF_CUSTODY: ArtifactType.CUSTODY_LOG,
}


@dataclass
class CaseContext:
    case: ShipmentCase
    policy: ProtocolPolicy
    manifest_rows: list[manifest_svc.ManifestRow]
    manifest_problems: list[str]
    manifest_artifact: EvidenceArtifact
    scanner_artifact: EvidenceArtifact
    links: dict[str, manifest_svc.LinkResult]
    logger_summaries: dict[str, temperature_svc.TemperatureSummary]
    logger_artifacts: dict[str, EvidenceArtifact]
    consent_records: list[consent_svc.ConsentRecord]
    consent_artifact: EvidenceArtifact | None
    addenda: list[tuple[EvidenceArtifact, consent_svc.ConsentAddendum]]
    attestations: dict[int, EvidenceArtifact]  # manifest_row → admitted CONFIRMING attestation
    refuted_rows: dict[int, EvidenceArtifact]  # manifest_row → admitted REFUTING attestation
    custody_events: list[custody_svc.CustodyEvent]
    custody_artifact: EvidenceArtifact | None
    protocol_artifact: EvidenceArtifact | None
    problems: list[str] = field(default_factory=list)

    @property
    def rows_by_number(self) -> dict[int, manifest_svc.ManifestRow]:
        return manifest_svc.manifest_row_lookup(self.manifest_rows)

    def effective_link(self, sample: Sample) -> manifest_svc.LinkResult | None:
        """The label→row link after applying admitted attestations (a refuted row is no row)."""
        link = self.links.get(sample.sample_id)
        if link is None:
            return None
        if link.manifest_row is not None and link.manifest_row in self.refuted_rows:
            return link.model_copy(
                update={
                    "manifest_row": None,
                    "exact": False,
                    "reason_codes": (ReasonCode.ASSOCIATION_REFUTED,),
                }
            )
        return link

    def effective_row(self, sample: Sample) -> manifest_svc.ManifestRow | None:
        link = self.effective_link(sample)
        if link is None or link.manifest_row is None:
            return None
        return self.rows_by_number.get(link.manifest_row)

    def association_confirmed(self, link: manifest_svc.LinkResult) -> bool:
        if link.exact:
            return True
        if link.manifest_row is None:
            return False
        att = self.attestations.get(link.manifest_row)
        return att is not None and att.metadata.get("corrected_value") == link.sample_id


class VerificationService:
    def __init__(
        self, repo: Repository, storage: ArtifactStorage, policy: ProtocolPolicy, lims: DemoLims, clock: Clock
    ) -> None:
        self._repo = repo
        self._storage = storage
        self._policy = policy
        self._lims = lims
        self._clock = clock
        self.fault_injector: dict[CheckCategory, int] = {}  # test hook: remaining crashes per category

    # ------------------------------------------------------------------------------------------
    def policy_for(self, case: ShipmentCase) -> ProtocolPolicy:
        """The rules this case is judged by: its study's, not the process's.

        A service instance serves every case, so holding one policy on it was only ever correct while there
        was one study. The moment a lab authors a second, a shipment announced against it would be judged
        against somebody else's protocol, which is precisely the failure this product exists to prevent.
        """
        if case.study_id:
            study = self._repo.get_study(case.study_id)
            if study is not None:
                return study.policy
        return self._policy

    def build_context(self, case_id: str) -> CaseContext:
        case = self._repo.get_case(case_id)
        policy = self.policy_for(case)

        def first(t: ArtifactType) -> EvidenceArtifact | None:
            return next((a for a in self._repo.list_artifacts(case_id, t)), None)

        def required(t: ArtifactType) -> EvidenceArtifact:
            art = first(t)
            if art is None:
                raise MissingEvidenceError(t)
            return art

        # Required to evaluate anything: the manifest is what was declared, the scans are what arrived.
        manifest_art = required(ArtifactType.MANIFEST)
        scanner_art = required(ArtifactType.SCANNER_EXPORT)
        # Per-check. Their absence makes their own check UNAVAILABLE rather than killing the whole run,
        # because a lab missing a custody log should still learn that eleven of its twelve specimens are fine.
        consent_art = first(ArtifactType.CONSENT_RECORDS)
        custody_art = first(ArtifactType.CUSTODY_LOG)
        protocol_art = first(ArtifactType.PROTOCOL)

        parsed = manifest_svc.parse_manifest(self._storage.get(manifest_art.storage_uri))
        rows, problems = list(parsed.rows), list(parsed.problems)
        export = manifest_svc.parse_scanner_export(self._storage.get(scanner_art.storage_uri))
        links = {lr.sample_id: lr for lr in manifest_svc.link_labels_to_manifest(rows, export)}

        logger_summaries: dict[str, temperature_svc.TemperatureSummary] = {}
        logger_artifacts: dict[str, EvidenceArtifact] = {}
        for art in self._repo.list_artifacts(case_id, ArtifactType.TEMPERATURE_LOG):
            logger_id = str(art.metadata.get("logger_id", art.original_filename))
            logger_artifacts[logger_id] = art
            logger_summaries[logger_id] = temperature_svc.evaluate_logger(
                logger_id, self._storage.get(art.storage_uri), policy.temperature
            )

        addenda: list[tuple[EvidenceArtifact, consent_svc.ConsentAddendum]] = []
        for art in self._repo.list_artifacts(case_id, ArtifactType.CONSENT_ADDENDUM):
            if art.validation_status is ArtifactValidation.VALID:
                addenda.append((art, consent_svc.parse_consent_addendum(self._storage.get(art.storage_uri))))
        attestations: dict[int, EvidenceArtifact] = {}
        refuted: dict[int, EvidenceArtifact] = {}
        for art in self._repo.list_artifacts(case_id, ArtifactType.SENDER_ATTESTATION):
            if art.validation_status is ArtifactValidation.VALID:
                row = int(art.metadata["manifest_row"])
                if art.metadata.get("refutes"):
                    refuted[row] = art
                else:
                    attestations[row] = art

        return CaseContext(
            case=case,
            policy=policy,
            manifest_rows=rows,
            manifest_problems=problems,
            manifest_artifact=manifest_art,
            scanner_artifact=scanner_art,
            links=links,
            logger_summaries=logger_summaries,
            logger_artifacts=logger_artifacts,
            consent_records=(
                consent_svc.parse_consent_records(self._storage.get(consent_art.storage_uri))
                if consent_art
                else []
            ),
            consent_artifact=consent_art,
            addenda=addenda,
            attestations=attestations,
            refuted_rows=refuted,
            custody_events=(
                custody_svc.parse_custody_log(self._storage.get(custody_art.storage_uri))
                if custody_art
                else []
            ),
            custody_artifact=custody_art,
            protocol_artifact=protocol_art,
        )

    # ------------------------------------------------------------------------------------------
    def run(
        self,
        case_id: str,
        actor: ActorContext,
        *,
        sample_ids: tuple[str, ...] | None = None,
        categories: tuple[CheckCategory, ...] | None = None,
        tool_name: str = "verification",
    ) -> list[CheckResult]:
        samples = [
            s.sample_id
            for s in self._repo.list_samples(case_id)
            if sample_ids is None or s.sample_id in sample_ids
        ]
        cats = categories or tuple(self._policy.required_checks)
        return self.run_pairs(
            case_id, actor, tuple((s, c) for s in samples for c in cats), tool_name=tool_name
        )

    def run_pairs(
        self,
        case_id: str,
        actor: ActorContext,
        pairs: tuple[tuple[str, CheckCategory], ...],
        *,
        tool_name: str = "verification",
    ) -> list[CheckResult]:
        """Evaluate exactly the given (sample, category) pairs and record them."""
        ctx = self.build_context(case_id)
        samples = {s.sample_id: s for s in self._repo.list_samples(case_id)}
        ids = iter(self._repo.next_ids("CHK", len(pairs)))
        results: list[CheckResult] = []
        for sid, cat in pairs:
            results.append(self._evaluate_one(ctx, samples[sid], cat, next(ids)))
        self._repo.save_checks(results)
        counts: dict[str, int] = {}
        for r in results:
            counts[r.status.value] = counts.get(r.status.value, 0) + 1
        self._repo.append_audit(
            case_id=case_id,
            event_type=AuditEventType.CHECK_RECORDED,
            actor=actor,
            tool_name=tool_name,
            summary=f"Ran {len(results)} checks on {len({s for s, _ in pairs})} samples: "
            + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
            sample_ids=tuple(dict.fromkeys(s for s, _ in pairs)),
            metadata={
                "results": {f"{r.sample_id}:{r.category.value}": r.status.value for r in results},
                "check_ids": [r.check_id for r in results],
            },
        )
        return results

    # ------------------------------------------------------------------------------------------
    @staticmethod
    def _missing_evidence_for(ctx: CaseContext, cat: CheckCategory) -> ArtifactType | None:
        """The document this check needs, if it was never supplied."""
        needed = EVIDENCE_FOR.get(cat)
        if needed is None:
            return None
        held = {
            ArtifactType.PROTOCOL: ctx.protocol_artifact,
            ArtifactType.CONSENT_RECORDS: ctx.consent_artifact,
            ArtifactType.CUSTODY_LOG: ctx.custody_artifact,
        }[needed]
        return None if held is not None else needed

    @staticmethod
    def _unavailable_evidence(artifact_type: ArtifactType) -> Evaluated:
        """Say what is missing, by name.

        UNAVAILABLE is already fail-closed in the disposition engine, so nothing can be accepted on the
        strength of a document nobody supplied. What this adds is the reason: a case that stalls should
        tell the coordinator which document to go and find, not merely refuse to finish.
        """
        readable = artifact_type.value.replace("_", " ").lower()
        return Evaluated(
            status=CheckStatus.UNAVAILABLE,
            codes=(ReasonCode.REQUIRED_EVIDENCE_NOT_SUPPLIED,),
            observed=f"no {readable} on file for this shipment",
            expected=f"a {readable}",
            refs=(),
            summary=f"Cannot evaluate: no {readable} has been supplied.",
        )

    def _evaluate_one(
        self, ctx: CaseContext, sample: Sample, cat: CheckCategory, check_id: str
    ) -> CheckResult:
        try:
            if self.fault_injector.get(cat, 0) > 0:
                self.fault_injector[cat] -= 1
                raise ConnectionError(f"injected transient failure for {cat.value}")
            missing = self._missing_evidence_for(ctx, cat)
            ev = self._unavailable_evidence(missing) if missing else self._dispatch(ctx, sample, cat)
        except Exception as e:  # evaluator crash → ERROR, never PASS
            ev = Evaluated(
                status=CheckStatus.ERROR,
                codes=(ReasonCode.CHECK_ERROR, ReasonCode.TOOL_FAILURE_TRANSIENT)
                if isinstance(e, ConnectionError | TimeoutError)
                else (ReasonCode.CHECK_ERROR,),
                observed=f"{type(e).__name__}: {e}",
                expected=None,
                refs=(),
                summary="Evaluator raised an exception.",
            )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "category": cat.value,
                    "sample": sample.sample_id,
                    "deps": sorted(ev.deps),
                    "versions": dict(sorted(ev.versions.items())),
                    "inputs": ev.inputs,
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()[:24]
        return CheckResult(
            check_id=f"CHK-{sample.sample_id}-{cat.value}-{check_id.split('-')[-1]}",
            case_id=sample.case_id,
            sample_id=sample.sample_id,
            category=cat,
            status=ev.status,
            reason_codes=ev.codes,
            observed_value=ev.observed,
            expected_value=ev.expected,
            evidence_refs=ev.refs,
            rule_version=f"{ctx.policy.policy_id}@{ctx.policy.version}",
            evaluator=f"biointake.services.verification:{cat.value}",
            evaluated_at=self._clock(),
            summary=ev.summary,
            evidence_dependency_ids=tuple(sorted(ev.deps)),
            source_record_versions=dict(sorted(ev.versions.items())),
            input_fingerprint=fingerprint,
            policy_version=ctx.policy.version,
            provisional=ev.provisional,
        )

    def _dispatch(self, ctx: CaseContext, sample: Sample, cat: CheckCategory) -> Evaluated:  # noqa: C901
        m, s = ctx.manifest_artifact, ctx.scanner_artifact
        base_refs = (m.artifact_id, s.artifact_id)
        base_versions = {m.artifact_id: m.sha256, s.artifact_id: s.sha256}
        link = ctx.effective_link(sample)
        row = ctx.effective_row(sample)
        tentative = (
            link is not None
            and link.manifest_row is not None
            and not link.exact
            and not ctx.association_confirmed(link)
        )
        assoc_deps: tuple[str, ...] = (
            (association_key(sample.sample_id, link.manifest_row),)
            if (link and link.manifest_row is not None and not link.exact)
            else ()
        )
        row_inputs = row.model_dump(mode="json") if row else None

        if cat is CheckCategory.IDENTITY_MATCH:
            if link is None:
                return Evaluated(
                    CheckStatus.UNAVAILABLE,
                    (ReasonCode.LABEL_UNREADABLE,),
                    "no scan",
                    sample.sample_id,
                    base_refs,
                    "No label scan for this sample.",
                    base_refs,
                    base_versions,
                )
            if link.duplicate:
                return Evaluated(
                    CheckStatus.FAIL,
                    (ReasonCode.LABEL_DUPLICATE,),
                    f"barcode {link.barcode} scanned more than once",
                    "unique barcode",
                    base_refs,
                    "Duplicate label in shipment.",
                    base_refs,
                    base_versions,
                )
            if ReasonCode.LABEL_UNREADABLE in link.reason_codes:
                return Evaluated(
                    CheckStatus.FAIL,
                    (ReasonCode.LABEL_UNREADABLE,),
                    "label unreadable",
                    sample.sample_id,
                    base_refs,
                    "Label could not be decoded.",
                    base_refs,
                    base_versions,
                )
            if link.exact:
                return Evaluated(
                    CheckStatus.PASS,
                    (),
                    f"label {sample.sample_id} = manifest row {link.manifest_row}",
                    sample.sample_id,
                    base_refs,
                    "Label matches manifest exactly.",
                    base_refs,
                    base_versions,
                    {"row": row_inputs},
                )
            if link.manifest_row is None:
                code = (
                    ReasonCode.ASSOCIATION_REFUTED
                    if ReasonCode.ASSOCIATION_REFUTED in link.reason_codes
                    else ReasonCode.MANIFEST_ROW_MISSING
                )
                deps = base_refs + tuple(a.artifact_id for a in ctx.refuted_rows.values())
                return Evaluated(
                    CheckStatus.UNAVAILABLE,
                    (code,),
                    f"label {sample.sample_id} has no manifest row",
                    "manifest row",
                    base_refs,
                    "No manifest row for this label."
                    if code is ReasonCode.MANIFEST_ROW_MISSING
                    else "Sender refuted the tentative row association.",
                    deps,
                    base_versions,
                )
            att = ctx.attestations.get(link.manifest_row)
            if att is not None and att.metadata.get("corrected_value") == sample.sample_id:
                deps = base_refs + assoc_deps + (att.artifact_id,)
                return Evaluated(
                    CheckStatus.PASS,
                    (),
                    f"manifest row {link.manifest_row} '{link.near_match_value}' confirmed as {sample.sample_id}",
                    sample.sample_id,
                    base_refs + (att.artifact_id,),
                    "Near-match confirmed by authenticated sender attestation.",
                    deps,
                    {**base_versions, att.artifact_id: att.sha256},
                    {"row": row_inputs},
                )
            return Evaluated(
                CheckStatus.AMBIGUOUS,
                (ReasonCode.MANIFEST_IDENTIFIER_NEAR_MATCH,),
                f"manifest row {link.manifest_row} reads '{link.near_match_value}'; label reads '{sample.sample_id}'",
                sample.sample_id,
                base_refs,
                "Identifier near-match requires sender confirmation.",
                base_refs + assoc_deps,
                base_versions,
                {"row": row_inputs},
            )

        if cat is CheckCategory.MANIFEST_MATCH:
            if link is None or link.manifest_row is None or row is None:
                return Evaluated(
                    CheckStatus.UNAVAILABLE,
                    (ReasonCode.MANIFEST_ROW_MISSING,),
                    "no manifest row",
                    "manifest row",
                    base_refs,
                    "No manifest row to compare against.",
                    base_refs,
                    base_versions,
                )
            att = ctx.attestations.get(link.manifest_row)
            if not ctx.association_confirmed(link):
                return Evaluated(
                    CheckStatus.AMBIGUOUS,
                    (ReasonCode.MANIFEST_IDENTIFIER_NEAR_MATCH,),
                    f"row {row.row} identifier unconfirmed",
                    sample.sample_id,
                    base_refs,
                    "Manifest row identity unconfirmed.",
                    base_refs + assoc_deps,
                    base_versions,
                    {"row": row_inputs},
                )
            deps = base_refs + assoc_deps + ((att.artifact_id,) if att else ())
            versions = {**base_versions, **({att.artifact_id: att.sha256} if att else {})}
            mismatches = []
            if row.container_id != sample.container_id:
                mismatches.append(f"container {row.container_id} ≠ scanned {sample.container_id}")
            if row.specimen_type.upper() != sample.specimen_type.upper():
                mismatches.append(f"specimen {row.specimen_type} ≠ {sample.specimen_type}")
            refs = base_refs + ((att.artifact_id,) if att else ())
            if mismatches:
                return Evaluated(
                    CheckStatus.FAIL,
                    (ReasonCode.MANIFEST_FIELD_MISMATCH,),
                    "; ".join(mismatches),
                    "manifest fields = observed",
                    refs,
                    "Manifest fields contradict the scanned label.",
                    deps,
                    versions,
                    {"row": row_inputs},
                )
            return Evaluated(
                CheckStatus.PASS,
                (),
                f"row {row.row}: {row.specimen_type}, {row.container_id}",
                "manifest fields = observed",
                refs,
                "Manifest fields agree with the scanned label.",
                deps,
                versions,
                {"row": row_inputs},
            )

        if cat is CheckCategory.PROTOCOL_ELIGIBILITY:
            if ctx.protocol_artifact is None:
                return self._unavailable_evidence(ArtifactType.PROTOCOL)
            protocol_art = ctx.protocol_artifact
            specimen = row.specimen_type if row else sample.specimen_type
            o = consent_svc.evaluate_protocol_eligibility(
                sample.expected_protocol_id, specimen, ctx.policy, protocol_art.artifact_id
            )
            return Evaluated(
                o.status,
                o.reason_codes,
                o.observed,
                o.expected,
                o.evidence_refs,
                o.summary,
                (protocol_art.artifact_id,) + assoc_deps,
                {protocol_art.artifact_id: protocol_art.sha256},
                {"specimen": specimen},
                provisional=tentative,
            )

        if cat is CheckCategory.CONSENT_VALIDITY:
            if ctx.consent_artifact is None:
                return self._unavailable_evidence(ArtifactType.CONSENT_RECORDS)
            consent_art = ctx.consent_artifact
            participant = row.participant_reference if row else None
            o = consent_svc.evaluate_consent(
                participant,
                ctx.case.protocol_id,
                ctx.consent_records,
                [(a.artifact_id, d) for a, d in ctx.addenda],
                ctx.policy.consent,
                consent_art.artifact_id,
            )
            used_addenda = [a for a, _ in ctx.addenda if a.artifact_id in o.evidence_refs]
            deps = (
                (consent_art.artifact_id, f"participant:{participant}")
                + tuple(a.artifact_id for a in used_addenda)
                + assoc_deps
            )
            versions = {
                consent_art.artifact_id: consent_art.sha256,
                **{a.artifact_id: a.sha256 for a in used_addenda},
            }
            return Evaluated(
                o.status,
                o.reason_codes,
                o.observed,
                o.expected,
                o.evidence_refs,
                o.summary,
                deps,
                versions,
                {"participant": participant},
                provisional=tentative,
            )

        if cat is CheckCategory.TEMPERATURE_REQUIREMENT:
            rule = ctx.policy.temperature
            expected = f"{rule.min_c:g}–{rule.max_c:g} °C, ≤{rule.tolerance_minutes:g} min out of range"
            if sample.logger_id is None or sample.logger_id not in ctx.logger_summaries:
                return Evaluated(
                    CheckStatus.UNAVAILABLE,
                    (ReasonCode.TEMPERATURE_LOG_MISSING,),
                    "no logger assigned",
                    expected,
                    (),
                    "No temperature logger covers this sample.",
                    (f"logger:{sample.logger_id}",),
                    {},
                )
            summ = ctx.logger_summaries[sample.logger_id]
            art = ctx.logger_artifacts[sample.logger_id]
            observed = (
                f"{summ.min_c:.1f}–{summ.max_c:.1f} °C; {summ.minutes_out_of_range:.0f} min out of range"
                if summ.max_c is not None
                else "no readings"
            )
            return Evaluated(
                summ.status,
                summ.reason_codes,
                observed,
                expected,
                (art.artifact_id,),
                summ.summary,
                (art.artifact_id,),
                {art.artifact_id: art.sha256},
                {"logger": sample.logger_id},
            )

        if cat is CheckCategory.CHAIN_OF_CUSTODY:
            if ctx.custody_artifact is None:
                return self._unavailable_evidence(ArtifactType.CUSTODY_LOG)
            oc = custody_svc.evaluate_custody(sample.sample_id, ctx.custody_events, ctx.policy.custody)
            art = ctx.custody_artifact
            return Evaluated(
                oc.status,
                oc.reason_codes,
                oc.observed,
                oc.expected,
                (art.artifact_id,),
                oc.summary,
                (art.artifact_id,),
                {art.artifact_id: art.sha256},
            )

        if cat is CheckCategory.LIMS_RECONCILIATION:
            r = self._lims.reconcile(sample, ctx.case.protocol_id)
            deps = tuple(f"lims:{rid}" for rid in r.record_ids)
            versions = {}
            for rid in r.record_ids:
                rec = self._lims.get(rid)
                versions[f"lims:{rid}"] = f"{rec.status}:{len(rec.history)}" if rec else "missing"
            return Evaluated(
                r.status,
                r.reason_codes,
                r.observed,
                r.expected,
                deps,
                r.summary,
                deps,
                versions,
                {"barcode": sample.barcode},
            )

        raise ValueError(f"unknown check category {cat}")
