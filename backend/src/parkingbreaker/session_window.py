"""The session window (architecture v6, section 6.1). Pure: callers pass the clock in.

The window is a fixed length (two hours by default) starting at authorization. It is not
extended by activity, questions or redrafts, so there is no inactivity logic here. A
warning is shown once 5 minutes or less remain. The window covers the letter states from
authorization up to freezing; once submission starts it no longer applies.

All datetimes must be timezone-aware; naive datetimes are rejected with ValueError.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from parkingbreaker.case_state import CaseState

DEFAULT_WINDOW = timedelta(hours=2)
WARNING_THRESHOLD = timedelta(minutes=5)


class WindowStatus(StrEnum):
    ACTIVE = "ACTIVE"
    WARNING = "WARNING"
    EXPIRED = "EXPIRED"


# Letter states (v6 section 8.1) the window governs: after authorization, before SUBMITTING.
WINDOWED_STATES: frozenset[CaseState] = frozenset(
    {
        CaseState.RESEARCHING,
        CaseState.NEEDS_CUSTOMER,
        CaseState.DRAFTING,
        CaseState.AWAITING_ADOPTION,
        CaseState.FROZEN,
    }
)

# States the window does not govern: untimed intake, submission onward, and the stop.
UNWINDOWED_STATES: frozenset[CaseState] = frozenset(
    {
        CaseState.INTAKE,
        CaseState.SUBMITTING,
        CaseState.IN_TRANSIT,
        CaseState.DELIVERED,
        CaseState.RETURNED,
        CaseState.CLOSED_RETURNED,
        CaseState.STOPPED_NO_CHARGE,
    }
)


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _check_window(window: timedelta) -> None:
    if window <= timedelta(0):
        raise ValueError("window must be positive")


def remaining(started_at: datetime, now: datetime, window: timedelta = DEFAULT_WINDOW) -> timedelta:
    """Time left in the window, never below zero."""
    _require_aware("started_at", started_at)
    _require_aware("now", now)
    _check_window(window)
    left = started_at + window - now
    return max(left, timedelta(0))


def seconds_remaining(
    started_at: datetime, now: datetime, window: timedelta = DEFAULT_WINDOW
) -> int:
    """Whole seconds left in the window (rounded down), never below zero."""
    return int(remaining(started_at, now, window) // timedelta(seconds=1))


def window_status(
    started_at: datetime,
    now: datetime,
    window: timedelta = DEFAULT_WINDOW,
    warning: timedelta = WARNING_THRESHOLD,
) -> WindowStatus:
    """ACTIVE, WARNING (warning or less left) or EXPIRED (nothing left)."""
    if warning < timedelta(0):
        raise ValueError("warning must not be negative")
    left = remaining(started_at, now, window)
    if left <= timedelta(0):
        return WindowStatus.EXPIRED
    if left <= warning:
        return WindowStatus.WARNING
    return WindowStatus.ACTIVE


def applies_to(letter: CaseState) -> bool:
    """Whether the window governs a letter in this state. False from SUBMITTING onward."""
    return letter in WINDOWED_STATES
