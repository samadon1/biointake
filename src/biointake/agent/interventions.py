"""Fail-closed policy intervention: denies unsafe tool calls before they run. Errors inside → deny."""

from __future__ import annotations

import re
from typing import Any

from strands.hooks import BeforeToolCallEvent
from strands.interventions import Deny, InterventionHandler, Proceed

from ..domain.commands import RESERVED_AUTHORITY_FIELDS
from ..domain.enums import TERMINAL_CASE_STATES, AuditEventType, AuditKind, EvidenceRequestStatus, ReasonCode
from .context import TrustedContext, ctx_from_state
from .tools import MUTATING_TOOLS, VERIFICATION_TOOLS

_KNOWN_TOOLS = set(VERIFICATION_TOOLS) | MUTATING_TOOLS | {"get_case_snapshot"}
_REF = re.compile(r"\b(ART|REQ|PLAN|CASE)-[A-Za-z0-9-]+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _walk(value: Any):  # type: ignore[no-untyped-def]
    if isinstance(value, dict):
        for k, v in value.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _walk(v)


class BioIntakePolicyHandler(InterventionHandler):
    name = "biointake-policy"

    @property
    def on_error(self) -> str:  # type: ignore[override]
        return "deny"  # a broken policy check must block, never allow

    def before_tool_call(self, event: BeforeToolCallEvent, **kwargs: Any) -> Proceed | Deny:
        ctx = ctx_from_state(event.invocation_state)
        if ctx is None:
            return Deny(reason="no trusted invocation context")
        name = str(event.tool_use.get("name"))
        raw = event.tool_use.get("input") or {}
        reason = self._check(ctx, name, raw if isinstance(raw, dict) else {})
        if reason is None:
            return Proceed()
        ctx.counters.intervention_denials += 1
        ctx.services.repo.append_audit(
            case_id=ctx.case_id,
            event_type=AuditEventType.INTERVENTION_DENIED,
            actor=ctx.actor,
            tool_name=name,
            summary=f"Intervention denied {name}: {reason}",
            reason_codes=(ReasonCode.INTERVENTION_DENIED,),
            output_status="denied",
            kind=AuditKind.TOOL_ATTEMPT,
            metadata={"event_id": ctx.event_id},
        )
        return Deny(reason=f"DENIED by biointake-policy: {reason}")

    # ------------------------------------------------------------------------------------------
    def _check(self, ctx: TrustedContext, name: str, inp: dict[str, Any]) -> str | None:  # noqa: C901
        svc = ctx.services
        if name not in _KNOWN_TOOLS:
            return f"unknown or unsupported tool {name}"
        if ctx.counters.tool_attempts >= ctx.budgets.max_tool_calls:
            return f"tool budget of {ctx.budgets.max_tool_calls} calls exhausted"
        for key, _ in _walk(inp):
            if isinstance(key, str) and key in RESERVED_AUTHORITY_FIELDS:
                return f"model-supplied authority field '{key}' is not permitted"
        case = svc.repo.get_case(ctx.case_id)
        if name in MUTATING_TOOLS and case.state in TERMINAL_CASE_STATES:
            return f"case is {case.state.value}; mutating calls are closed"
        # cross-case references anywhere in the input
        for _, value in _walk(inp):
            if isinstance(value, str):
                for m in _REF.finditer(value):
                    ref = m.group(0)
                    if ref.startswith("CASE-") and ref != ctx.case_id:
                        return f"reference to another case ({ref})"
                    if ref.startswith("ART-"):
                        try:
                            if svc.repo.get_artifact(ref).case_id != ctx.case_id:
                                return f"artifact {ref} belongs to another case"
                        except Exception:  # noqa: BLE001
                            return f"unknown artifact reference {ref}"
                    if ref.startswith("REQ-"):
                        try:
                            if svc.repo.get_request(ref).case_id != ctx.case_id:
                                return f"request {ref} belongs to another case"
                        except Exception:  # noqa: BLE001
                            return f"unknown request reference {ref}"
                    if ref.startswith("PLAN-"):
                        plan = svc.repo.get_plan(ref)
                        if plan is None or plan.case_id != ctx.case_id:
                            return f"invalid plan reference {ref}"
        if name == "create_evidence_request":
            recipient = str(inp.get("recipient_contact_id", ""))
            verified = {c.contact_id for c in svc.contacts.search(case.shipment_id)}
            if recipient not in verified:
                return f"recipient '{recipient}' is not a verified contact for shipment {case.shipment_id}"
            if _EMAIL.search(str(inp.get("draft_message", ""))):
                return "draft message must not contain an address"
            keys = inp.get("requirement_keys") or []
            unresolved = {r.key() for r in svc.unresolved_requirements(ctx.case_id)}
            unknown = [k for k in keys if k not in unresolved]
            if unknown:
                return f"requirement keys are not unresolved requirements of this case: {unknown}"
        if name == "commit_dispositions":
            for r in inp.get("requests") or []:
                if isinstance(r, dict) and str(r.get("requested")) not in ("ACCEPT", "QUARANTINE"):
                    return f"disposition {r.get('requested')} is not available to the agent"
        if name == "request_human_disposition" and svc.repo.list_requests(
            ctx.case_id, EvidenceRequestStatus.ACTIVE
        ):
            return "human escalation is not permitted while evidence recovery is active"
        if name == "admit_and_reverify_received_evidence" and ctx.evidence is None:
            return "no evidence has been staged for this event"
        return None
