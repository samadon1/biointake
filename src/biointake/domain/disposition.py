"""Deterministic disposition engine, the only authority that can produce ALLOWED.

Pure function of (policy, check results, requested disposition, optional human decision).
No model output enters here. Unknown, missing, ERROR, UNAVAILABLE and AMBIGUOUS checks fail closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from .enums import (
    CheckCategory,
    CheckStatus,
    Disposition,
    HumanOption,
    PolicyDecision,
    ReasonCode,
)
from .models import CheckResult, HumanDecision, PolicyEvaluation
from .policies import ProtocolPolicy

HARD_CONFLICT_CODES: frozenset[ReasonCode] = frozenset({ReasonCode.BARCODE_COLLISION})
RECOVERABLE_STATUSES: frozenset[CheckStatus] = frozenset({CheckStatus.UNAVAILABLE, CheckStatus.AMBIGUOUS})


class DispositionEngine:
    def __init__(self, policy: ProtocolPolicy) -> None:
        self.policy = policy

    def evaluate(
        self,
        *,
        evaluation_id: str,
        case_id: str,
        sample_id: str,
        checks: Mapping[CheckCategory, CheckResult],
        requested: Disposition,
        human_decision: HumanDecision | None,
        now: datetime,
    ) -> PolicyEvaluation:
        decision, blocking, codes = self._decide(sample_id, checks, requested, human_decision)
        return PolicyEvaluation(
            evaluation_id=evaluation_id,
            policy_id=self.policy.policy_id,
            policy_version=self.policy.version,
            case_id=case_id,
            sample_id=sample_id,
            requested_disposition=requested,
            decision=decision,
            blocking_checks=tuple(blocking),
            reason_codes=tuple(codes),
            human_decision_id=human_decision.decision_id if human_decision else None,
            evaluated_at=now,
        )

    # ------------------------------------------------------------------------------------------
    def _decide(
        self,
        sample_id: str,
        checks: Mapping[CheckCategory, CheckResult],
        requested: Disposition,
        decision: HumanDecision | None,
    ) -> tuple[PolicyDecision, list[CheckCategory], list[ReasonCode]]:
        if requested is Disposition.REJECT:
            return self._decide_reject(sample_id, decision)
        if requested is Disposition.QUARANTINE:
            return self._decide_quarantine(sample_id, checks, decision)

        missing = [
            c for c in self.policy.required_checks if c not in checks or checks[c].sample_id != sample_id
        ]
        if missing:
            return PolicyDecision.DENIED, missing, [ReasonCode.REQUIRED_CHECK_MISSING]

        by_status: dict[CheckStatus, list[CheckCategory]] = {s: [] for s in CheckStatus}
        for cat in self.policy.required_checks:
            status = checks[cat].status
            if status not in by_status:  # unknown status value → fail closed
                return PolicyDecision.DENIED, [cat], [ReasonCode.CHECK_ERROR]
            by_status[status].append(cat)

        errors = by_status[CheckStatus.ERROR]
        fails = by_status[CheckStatus.FAIL]
        recoverable = by_status[CheckStatus.UNAVAILABLE] + by_status[CheckStatus.AMBIGUOUS]
        temp = CheckCategory.TEMPERATURE_REQUIREMENT
        hard_fails = [c for c in fails if c is not temp]
        temp_failed = temp in fails

        def codes_of(cats: list[CheckCategory]) -> list[ReasonCode]:
            return sorted({rc for c in cats for rc in checks[c].reason_codes}, key=str)

        if errors:
            return PolicyDecision.SYSTEM_ERROR, errors, [ReasonCode.CHECK_ERROR, *codes_of(errors)]
        # Results evaluated through a tentative row association are usable only once identity and
        # manifest matching have PASSED (which is what confirms the association).
        anchors = (CheckCategory.IDENTITY_MATCH, CheckCategory.MANIFEST_MATCH)
        if any(checks[c].provisional for c in self.policy.required_checks) and not all(
            checks[a].status is CheckStatus.PASS for a in anchors
        ):
            blocked = [a for a in anchors if checks[a].status is not CheckStatus.PASS] or list(anchors)
            return (
                PolicyDecision.WAITING_FOR_EVIDENCE,
                blocked,
                [ReasonCode.PROVISIONAL_RESULT, *codes_of(blocked)],
            )
        if hard_fails:
            return PolicyDecision.DENIED, hard_fails, codes_of(hard_fails) or [ReasonCode.CHECK_ERROR]
        if recoverable:
            return PolicyDecision.WAITING_FOR_EVIDENCE, recoverable, codes_of(recoverable)

        if requested is Disposition.ACCEPT:
            if temp_failed:
                if self.policy.temperature.exception_allowed:
                    return (
                        PolicyDecision.HUMAN_DECISION_REQUIRED,
                        [temp],
                        [
                            ReasonCode.HUMAN_AUTHORITY_REQUIRED,
                            *codes_of([temp]),
                        ],
                    )
                return PolicyDecision.DENIED, [temp], [ReasonCode.EXCEPTION_NOT_PERMITTED, *codes_of([temp])]
            return PolicyDecision.ALLOWED, [], [ReasonCode.ALL_CHECKS_PASS]

        # ACCEPT_WITH_EXCEPTION
        if not temp_failed:
            return PolicyDecision.DENIED, [], [ReasonCode.EXCEPTION_NOT_PERMITTED]
        if not self.policy.temperature.exception_allowed:
            return PolicyDecision.DENIED, [temp], [ReasonCode.EXCEPTION_NOT_PERMITTED]
        if decision is None or decision.sample_id != sample_id:
            return PolicyDecision.HUMAN_DECISION_REQUIRED, [temp], [ReasonCode.HUMAN_AUTHORITY_REQUIRED]
        if decision.selected_option is not HumanOption.APPROVE_EXCEPTION:
            return PolicyDecision.DENIED, [temp], [ReasonCode.HUMAN_AUTHORITY_REQUIRED]
        if decision.actor_role not in self.policy.temperature.exception_roles:
            return PolicyDecision.DENIED, [temp], [ReasonCode.INSUFFICIENT_ROLE]
        return PolicyDecision.ALLOWED, [], [ReasonCode.HUMAN_DECISION_APPLIED, *codes_of([temp])]

    def _decide_reject(
        self,
        sample_id: str,
        decision: HumanDecision | None,
    ) -> tuple[PolicyDecision, list[CheckCategory], list[ReasonCode]]:
        """Rejection is irreversible; the specimen is not going into the freezer and cannot be un-rejected.

        So unlike every other disposition there is no automatic path to it, not even when every check fails.
        A failing check produces a hold, and a person with the authority the protocol names decides that the
        material is not salvageable. The engine's only job here is to verify that authority."""
        if decision is None or decision.sample_id != sample_id:
            return PolicyDecision.HUMAN_DECISION_REQUIRED, [], [ReasonCode.HUMAN_AUTHORITY_REQUIRED]
        if decision.selected_option is not HumanOption.REJECT:
            return PolicyDecision.DENIED, [], [ReasonCode.HUMAN_AUTHORITY_REQUIRED]
        if decision.actor_role not in self.policy.reject_roles:
            return PolicyDecision.DENIED, [], [ReasonCode.INSUFFICIENT_ROLE]
        return PolicyDecision.ALLOWED, [], [ReasonCode.HUMAN_DECISION_APPLIED, ReasonCode.SPECIMEN_REJECTED]

    def _decide_quarantine(
        self,
        sample_id: str,
        checks: Mapping[CheckCategory, CheckResult],
        decision: HumanDecision | None,
    ) -> tuple[PolicyDecision, list[CheckCategory], list[ReasonCode]]:
        conflicts = [
            c
            for c, r in checks.items()
            if r.sample_id == sample_id
            and r.status is CheckStatus.FAIL
            and HARD_CONFLICT_CODES & set(r.reason_codes)
        ]
        if conflicts:
            codes = sorted(
                {rc for c in conflicts for rc in checks[c].reason_codes if rc in HARD_CONFLICT_CODES}, key=str
            )
            return PolicyDecision.ALLOWED, conflicts, codes
        if decision is not None and decision.sample_id == sample_id:
            if decision.selected_option is not HumanOption.QUARANTINE:
                return PolicyDecision.DENIED, [], [ReasonCode.HUMAN_AUTHORITY_REQUIRED]
            if decision.actor_role not in self.policy.quarantine_roles:
                return PolicyDecision.DENIED, [], [ReasonCode.INSUFFICIENT_ROLE]
            return PolicyDecision.ALLOWED, [], [ReasonCode.HUMAN_DECISION_APPLIED]
        return PolicyDecision.HUMAN_DECISION_REQUIRED, [], [ReasonCode.HUMAN_AUTHORITY_REQUIRED]
