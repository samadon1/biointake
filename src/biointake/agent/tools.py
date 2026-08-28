"""Model-facing tools. Every wrapper derives its operation id from trusted context and returns a
structured envelope. No tool accepts an operation id, actor, case id, address or evaluation id.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field
from strands import tool
from strands.types.tools import ToolContext

from ..domain.commands import (
    ApplyInvalidationPlanCommand,
    CreateEvidenceRequestCommand,
    FinalizeCaseCommand,
    ProposedCorrection,
    RaisePendingDecisionCommand,
    ReceiveEvidenceCommand,
    RecordHumanDecisionCommand,
    RequestDispositionCommand,
    derive_operation_id,
)
from ..domain.enums import CheckCategory, Disposition, EvidenceRequestStatus, HumanOption, ReasonCode
from ..domain.errors import BioIntakeError, UnauthorizedError
from ..domain.models import CommandResult, EvidenceRequirement
from .context import TrustedContext, ctx_from

VERIFICATION_TOOLS: dict[str, tuple[CheckCategory, ...]] = {
    "inspect_manifest_and_labels": (CheckCategory.IDENTITY_MATCH, CheckCategory.MANIFEST_MATCH),
    "evaluate_temperature_logs": (CheckCategory.TEMPERATURE_REQUIREMENT,),
    "verify_consent_and_protocol": (CheckCategory.CONSENT_VALIDITY, CheckCategory.PROTOCOL_ELIGIBILITY),
    "check_chain_of_custody": (CheckCategory.CHAIN_OF_CUSTODY,),
    "reconcile_lims_records": (CheckCategory.LIMS_RECONCILIATION,),
}
MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "commit_dispositions",
        "create_evidence_request",
        "admit_and_reverify_received_evidence",
        "request_human_disposition",
        "finalize_intake",
    }
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_SAMPLE_ID = re.compile(r"\bBX-\d{3}\b")
MAX_DECISION_ATTEMPTS = 4


# ----------------------------------------------------------------------------------------------
def envelope(
    ctx: TrustedContext,
    *,
    success: bool,
    status: str,
    summary: str,
    operation_id: str | None = None,
    reason_codes: tuple[ReasonCode, ...] = (),
    evidence_refs: tuple[str, ...] = (),
    retryable: bool = False,
    error_code: str | None = None,
    audit_event_ids: tuple[str, ...] = (),
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    case = ctx.services.repo.get_case(ctx.case_id)
    body = {
        "success": success,
        "status": status,
        "operation_id": operation_id,
        "case_id": ctx.case_id,
        "case_version": case.case_version,
        "case_state": case.state.value,
        "summary": summary,
        "reason_codes": [c.value for c in reason_codes],
        "evidence_refs": list(evidence_refs),
        "retryable": retryable,
        "error_code": error_code,
        "audit_event_ids": list(audit_event_ids),
        "data": data or {},
    }
    return {"status": "success" if success else "error", "content": [{"json": body}]}


def from_result(ctx: TrustedContext, r: CommandResult, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    ok = r.status in ("ok", "waiting", "human_required", "replayed")
    return envelope(
        ctx,
        success=ok,
        status=r.status,
        summary=r.summary,
        operation_id=r.operation_id,
        reason_codes=r.reason_codes,
        audit_event_ids=r.audit_event_ids,
        error_code=None if ok else (r.reason_codes[0].value if r.reason_codes else r.status.upper()),
        data={**r.data, **(extra or {})},
    )


def from_error(ctx: TrustedContext, e: Exception, operation_id: str | None = None) -> dict[str, Any]:
    if isinstance(e, BioIntakeError):
        transient = e.code is ReasonCode.TOOL_FAILURE_TRANSIENT
        return envelope(
            ctx,
            success=False,
            status="error",
            summary=e.message,
            operation_id=operation_id,
            reason_codes=(e.code,),
            retryable=transient,
            error_code=e.code.value,
        )
    transient = isinstance(e, ConnectionError | TimeoutError)
    return envelope(
        ctx,
        success=False,
        status="error",
        summary=f"{type(e).__name__}: {e}",
        operation_id=operation_id,
        retryable=transient,
        error_code="TOOL_FAILURE_TRANSIENT" if transient else "TOOL_FAILURE",
    )


def _op(ctx: TrustedContext, tool_name: str, payload: dict[str, Any]) -> str:
    return derive_operation_id(ctx.case_id, ctx.event_id, tool_name, payload)


def _maybe_inject_failure(ctx: TrustedContext, tool_name: str) -> None:
    remaining = ctx.tool_failure_injector.get(tool_name, 0)
    if remaining > 0:
        ctx.tool_failure_injector[tool_name] = remaining - 1
        raise ConnectionError(f"injected transient outage in {tool_name}")


def _settle(ctx: TrustedContext) -> None:
    ctx.services.recompute_case_state(ctx.case_id, ctx.actor)


# ----------------------------------------------------------------------------------------------
@tool(context=True)
def get_case_snapshot(tool_context: ToolContext) -> dict[str, Any]:
    """Load the authoritative current case state: samples, check statuses, blockers, unresolved
    requirements, active evidence requests and pending human decisions. Read-only."""
    ctx = ctx_from(tool_context)
    snap = ctx.services.snapshot(ctx.case_id)
    return envelope(
        ctx, success=True, status="ok", summary=f"case {ctx.case_id} is {snap['state']}", data=snap
    )


def _run_verification(
    tool_context: ToolContext, tool_name: str, sample_ids: list[str] | None
) -> dict[str, Any]:
    ctx = ctx_from(tool_context)
    cats = VERIFICATION_TOOLS[tool_name]
    try:
        _maybe_inject_failure(ctx, tool_name)
        results = ctx.services.verify(
            ctx.case_id,
            ctx.actor,
            sample_ids=tuple(sample_ids) if sample_ids else None,
            categories=cats,
            tool_name=tool_name,
        )
    except Exception as e:  # noqa: BLE001
        return from_error(ctx, e)
    transient = [r for r in results if ReasonCode.TOOL_FAILURE_TRANSIENT in r.reason_codes]
    by_sample: dict[str, dict[str, str]] = {}
    for r in results:
        by_sample.setdefault(r.sample_id, {})[r.category.value] = r.status.value
    blockers = [
        {
            "sample_id": r.sample_id,
            "category": r.category.value,
            "status": r.status.value,
            "reason_codes": [c.value for c in r.reason_codes],
            "observed": r.observed_value,
        }
        for r in results
        if r.status.value != "PASS"
    ]
    return envelope(
        ctx,
        success=not transient,
        status="ok" if not transient else "error",
        summary=f"{tool_name}: {len(results)} checks; {len(blockers)} not PASS",
        retryable=bool(transient),
        error_code="TOOL_FAILURE_TRANSIENT" if transient else None,
        reason_codes=(ReasonCode.TOOL_FAILURE_TRANSIENT,) if transient else (),
        evidence_refs=tuple(dict.fromkeys(ref for r in results for ref in r.evidence_refs)),
        data={"results": by_sample, "blockers": blockers},
    )


@tool(context=True)
def inspect_manifest_and_labels(
    tool_context: ToolContext, sample_ids: list[str] | None = None
) -> dict[str, Any]:
    """Reconcile scanned labels against the manifest (identity and manifest-field checks). Exact
    identifier matches PASS; near-matches (e.g. O/0 confusion) are AMBIGUOUS until the sender confirms."""
    return _run_verification(tool_context, "inspect_manifest_and_labels", sample_ids)


@tool(context=True)
def evaluate_temperature_logs(
    tool_context: ToolContext, sample_ids: list[str] | None = None
) -> dict[str, Any]:
    """Deterministically evaluate transport temperature logs against the protocol range and tolerance."""
    return _run_verification(tool_context, "evaluate_temperature_logs", sample_ids)


@tool(context=True)
def verify_consent_and_protocol(
    tool_context: ToolContext, sample_ids: list[str] | None = None
) -> dict[str, Any]:
    """Check consent validity (registry + admitted addenda) and protocol/specimen eligibility."""
    return _run_verification(tool_context, "verify_consent_and_protocol", sample_ids)


@tool(context=True)
def check_chain_of_custody(tool_context: ToolContext, sample_ids: list[str] | None = None) -> dict[str, Any]:
    """Verify required custody handoff events are present, attributed and in order."""
    return _run_verification(tool_context, "check_chain_of_custody", sample_ids)


@tool(context=True)
def reconcile_lims_records(tool_context: ToolContext, sample_ids: list[str] | None = None) -> dict[str, Any]:
    """Compare intake identifiers with the demonstration LIMS; detects barcode collisions. Read-only."""
    return _run_verification(tool_context, "reconcile_lims_records", sample_ids)


# ----------------------------------------------------------------------------------------------
class DispositionRequest(BaseModel):
    sample_id: str
    requested: Literal["ACCEPT", "QUARANTINE"]
    rationale: str = Field(default="", max_length=300)


@tool(context=True)
def commit_dispositions(tool_context: ToolContext, requests: list[DispositionRequest]) -> dict[str, Any]:
    """Ask the deterministic policy engine to apply dispositions. ACCEPT is allowed only when every
    required check PASSES; QUARANTINE without a human decision is allowed only for a hard identity
    conflict. Returns per-sample outcomes and, for blocked samples, the recoverable requirements."""
    ctx = ctx_from(tool_context)
    outcomes: dict[str, Any] = {}
    requirements: dict[str, dict[str, Any]] = {}
    audit: list[str] = []
    for raw in requests:
        req = DispositionRequest.model_validate(raw) if isinstance(raw, dict) else raw
        payload = req.model_dump()
        op = _op(ctx, "commit_dispositions", payload)
        try:
            r = ctx.services.request_disposition(
                RequestDispositionCommand(
                    operation_id=op,
                    case_id=ctx.case_id,
                    actor=ctx.actor,
                    sample_id=req.sample_id,
                    requested=Disposition(req.requested),
                )
            )
        except BioIntakeError as e:
            outcomes[req.sample_id] = {
                "status": "error",
                "summary": e.message,
                "reason_codes": [e.code.value],
                "operation_id": op,
            }
            continue
        outcomes[req.sample_id] = {
            "status": r.status,
            "summary": r.summary,
            "reason_codes": [c.value for c in r.reason_codes],
            "operation_id": op,
            **{k: v for k, v in r.data.items() if k != "recoverable_requirements"},
        }
        audit.extend(r.audit_event_ids)
        for item in r.data.get("recoverable_requirements", []):
            key = f"{item['requirement_type']}:{item['sample_id']}"
            requirements[key] = item
    _settle(ctx)
    n_ok = sum(1 for o in outcomes.values() if o["status"] == "ok")
    return envelope(
        ctx,
        success=True,
        status="ok",
        summary=f"{n_ok}/{len(requests)} dispositions committed; {len(requirements)} unresolved requirement(s)",
        audit_event_ids=tuple(audit),
        data={"outcomes": outcomes, "unresolved_requirements": list(requirements.values())},
    )


@tool(context=True)
def create_evidence_request(
    tool_context: ToolContext,
    recipient_contact_id: str,
    requirement_keys: list[str],
    draft_message: str = Field(default="", max_length=1500),
    grouping_rationale: str = Field(default="", max_length=300),
) -> dict[str, Any]:
    """Send ONE consolidated request for missing evidence to a VERIFIED contact (by contact_id).
    requirement_keys must be unresolved requirements from the snapshot (e.g. "CONSENT_ADDENDUM:BX-209").
    The system owns the destination and the request id; it refuses duplicates and non-missing items."""
    ctx = ctx_from(tool_context)
    unresolved = {r.key(): r for r in ctx.services.unresolved_requirements(ctx.case_id)}
    unknown = [k for k in requirement_keys if k not in unresolved]
    if unknown:
        return envelope(
            ctx,
            success=False,
            status="denied",
            summary=f"unknown or resolved requirement keys: {unknown}",
            reason_codes=(ReasonCode.EVIDENCE_UNMATCHED,),
            error_code="UNKNOWN_REQUIREMENT",
        )
    affected = {unresolved[k].sample_id for k in requirement_keys}
    if _EMAIL.search(draft_message):
        return envelope(
            ctx,
            success=False,
            status="denied",
            summary="draft must not contain addresses",
            reason_codes=(ReasonCode.RECIPIENT_NOT_VERIFIED,),
            error_code="ADDRESS_IN_DRAFT",
        )
    stray = sorted(set(_SAMPLE_ID.findall(draft_message)) - affected)
    if stray:
        return envelope(
            ctx,
            success=False,
            status="denied",
            summary=f"draft mentions unrelated samples: {stray}",
            reason_codes=(ReasonCode.EVIDENCE_UNMATCHED,),
            error_code="UNRELATED_SAMPLES_IN_DRAFT",
        )
    reqs: tuple[EvidenceRequirement, ...] = tuple(unresolved[k] for k in sorted(set(requirement_keys)))
    payload = {
        "recipient_contact_id": recipient_contact_id,
        "requirement_keys": sorted(set(requirement_keys)),
    }
    op = _op(ctx, "create_evidence_request", payload)
    try:
        r = ctx.services.create_evidence_request(
            CreateEvidenceRequestCommand(
                operation_id=op,
                case_id=ctx.case_id,
                actor=ctx.actor,
                recipient_contact_id=recipient_contact_id,
                requirements=reqs,
                note_for_recipient=draft_message,
            )
        )
    except BioIntakeError as e:
        return from_error(ctx, e, op)
    _settle(ctx)
    return from_result(ctx, r, {"grouping_rationale": grouping_rationale})


class ProposedCorrectionInput(BaseModel):
    manifest_row: int
    manifest_value: str
    corrected_value: str
    sender_statement: str = Field(default="", max_length=300)


@tool(context=True)
def admit_and_reverify_received_evidence(
    tool_context: ToolContext, proposed_corrections: list[ProposedCorrectionInput] | None = None
) -> dict[str, Any]:
    """Admit the evidence staged for this event (files + any manifest corrections you extracted from
    the sender's message), then re-run exactly the checks the deterministic dependency service says
    were invalidated. Your corrections are proposals; the system decides whether they are admissible."""
    ctx = ctx_from(tool_context)
    delivery = ctx.evidence
    if delivery is None:
        return envelope(
            ctx,
            success=False,
            status="denied",
            summary="no evidence has been staged for this event",
            reason_codes=(ReasonCode.EVIDENCE_UNMATCHED,),
            error_code="NO_STAGED_EVIDENCE",
        )
    corrections = tuple(
        ProposedCorrection(
            **(ProposedCorrectionInput.model_validate(c) if isinstance(c, dict) else c).model_dump()
        )
        for c in (proposed_corrections or [])
    )
    payload = {
        "request_id": delivery.request_id,
        "files": [a.filename for a in delivery.artifacts],
        "corrections": [c.model_dump() for c in corrections],
    }
    op = _op(ctx, "admit_and_reverify_received_evidence", payload)
    try:
        received = ctx.services.receive_evidence(
            ReceiveEvidenceCommand(
                operation_id=op,
                case_id=ctx.case_id,
                actor=ctx.actor,
                request_id=delivery.request_id,
                upload_token=delivery.upload_token,
                submitted_by_contact_id=delivery.submitted_by_contact_id,
                artifacts=delivery.artifacts,
                proposed_corrections=corrections,
            )
        )
    except BioIntakeError as e:
        return from_error(ctx, e, op)
    reverified: list[dict[str, Any]] = []
    total = ctx.services.total_check_slots(ctx.case_id)
    plan_id = received.data.get("plan_id")
    if plan_id:
        plan_op = _op(ctx, "apply_invalidation_plan", {"plan_id": plan_id})
        try:
            applied = ctx.services.apply_invalidation_plan(
                ApplyInvalidationPlanCommand(
                    operation_id=plan_op, case_id=ctx.case_id, actor=ctx.actor, plan_id=plan_id
                )
            )
            reverified = list(applied.data["reverified"])
        except BioIntakeError as e:
            return from_error(ctx, e, plan_op)
    _settle(ctx)
    return from_result(
        ctx,
        received,
        {
            "plan_id": plan_id,
            "checks_reverified": len(reverified),
            "total_check_slots": total,
            "reverified": reverified,
            "reverified_sample_ids": sorted({r["sample_id"] for r in reverified}),
        },
    )


@tool(context=True)
def request_human_disposition(
    tool_context: ToolContext, sample_id: str, proposed_options: list[str]
) -> dict[str, Any]:
    """Raise a decision card for a sample that needs human authority and WAIT for the decision.
    Allowed only when no evidence request is active. Options must be those the policy permits
    (e.g. ["QUARANTINE", "APPROVE_EXCEPTION"]). Before waiting this tool only reads and upserts the card."""
    ctx = ctx_from(tool_context)
    svc = ctx.services
    if svc.repo.list_requests(ctx.case_id, EvidenceRequestStatus.ACTIVE):
        return envelope(
            ctx,
            success=False,
            status="denied",
            summary="evidence recovery in progress; human decision deferred",
            reason_codes=(ReasonCode.EVIDENCE_RECOVERY_IN_PROGRESS,),
            error_code="EVIDENCE_RECOVERY_IN_PROGRESS",
        )
    raise_op = _op(ctx, "raise_pending_decision", {"sample_id": sample_id})
    try:
        raised = svc.raise_pending_decision(
            RaisePendingDecisionCommand(
                operation_id=raise_op, case_id=ctx.case_id, actor=ctx.actor, sample_id=sample_id
            )
        )
    except BioIntakeError as e:
        return from_error(ctx, e, raise_op)
    card = raised.data["card"]
    permitted = [o["option"] for o in card["options"]]
    if sorted(proposed_options) != sorted(permitted):
        return envelope(
            ctx,
            success=False,
            status="denied",
            summary=f"options must match policy: {permitted}",
            reason_codes=(ReasonCode.HUMAN_AUTHORITY_REQUIRED,),
            error_code="OPTIONS_MISMATCH",
            data={"permitted_options": permitted},
        )
    reason = {
        "issue_id": card["issue_id"],
        "sample_id": card["sample_id"],
        "issue_type": card["issue_type"],
        "observed_value": card["observed_value"],
        "expected_value": card["expected_value"],
        "policy_clause": card["policy_clause"],
        "evidence_refs": card["evidence_refs"],
        "passed_checks": card["passed_checks"],
        "blocked_checks": card["blocked_checks"],
        "options": card["options"],
    }
    # ---- everything above is reads + one idempotent upsert; the interrupt re-runs it on resume ----
    # Each attempt uses its own interrupt name (hence its own interrupt id), so a response the policy
    # engine refuses does NOT consume the decision: a fresh interrupt is raised and the human can
    # choose again. Without this the case would be stuck in NEEDS_HUMAN_DECISION with no live interrupt.
    refusal: dict[str, Any] | None = None
    for attempt in range(MAX_DECISION_ATTEMPTS):
        name = "human_disposition" if attempt == 0 else f"human_disposition_retry_{attempt}"
        response = tool_context.interrupt(
            name, reason={**reason, "attempt": attempt, "previous_refusal": refusal}
        )
        # ---- resumed with the human's answer; the ACTOR comes from trusted context, never the payload ----
        if not isinstance(response, dict) or "selected_option" not in response:
            refusal = {"reason": "malformed response", "detail": "expected {'selected_option': ...}"}
            continue
        try:
            option = HumanOption(str(response["selected_option"]))
        except ValueError:
            refusal = {
                "reason": "unknown option",
                "detail": str(response.get("selected_option"))[:60],
                "permitted_options": permitted,
            }
            continue
        if option.value not in permitted:
            refusal = {"reason": "option not offered", "detail": option.value, "permitted_options": permitted}
            continue
        decide_op = _op(
            ctx, "record_human_decision", {"issue_id": card["issue_id"], "selected_option": option.value}
        )
        try:
            decided = svc.record_human_decision(
                RecordHumanDecisionCommand(
                    operation_id=decide_op,
                    case_id=ctx.case_id,
                    actor=ctx.actor,
                    issue_id=card["issue_id"],
                    selected_option=option,
                    comment=str(response.get("comment", ""))[:500],
                    client_payload={
                        k: v for k, v in response.items() if k not in ("selected_option", "comment")
                    },
                )
            )
        except UnauthorizedError as e:
            refusal = {
                "reason": "insufficient role",
                "detail": e.message,
                "attempted_option": option.value,
                "actor_role": ctx.actor.role.value,
            }
            continue
        except BioIntakeError as e:
            return from_error(ctx, e, decide_op)
        _settle(ctx)
        return from_result(ctx, decided, {"issue_id": card["issue_id"], "attempts": attempt + 1})
    return envelope(
        ctx,
        success=False,
        status="denied",
        summary=f"{MAX_DECISION_ATTEMPTS} decision attempts were refused; the decision card remains open for an authorised user",
        reason_codes=(ReasonCode.HUMAN_AUTHORITY_REQUIRED,),
        error_code="DECISION_ATTEMPTS_EXHAUSTED",
        data={"issue_id": card["issue_id"], "last_refusal": refusal, "permitted_options": permitted},
    )


@tool(context=True)
def finalize_intake(tool_context: ToolContext) -> dict[str, Any]:
    """Close the case and produce the audit report. Allowed only when every sample is terminal, no
    evidence request is active and no human decision is pending."""
    ctx = ctx_from(tool_context)
    op = _op(ctx, "finalize_intake", {})
    try:
        r = ctx.services.finalize(FinalizeCaseCommand(operation_id=op, case_id=ctx.case_id, actor=ctx.actor))
    except BioIntakeError as e:
        return from_error(ctx, e, op)
    return from_result(ctx, r)


ALL_TOOLS = [
    get_case_snapshot,
    inspect_manifest_and_labels,
    evaluate_temperature_logs,
    verify_consent_and_protocol,
    check_chain_of_custody,
    reconcile_lims_records,
    commit_dispositions,
    create_evidence_request,
    admit_and_reverify_received_evidence,
    request_human_disposition,
    finalize_intake,
]
