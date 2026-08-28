"""Deterministic transport-temperature evaluation. No model ever touches these numbers."""

from __future__ import annotations

import csv
import io
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ..domain.enums import CheckStatus, ReasonCode
from ..domain.policies import TemperatureRule


class Reading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    timestamp: datetime
    temp_c: float


class TemperatureSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    logger_id: str
    status: CheckStatus
    reason_codes: tuple[ReasonCode, ...]
    reading_count: int
    min_c: float | None
    max_c: float | None
    minutes_out_of_range: float
    longest_continuous_minutes: float
    largest_gap_minutes: float
    malformed_rows: int
    summary: str


def parse_logger_csv(data: bytes) -> tuple[list[Reading], int]:
    """Returns (readings sorted by time, malformed_row_count)."""
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    readings: list[Reading] = []
    malformed = 0
    for raw in reader:
        try:
            ts = raw.get("timestamp", "") or ""
            temp = raw.get("temp_c", "") or ""
            readings.append(Reading(timestamp=datetime.fromisoformat(ts.strip()), temp_c=float(temp)))
        except (ValueError, TypeError):
            malformed += 1
    readings.sort(key=lambda r: r.timestamp)
    return readings, malformed


def evaluate_logger(logger_id: str, data: bytes | None, rule: TemperatureRule) -> TemperatureSummary:
    if data is None:
        return TemperatureSummary(
            logger_id=logger_id,
            status=CheckStatus.UNAVAILABLE,
            reason_codes=(ReasonCode.TEMPERATURE_LOG_MISSING,),
            reading_count=0,
            min_c=None,
            max_c=None,
            minutes_out_of_range=0.0,
            longest_continuous_minutes=0.0,
            largest_gap_minutes=0.0,
            malformed_rows=0,
            summary=f"No temperature log available for {logger_id}.",
        )
    readings, malformed = parse_logger_csv(data)
    if len(readings) < 2 or malformed:
        return TemperatureSummary(
            logger_id=logger_id,
            status=CheckStatus.UNAVAILABLE,
            reason_codes=(ReasonCode.TEMPERATURE_LOG_MALFORMED,),
            reading_count=len(readings),
            min_c=min((r.temp_c for r in readings), default=None),
            max_c=max((r.temp_c for r in readings), default=None),
            minutes_out_of_range=0.0,
            longest_continuous_minutes=0.0,
            largest_gap_minutes=0.0,
            malformed_rows=malformed,
            summary=f"Log for {logger_id} unusable: {malformed} malformed rows, {len(readings)} readings.",
        )

    total_out = 0.0
    longest = 0.0
    current_run = 0.0
    largest_gap = 0.0
    for prev, cur in zip(readings, readings[1:], strict=False):
        gap = (cur.timestamp - prev.timestamp).total_seconds() / 60.0
        largest_gap = max(largest_gap, gap)
        out = prev.temp_c < rule.min_c or prev.temp_c > rule.max_c
        if out:
            total_out += gap
            current_run += gap
            longest = max(longest, current_run)
        else:
            current_run = 0.0
    min_c = min(r.temp_c for r in readings)
    max_c = max(r.temp_c for r in readings)

    if largest_gap > rule.max_gap_minutes:
        return TemperatureSummary(
            logger_id=logger_id,
            status=CheckStatus.UNAVAILABLE,
            reason_codes=(ReasonCode.TEMPERATURE_LOG_MALFORMED,),
            reading_count=len(readings),
            min_c=min_c,
            max_c=max_c,
            minutes_out_of_range=total_out,
            longest_continuous_minutes=longest,
            largest_gap_minutes=largest_gap,
            malformed_rows=malformed,
            summary=f"Log for {logger_id} has a {largest_gap:.0f}-minute gap (limit {rule.max_gap_minutes:.0f}).",
        )
    codes: tuple[ReasonCode, ...]
    if total_out > rule.tolerance_minutes:
        status, codes = CheckStatus.FAIL, (ReasonCode.TEMPERATURE_EXCURSION,)
        summary = (
            f"{logger_id}: {total_out:.0f} min outside {rule.min_c:g}–{rule.max_c:g} °C "
            f"(max {max_c:.1f} °C); tolerance {rule.tolerance_minutes:g} min exceeded."
        )
    else:
        status, codes = CheckStatus.PASS, ()
        summary = (
            f"{logger_id}: {len(readings)} readings, {min_c:.1f}–{max_c:.1f} °C, "
            f"{total_out:.0f} min outside range (within {rule.tolerance_minutes:g}-min tolerance)."
        )
    return TemperatureSummary(
        logger_id=logger_id,
        status=status,
        reason_codes=codes,
        reading_count=len(readings),
        min_c=min_c,
        max_c=max_c,
        minutes_out_of_range=total_out,
        longest_continuous_minutes=longest,
        largest_gap_minutes=largest_gap,
        malformed_rows=malformed,
        summary=summary,
    )
