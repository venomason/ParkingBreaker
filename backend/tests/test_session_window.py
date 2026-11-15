from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from parkingbreaker.case_state import CaseState
from parkingbreaker.session_window import (
    DEFAULT_WINDOW,
    UNWINDOWED_STATES,
    WINDOWED_STATES,
    WindowStatus,
    applies_to,
    seconds_remaining,
    window_status,
)

START = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return START + timedelta(minutes=minutes)


def test_default_window_is_two_hours() -> None:
    assert timedelta(hours=2) == DEFAULT_WINDOW


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (0, WindowStatus.ACTIVE),
        (60, WindowStatus.ACTIVE),
        (114 + 59 / 60, WindowStatus.ACTIVE),
        (115, WindowStatus.WARNING),  # exactly 5:00 left of 120m
        (119 + 59 / 60, WindowStatus.WARNING),
        (120, WindowStatus.EXPIRED),  # exactly 120:00 elapsed
        (121, WindowStatus.EXPIRED),
        (600, WindowStatus.EXPIRED),
    ],
)
def test_window_status_boundaries(minutes: float, expected: WindowStatus) -> None:
    assert window_status(START, at(minutes)) is expected


def test_one_microsecond_before_expiry_is_still_warning() -> None:
    now = at(120) - timedelta(microseconds=1)
    assert window_status(START, now) is WindowStatus.WARNING
    assert seconds_remaining(START, now) == 0


def test_seconds_remaining() -> None:
    assert seconds_remaining(START, START) == 7200
    assert seconds_remaining(START, at(115)) == 300
    assert seconds_remaining(START, at(120)) == 0
    assert seconds_remaining(START, at(140)) == 0
    assert seconds_remaining(START, START + timedelta(seconds=1.5)) == 7198


def test_now_before_start_reports_full_window_plus_skew() -> None:
    # Clock skew: remaining can exceed the window; status stays ACTIVE.
    early = START - timedelta(seconds=10)
    assert seconds_remaining(START, early) == 7210
    assert window_status(START, early) is WindowStatus.ACTIVE


def test_window_length_is_configurable() -> None:
    ten = timedelta(minutes=10)
    assert seconds_remaining(START, START, window=ten) == 600
    assert window_status(START, at(4), window=ten) is WindowStatus.ACTIVE
    assert window_status(START, at(5), window=ten) is WindowStatus.WARNING
    assert window_status(START, at(10), window=ten) is WindowStatus.EXPIRED


def test_warning_threshold_is_configurable() -> None:
    assert window_status(START, at(115), warning=timedelta(minutes=2)) is WindowStatus.ACTIVE
    assert window_status(START, at(118), warning=timedelta(minutes=2)) is WindowStatus.WARNING


@pytest.mark.parametrize("bad", [timedelta(0), timedelta(minutes=-1)])
def test_non_positive_window_rejected(bad: timedelta) -> None:
    with pytest.raises(ValueError, match="window must be positive"):
        window_status(START, START, window=bad)
    with pytest.raises(ValueError, match="window must be positive"):
        seconds_remaining(START, START, window=bad)


def test_negative_warning_rejected() -> None:
    with pytest.raises(ValueError, match="warning"):
        window_status(START, START, warning=timedelta(seconds=-1))


def test_mixed_timezones_compare_by_instant() -> None:
    plus_two = timezone(timedelta(hours=2))
    now = datetime(2026, 10, 11, 15, 55, 0, tzinfo=plus_two)  # 13:55 UTC (at 115m)
    assert seconds_remaining(START, now) == 300
    assert window_status(START, now) is WindowStatus.WARNING


def test_naive_started_at_rejected() -> None:
    naive = datetime(2026, 10, 11, 12, 0, 0)
    with pytest.raises(ValueError, match="started_at must be timezone-aware"):
        window_status(naive, START)
    with pytest.raises(ValueError, match="started_at must be timezone-aware"):
        seconds_remaining(naive, START)


def test_naive_now_rejected() -> None:
    naive = datetime(2026, 10, 11, 12, 5, 0)
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        window_status(START, naive)
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        seconds_remaining(START, naive)


@pytest.mark.parametrize(
    "state",
    [
        CaseState.RESEARCHING,
        CaseState.NEEDS_CUSTOMER,
        CaseState.DRAFTING,
        CaseState.AWAITING_ADOPTION,
        CaseState.FROZEN,
    ],
)
def test_applies_before_submission(state: CaseState) -> None:
    assert applies_to(state) is True


@pytest.mark.parametrize(
    "state",
    [
        CaseState.SUBMITTING,
        CaseState.IN_TRANSIT,
        CaseState.DELIVERED,
        CaseState.RETURNED,
        CaseState.CLOSED_RETURNED,
        CaseState.STOPPED_NO_CHARGE,
        CaseState.INTAKE,
    ],
)
def test_does_not_apply_from_submitting_on_or_outside_window(state: CaseState) -> None:
    assert applies_to(state) is False


def test_state_sets_partition_case_states() -> None:
    assert WINDOWED_STATES.isdisjoint(UNWINDOWED_STATES)
    assert frozenset(CaseState) == WINDOWED_STATES | UNWINDOWED_STATES
