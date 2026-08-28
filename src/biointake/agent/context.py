"""Trusted per-invocation context injected into tools/hooks via Strands `invocation_state`.

Nothing here is model-visible or model-writable. Tools read authority (case id, actor, event id,
service handles, budgets) from this object only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strands.types.tools import ToolContext

from ..domain.models import ActorContext
from ..services.intake import IntakeService
from .events import EvidenceDelivery, InvocationEvent

INVOCATION_KEY = "biointake_trusted_context"


@dataclass
class Budgets:
    max_tool_calls: int = 25
    max_retries_per_tool_call: int = 2
    max_turns: int = 12


@dataclass
class Counters:
    tool_attempts: int = 0
    tool_results: int = 0
    retries: int = 0
    model_calls: int = 0
    intervention_denials: int = 0
    retries_by_tool_use: dict[str, int] = field(default_factory=dict)


@dataclass
class TrustedContext:
    event: InvocationEvent
    services: IntakeService
    actor: ActorContext
    budgets: Budgets = field(default_factory=Budgets)
    counters: Counters = field(default_factory=Counters)
    tool_failure_injector: dict[str, int] = field(
        default_factory=dict
    )  # tests: tool_name → remaining failures
    audit_start_sequence: int = 0

    @property
    def case_id(self) -> str:
        return self.event.case_id

    @property
    def event_id(self) -> str:
        return self.event.event_id

    @property
    def evidence(self) -> EvidenceDelivery | None:
        return self.event.evidence

    def as_invocation_state(self) -> dict[str, Any]:
        return {INVOCATION_KEY: self}


def ctx_from(tool_context: ToolContext) -> TrustedContext:
    ctx = tool_context.invocation_state.get(INVOCATION_KEY)
    if not isinstance(ctx, TrustedContext):
        raise RuntimeError("trusted invocation context missing; refusing to act")
    return ctx


def ctx_from_state(invocation_state: dict[str, Any]) -> TrustedContext | None:
    ctx = invocation_state.get(INVOCATION_KEY)
    return ctx if isinstance(ctx, TrustedContext) else None
