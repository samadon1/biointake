"""Deterministic evidence-dependency service.

Given newly admitted evidence, computes the exact set of CheckResults it invalidates. The model may
*initiate* re-verification; it may not author, extend or trim the invalidation set.
"""

from __future__ import annotations

import hashlib
import json

from ..clock import Clock
from ..domain.enums import ArtifactType, ArtifactValidation, AuditEventType, CheckCategory, ReasonCode
from ..domain.errors import PolicyDeniedError
from ..domain.models import ActorContext, CheckResult, InvalidationPlan
from ..repositories.interfaces import Repository
from .verification import VerificationService, association_key


def plan_digest(plan: InvalidationPlan) -> str:
    body = plan.model_dump(mode="json", exclude={"digest", "applied_operation_id", "produced_check_ids"})
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:32]


class EvidenceDependencyService:
    def __init__(self, repo: Repository, clock: Clock) -> None:
        self._repo = repo
        self._clock = clock

    def compute_invalidation_plan(
        self, case_id: str, newly_admitted_evidence_ids: list[str], actor: ActorContext
    ) -> InvalidationPlan:
        case = self._repo.get_case(case_id)
        artifacts = []
        for aid in newly_admitted_evidence_ids:
            art = self._repo.get_artifact(aid)
            if art.case_id != case_id:
                raise PolicyDeniedError(
                    f"artifact {aid} belongs to another case", code=ReasonCode.INVALIDATION_PLAN_INVALID
                )
            if art.validation_status is not ArtifactValidation.VALID:
                raise PolicyDeniedError(
                    f"artifact {aid} was not admitted", code=ReasonCode.INVALIDATION_PLAN_INVALID
                )
            artifacts.append(art)

        samples = self._repo.list_samples(case_id)
        by_participant: dict[str, list[str]] = {}
        for s in samples:
            if s.participant_reference:
                by_participant.setdefault(s.participant_reference, []).append(s.sample_id)
        current: dict[tuple[str, CheckCategory], CheckResult] = {
            (c.sample_id, c.category): c for c in self._repo.current_checks(case_id)
        }

        invalidated: dict[str, str] = {}
        retained: list[str] = []

        def hit(sid: str, cat: CheckCategory, why: str) -> None:
            chk = current.get((sid, cat))
            if chk is not None and chk.check_id not in invalidated:
                invalidated[chk.check_id] = why

        for art in artifacts:
            if art.artifact_type is ArtifactType.CONSENT_ADDENDUM:
                for participant in art.metadata.get("participants", []):
                    for sid in by_participant.get(str(participant), []):
                        hit(
                            sid,
                            CheckCategory.CONSENT_VALIDITY,
                            f"{art.artifact_id} (consent addendum v{art.metadata.get('version')}) covers participant {participant}",
                        )
            elif art.artifact_type is ArtifactType.CONSENT_RECORDS:
                # The registry the shipment arrived without. Every consent decision taken while it was
                # missing was taken without it, so every one of them is re-decided.
                for participant in art.metadata.get("participants", []):
                    for sid in by_participant.get(str(participant), []):
                        hit(
                            sid,
                            CheckCategory.CONSENT_VALIDITY,
                            f"{art.artifact_id} (consent registry) covers participant {participant}",
                        )
            elif art.artifact_type is ArtifactType.CUSTODY_LOG:
                for sid in art.metadata.get("samples", []):
                    hit(
                        str(sid),
                        CheckCategory.CHAIN_OF_CUSTODY,
                        f"{art.artifact_id} (chain-of-custody log) covers {sid}",
                    )
            elif art.artifact_type is ArtifactType.SENDER_ATTESTATION:
                row = int(art.metadata["manifest_row"])
                sid = str(art.metadata.get("tentative_sample_id", art.metadata["corrected_value"]))
                key = association_key(sid, row)
                if art.metadata.get("refutes"):
                    for chk in current.values():
                        if key in chk.evidence_dependency_ids:
                            hit(
                                chk.sample_id,
                                chk.category,
                                f"{art.artifact_id} refutes the tentative association {key}",
                            )
                else:
                    hit(
                        sid,
                        CheckCategory.IDENTITY_MATCH,
                        f"{art.artifact_id} confirms manifest row {row} → {sid}",
                    )
                    hit(
                        sid,
                        CheckCategory.MANIFEST_MATCH,
                        f"{art.artifact_id} confirms manifest row {row} → {sid}",
                    )
                    for chk in current.values():
                        if (
                            key in chk.evidence_dependency_ids
                            and chk.check_id not in invalidated
                            and chk.provisional
                        ):
                            retained.append(chk.check_id)  # inputs unchanged; association now confirmed

        plan = InvalidationPlan(
            plan_id=self._repo.next_id("PLAN"),
            case_id=case_id,
            evidence_ids=tuple(a.artifact_id for a in artifacts),
            invalidated_check_ids=tuple(sorted(invalidated)),
            reasons_by_check=dict(sorted(invalidated.items())),
            retained_provisional_check_ids=tuple(sorted(retained)),
            created_at=self._clock(),
            case_version=case.case_version,
        )
        plan = plan.model_copy(update={"digest": plan_digest(plan)})
        self._repo.save_plan(plan)
        self._repo.append_audit(
            case_id=case_id,
            event_type=AuditEventType.INVALIDATION_PLAN_CREATED,
            actor=actor,
            tool_name="evidence_dependency",
            summary=f"Plan {plan.plan_id}: {len(plan.invalidated_check_ids)} check(s) invalidated by {', '.join(plan.evidence_ids)}; {len(retained)} provisional result(s) retained",
            sample_ids=tuple(
                dict.fromkeys(self._repo_check_sample(cid) for cid in plan.invalidated_check_ids)
            ),
            metadata={
                "plan_id": plan.plan_id,
                "invalidated": plan.reasons_by_check,
                "retained": list(retained),
                "case_version": case.case_version,
            },
        )
        return plan

    def _repo_check_sample(self, check_id: str) -> str:
        return check_id.split("-", 1)[1].rsplit("-", 2)[0] if check_id.startswith("CHK-") else check_id

    def apply_plan(
        self, plan_id: str, actor: ActorContext, verification: VerificationService, operation_id: str
    ) -> tuple[InvalidationPlan, list[CheckResult]]:
        plan = self._repo.get_plan(plan_id)
        if plan is None:
            raise PolicyDeniedError(
                f"unknown invalidation plan {plan_id}", code=ReasonCode.INVALIDATION_PLAN_INVALID
            )
        if plan.digest != plan_digest(plan):
            raise PolicyDeniedError(
                f"invalidation plan {plan_id} was altered after creation",
                code=ReasonCode.INVALIDATION_PLAN_INVALID,
            )
        case = self._repo.get_case(plan.case_id)
        if plan.applied_operation_id is not None:
            return plan, [
                c for c in self._repo.current_checks(plan.case_id) if c.check_id in plan.produced_check_ids
            ]
        if plan.case_version != case.case_version:
            raise PolicyDeniedError(
                f"invalidation plan {plan_id} was created at case version {plan.case_version}; case is now {case.case_version}",
                code=ReasonCode.INVALIDATION_PLAN_INVALID,
            )
        current = {c.check_id: c for c in self._repo.current_checks(plan.case_id)}
        pairs: list[tuple[str, CheckCategory]] = []
        for cid in plan.invalidated_check_ids:
            chk = current.get(cid)
            if chk is None or chk.case_id != plan.case_id:
                raise PolicyDeniedError(
                    f"check {cid} is not a current check of case {plan.case_id}",
                    code=ReasonCode.INVALIDATION_PLAN_INVALID,
                )
            pairs.append((chk.sample_id, chk.category))
        results = (
            verification.run_pairs(plan.case_id, actor, tuple(pairs), tool_name="reverify") if pairs else []
        )
        applied = plan.model_copy(
            update={
                "applied_operation_id": operation_id,
                "produced_check_ids": tuple(r.check_id for r in results),
            }
        )
        self._repo.save_plan(applied)
        self._repo.append_audit(
            case_id=plan.case_id,
            event_type=AuditEventType.INVALIDATION_PLAN_APPLIED,
            actor=actor,
            tool_name="reverify",
            operation_id=operation_id,
            summary=f"Plan {plan_id} applied: re-ran {len(results)} check(s)",
            sample_ids=tuple(dict.fromkeys(s for s, _ in pairs)),
            metadata={
                "plan_id": plan_id,
                "results": {f"{r.sample_id}:{r.category.value}": r.status.value for r in results},
            },
        )
        return applied, results
