from __future__ import annotations

from datetime import UTC, datetime, timedelta

from biointake.domain.enums import CheckStatus, ReasonCode
from biointake.services.temperature import evaluate_logger, parse_logger_csv


def csv_of(temps: list[float], step_min: int = 1, gap_after: int | None = None, gap_min: int = 0) -> bytes:
    t = datetime(2026, 8, 25, 8, 0, tzinfo=UTC)
    lines = ["timestamp,temp_c"]
    for i, v in enumerate(temps):
        lines.append(f"{t.isoformat()},{v}")
        t += timedelta(minutes=step_min + (gap_min if gap_after is not None and i == gap_after else 0))
    return "\n".join(lines).encode()


def test_missing_log_is_unavailable(policy):
    s = evaluate_logger("L", None, policy.temperature)
    assert s.status is CheckStatus.UNAVAILABLE and s.reason_codes == (ReasonCode.TEMPERATURE_LOG_MISSING,)


def test_malformed_rows_make_log_unusable(policy):
    data = b"timestamp,temp_c\n2026-08-25T08:00:00+00:00,4.0\nbroken,row\n2026-08-25T08:05:00+00:00,4.2\n"
    readings, malformed = parse_logger_csv(data)
    assert len(readings) == 2 and malformed == 1
    s = evaluate_logger("L", data, policy.temperature)
    assert s.status is CheckStatus.UNAVAILABLE and s.reason_codes == (ReasonCode.TEMPERATURE_LOG_MALFORMED,)


def test_excursion_duration_is_exact(policy):
    temps = [4.0] * 10 + [9.0] * 19 + [4.0] * 10
    s = evaluate_logger("L", csv_of(temps), policy.temperature)
    assert s.status is CheckStatus.FAIL
    assert s.minutes_out_of_range == 19.0 and s.longest_continuous_minutes == 19.0
    assert s.max_c == 9.0


def test_blip_within_tolerance_passes(policy):
    temps = [4.0] * 10 + [8.3] + [4.0] * 10
    s = evaluate_logger("L", csv_of(temps, step_min=5), policy.temperature)
    assert s.status is CheckStatus.PASS and s.minutes_out_of_range == 5.0


def test_cumulative_excursions_add_up(policy):
    temps = [4.0] * 5 + [9.0] * 6 + [4.0] * 5 + [9.0] * 6 + [4.0] * 5
    s = evaluate_logger("L", csv_of(temps), policy.temperature)
    assert (
        s.status is CheckStatus.FAIL
        and s.minutes_out_of_range == 12.0
        and s.longest_continuous_minutes == 6.0
    )


def test_large_gap_makes_log_unavailable(policy):
    temps = [4.0] * 20
    s = evaluate_logger("L", csv_of(temps, gap_after=10, gap_min=45), policy.temperature)
    assert s.status is CheckStatus.UNAVAILABLE and s.largest_gap_minutes == 46.0


def test_fixture_loggers_match_scenario(package, policy):
    a = evaluate_logger("LOGGER-A", package.temperature_logs["LOGGER-A"], policy.temperature)
    b = evaluate_logger("LOGGER-B", package.temperature_logs["LOGGER-B"], policy.temperature)
    assert a.status is CheckStatus.PASS
    assert b.status is CheckStatus.FAIL and b.minutes_out_of_range == 19.0 and b.max_c == 11.8
