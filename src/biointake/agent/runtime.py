"""Runs one invocation event through the real Strands loop and returns a typed RunResult."""

from __future__ import annotations

import json
import uuid
from typing import Any

from strands.session.session_manager import SessionManager

from ..domain.enums import TERMINAL_CASE_STATES, AuditEventType, AuditKind, SampleState
from ..services.intake import IntakeService
from .context import Budgets, TrustedContext
from .events import InvocationEvent, PendingInterrupt, RunResult
from .factory import build_agent
from .summary import build_model_input


def run_event(
    event: InvocationEvent,
    services: IntakeService,
    model: Any,
    session_manager: SessionManager | None,
    *,
    budgets: Budgets | None = None,
    tool_failure_injector: dict[str, int] | None = None,
) -> RunResult:
    repo = services.repo
    ctx = TrustedContext(event=event, services=services, actor=event.actor(), budgets=budgets or Budgets())
    if tool_failure_injector:
        ctx.tool_failure_injector.update(tool_failure_injector)
    ctx.audit_start_sequence = len(repo.list_audit(event.case_id))
    before_states = {s.sample_id: s.state for s in repo.list_samples(event.case_id)}
    before_requests = {r.request_id for r in repo.list_requests(event.case_id)}

    agent = build_agent(
        model,
        session_manager,
        trace_attributes={"biointake.case_id": event.case_id, "biointake.event_id": event.event_id},
    )
    warnings: list[str] = []
    final_text = ""
    interrupts: list[Any] = []
    if event.interrupt_responses:
        prompt: Any = [
            {"interruptResponse": {"interruptId": r["interruptId"], "response": r["response"]}}
            for r in event.interrupt_responses
        ]
    else:
        # The session may already be parked on a human interrupt (a decision is outstanding). Sending a
        # text prompt there is a protocol error in Strands; report the pending decision instead.
        outstanding = [i for i in agent._interrupt_state.interrupts.values() if i.response is None]
        if agent._interrupt_state.activated and outstanding:
            it = outstanding[0]
            case = repo.get_case(event.case_id)
            if case.state not in TERMINAL_CASE_STATES:
                case = services.recompute_case_state(event.case_id, ctx.actor)
            report = services.build_report(event.case_id)
            return RunResult(
                case_id=event.case_id,
                event_id=event.event_id,
                session_id=event.session_id,
                stable_state=case.state,
                is_stable=services.is_stable(event.case_id),
                case_version=case.case_version,
                stop_reason="interrupt",
                pending_interrupt=PendingInterrupt(
                    interrupt_id=it.id,
                    name=it.name,
                    reason=dict(it.reason) if isinstance(it.reason, dict) else {"reason": it.reason},
                ),
                unauthorized_acceptances=int(report["unauthorized_acceptances"]),
                warnings=(
                    "a human decision is already outstanding for this case; respond to the interrupt instead of starting a new run",
                ),
                boot_id=PROCESS_BOOT_ID,
            )
        prompt = build_model_input(ctx)
    try:
        result = agent(
            prompt, invocation_state=ctx.as_invocation_state(), limits={"turns": ctx.budgets.max_turns}
        )
        stop_reason = str(result.stop_reason)
        interrupts = list(result.interrupts or [])
        final_text = str(result).strip() if stop_reason == "end_turn" else ""
    except Exception as e:  # noqa: BLE001
        stop_reason = "error"
        warnings.append(f"agent invocation raised {type(e).__name__}: {e}")
        repo.append_audit(
            case_id=event.case_id,
            event_type=AuditEventType.INVOCATION_FINISHED,
            actor=ctx.actor,
            summary=f"Agent invocation raised {type(e).__name__}: {e}",
            kind=AuditKind.TELEMETRY,
            output_status="error",
        )

    if stop_reason.startswith("limit_"):
        warnings.append(
            f"invocation stopped by budget ({stop_reason}); the case may not have reached a stable state"
        )

    case = repo.get_case(event.case_id)
    if case.state not in TERMINAL_CASE_STATES:
        case = services.recompute_case_state(event.case_id, ctx.actor)
    stable = services.is_stable(event.case_id)
    if not stable and stop_reason != "interrupt":
        warnings.append(f"case ended in non-stable state {case.state.value}")

    after_states = {s.sample_id: s.state for s in repo.list_samples(event.case_id)}
    committed = {
        sid: st.value
        for sid, st in after_states.items()
        if before_states.get(sid) != st
        and st in (SampleState.ACCEPTED, SampleState.ACCEPTED_WITH_EXCEPTION, SampleState.QUARANTINED)
    }
    created_requests = tuple(
        sorted({r.request_id for r in repo.list_requests(event.case_id)} - before_requests)
    )
    new_audit = [a for a in repo.list_audit(event.case_id) if a.sequence > ctx.audit_start_sequence]
    checks_evaluated = sum(
        len(a.metadata.get("check_ids", []))
        for a in new_audit
        if a.event_type is AuditEventType.CHECK_RECORDED
    )
    checks_reverified = sum(
        len(a.metadata.get("check_ids", []))
        for a in new_audit
        if a.event_type is AuditEventType.CHECK_RECORDED and a.tool_name == "reverify"
    )
    logical_effects = sum(1 for a in new_audit if a.kind is AuditKind.DOMAIN_EFFECT)
    pending = None
    if interrupts:
        it = interrupts[0]
        pending = PendingInterrupt(
            interrupt_id=it.id,
            name=it.name,
            reason=dict(it.reason) if isinstance(it.reason, dict) else {"reason": it.reason},
        )
    report = services.build_report(event.case_id)
    return RunResult(
        case_id=event.case_id,
        event_id=event.event_id,
        session_id=event.session_id,
        stable_state=case.state,
        is_stable=stable,
        case_version=case.case_version,
        stop_reason=stop_reason,
        committed_dispositions=committed,
        created_evidence_request_ids=created_requests,
        pending_interrupt=pending,
        checks_evaluated=checks_evaluated,
        checks_reverified=checks_reverified,
        tool_attempt_count=ctx.counters.tool_attempts,
        logical_effect_count=logical_effects,
        retry_count=ctx.counters.retries,
        model_call_count=ctx.counters.model_calls,
        intervention_denials=ctx.counters.intervention_denials,
        unauthorized_acceptances=int(report["unauthorized_acceptances"]),
        warnings=tuple(warnings),
        final_text=final_text[:2000],
        boot_id=PROCESS_BOOT_ID,
    )


PROCESS_BOOT_ID = str(uuid.uuid4())


def run_result_json(result: RunResult) -> str:
    return json.dumps(result.model_dump(mode="json"), indent=2)
