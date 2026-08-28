"""Chain-of-custody verification: required handoff events, present and in order."""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ..domain.enums import CheckStatus, ReasonCode
from ..domain.policies import CustodyRule


class CustodyEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    sample_id: str
    event: str
    actor_id: str
    timestamp: datetime
    location: str = ""


def parse_custody_log(data: bytes) -> list[CustodyEvent]:
    return [CustodyEvent.model_validate(e) for e in json.loads(data)["events"]]


class CustodyOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: CheckStatus
    reason_codes: tuple[ReasonCode, ...]
    observed: str
    expected: str
    summary: str


def evaluate_custody(sample_id: str, events: list[CustodyEvent], rule: CustodyRule) -> CustodyOutcome:
    mine = sorted((e for e in events if e.sample_id == sample_id), key=lambda e: e.timestamp)
    present = {e.event: e for e in mine}
    expected = " → ".join(rule.required_events)
    missing = [ev for ev in rule.required_events if ev not in present]
    if missing:
        return CustodyOutcome(
            status=CheckStatus.UNAVAILABLE,
            reason_codes=(ReasonCode.CUSTODY_EVENT_MISSING,),
            observed=" → ".join(e.event for e in mine) or "no events",
            expected=expected,
            summary=f"Missing custody events: {', '.join(missing)}.",
        )
    order = [present[ev].timestamp for ev in rule.required_events]
    if any(a > b for a, b in zip(order, order[1:], strict=False)):
        return CustodyOutcome(
            status=CheckStatus.FAIL,
            reason_codes=(ReasonCode.CUSTODY_ORDER_INVALID,),
            observed=" → ".join(e.event for e in mine),
            expected=expected,
            summary="Custody events are out of chronological order.",
        )
    if any(not e.actor_id for e in mine):
        return CustodyOutcome(
            status=CheckStatus.FAIL,
            reason_codes=(ReasonCode.CUSTODY_ORDER_INVALID,),
            observed="event without actor",
            expected=expected,
            summary="A custody event has no recorded actor.",
        )
    return CustodyOutcome(
        status=CheckStatus.PASS,
        reason_codes=(),
        observed=" → ".join(e.event for e in mine),
        expected=expected,
        summary=f"{len(mine)} custody events present and ordered.",
    )
