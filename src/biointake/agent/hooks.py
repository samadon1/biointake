"""Audit + retry hook provider. Tool attempts are recorded as TOOL_ATTEMPT (may repeat); domain
effects are recorded by the services themselves. Automatic retry is limited to verification tools
that report a transient failure, at most `max_retries_per_tool_call` times per tool use."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from strands.hooks import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from ..domain.enums import AuditEventType, AuditKind, ReasonCode
from .context import TrustedContext, ctx_from_state
from .tools import VERIFICATION_TOOLS

RETRYABLE_TOOLS = frozenset(VERIFICATION_TOOLS)


def _digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _body(result: Any) -> dict[str, Any]:
    try:
        for block in result.get("content", []):
            if "json" in block:
                return dict(block["json"])
    except Exception:  # noqa: BLE001
        pass
    return {}


class AuditHookProvider(HookProvider):
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self.on_before_invocation)
        registry.add_callback(AfterInvocationEvent, self.on_after_invocation)
        registry.add_callback(BeforeModelCallEvent, self.on_before_model)
        registry.add_callback(AfterModelCallEvent, self.on_after_model)
        registry.add_callback(BeforeToolCallEvent, self.on_before_tool)
        registry.add_callback(AfterToolCallEvent, self.on_after_tool)

    def _ctx(self, invocation_state: dict[str, Any]) -> TrustedContext | None:
        return ctx_from_state(invocation_state)

    def on_before_invocation(self, event: BeforeInvocationEvent) -> None:
        ctx = self._ctx(event.invocation_state)
        if ctx:
            ctx.services.repo.append_audit(
                case_id=ctx.case_id,
                event_type=AuditEventType.INVOCATION_STARTED,
                actor=ctx.actor,
                summary=f"Agent invocation started for {ctx.event.event_type.value} ({ctx.event_id})",
                kind=AuditKind.TELEMETRY,
                metadata={"session_id": ctx.event.session_id, "trace_id": ctx.event.trace_id},
            )

    def on_after_invocation(self, event: AfterInvocationEvent) -> None:
        ctx = self._ctx(event.invocation_state)
        if ctx:
            stop = getattr(event.result, "stop_reason", None) if event.result is not None else None
            ctx.services.repo.append_audit(
                case_id=ctx.case_id,
                event_type=AuditEventType.INVOCATION_FINISHED,
                actor=ctx.actor,
                summary=f"Agent invocation finished: stop_reason={stop}",
                kind=AuditKind.TELEMETRY,
                metadata={
                    "tool_attempts": ctx.counters.tool_attempts,
                    "retries": ctx.counters.retries,
                    "model_calls": ctx.counters.model_calls,
                },
            )

    def on_before_model(self, event: BeforeModelCallEvent) -> None:
        ctx = self._ctx(event.invocation_state)
        if ctx:
            ctx.counters.model_calls += 1

    def on_after_model(self, event: AfterModelCallEvent) -> None:
        ctx = self._ctx(event.invocation_state)
        if ctx:
            stop = (
                getattr(event.stop_response, "stop_reason", None) if event.stop_response is not None else None
            )
            ctx.services.repo.append_audit(
                case_id=ctx.case_id,
                event_type=AuditEventType.MODEL_CALL,
                actor=ctx.actor,
                summary=f"Model call {ctx.counters.model_calls}: stop_reason={stop}",
                kind=AuditKind.TELEMETRY,
                output_status=str(stop),
            )

    def on_before_tool(self, event: BeforeToolCallEvent) -> None:
        ctx = self._ctx(event.invocation_state)
        if ctx is None:
            return
        ctx.counters.tool_attempts += 1
        name = str(event.tool_use.get("name"))
        ctx.services.repo.append_audit(
            case_id=ctx.case_id,
            event_type=AuditEventType.TOOL_ATTEMPT,
            actor=ctx.actor,
            tool_name=name,
            summary=f"Tool attempt {ctx.counters.tool_attempts}: {name}",
            input_digest=_digest(event.tool_use.get("input")),
            kind=AuditKind.TOOL_ATTEMPT,
            metadata={"tool_use_id": event.tool_use.get("toolUseId"), "event_id": ctx.event_id},
        )

    def on_after_tool(self, event: AfterToolCallEvent) -> None:
        ctx = self._ctx(event.invocation_state)
        if ctx is None:
            return
        ctx.counters.tool_results += 1
        name = str(event.tool_use.get("name"))
        tool_use_id = str(event.tool_use.get("toolUseId"))
        body = _body(event.result)
        status = "cancelled" if event.cancel_message else str(event.result.get("status", "unknown"))
        retryable = (
            bool(body.get("retryable"))
            and name in RETRYABLE_TOOLS
            and event.exception is None
            and not event.cancel_message
        )
        attempts = ctx.counters.retries_by_tool_use.get(tool_use_id, 0)
        will_retry = retryable and attempts < ctx.budgets.max_retries_per_tool_call
        reason_codes: tuple[ReasonCode, ...] = ()
        if retryable:
            reason_codes = (ReasonCode.TOOL_FAILURE_TRANSIENT,)
        ctx.services.repo.append_audit(
            case_id=ctx.case_id,
            event_type=AuditEventType.TOOL_RESULT,
            actor=ctx.actor,
            tool_name=name,
            summary=f"{name} → {body.get('status', status)}"
            + (f" (retrying, attempt {attempts + 2})" if will_retry else "")
            + (f"; {event.cancel_message}" if event.cancel_message else ""),
            output_status=status,
            reason_codes=reason_codes,
            operation_id=body.get("operation_id"),
            kind=AuditKind.TOOL_ATTEMPT,
            metadata={
                "tool_use_id": tool_use_id,
                "duration_s": round(event.duration or 0.0, 4),
                "retryable": retryable,
            },
        )
        if will_retry:
            ctx.counters.retries_by_tool_use[tool_use_id] = attempts + 1
            ctx.counters.retries += 1
            event.retry = True
