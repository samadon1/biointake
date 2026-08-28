"""Injectable clocks so every run of the deterministic demo produces identical timestamps."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


class SteppingClock:
    """Starts at a fixed instant and advances by a fixed step on every call."""

    def __init__(self, start: datetime, step_seconds: float = 1.0) -> None:
        self._now = start
        self._step = timedelta(seconds=step_seconds)

    def __call__(self) -> datetime:
        current = self._now
        self._now = self._now + self._step
        return current
