"""Which Lob production run a letter submitted now joins. Pure: callers pass the time in.

Lob prints once per production day. A letter joins today's run only if it is released
before the 10 a.m. Pacific daily cutoff, and the account's cancellation window delays
release: with a 60-minute window the effective cutoff is 9 a.m. Pacific (help.lob.com,
"Managing mail settings", read 2026-10-29). New accounts default to a 4-hour window.

Production days are Monday to Friday except Lob's no-production days. Lob publishes those
for one year at a time (help.lob.com, "Getting support", 2026 list read 2026-10-29): the
federal holidays except Columbus Day and Veterans Day, plus the day after Thanksgiving,
Christmas Eve and New Year's Eve. Because later years are not published, every observed
federal holiday plus those three extra days counts as a no-production day. Counting extra
days off can only make a computed run later, which is the safe direction for deadlines.

Pacific time is computed from the US daylight-saving rule (second Sunday of March to first
Sunday of November, 2 a.m. local), so no time-zone database is read.
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, time, timedelta
from functools import cache

from parkingbreaker.holidays import nth_weekday, observed_holidays
from parkingbreaker.mail_config import LOB_CANCELLATION_WINDOW

LOB_DAILY_CUTOFF = time(10, 0)
_PST = timedelta(hours=-8)
_PDT = timedelta(hours=-7)


def pacific(moment: datetime) -> datetime:
    """The Pacific wall-clock time of an aware instant (naive result)."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("moment must be timezone-aware")
    utc = moment.astimezone(UTC).replace(tzinfo=None)
    year = utc.year
    # DST starts 2 a.m. PST (10:00 UTC) and ends 2 a.m. PDT (09:00 UTC).
    start = datetime.combine(nth_weekday(year, 3, calendar.SUNDAY, 2), time(10))
    end = datetime.combine(nth_weekday(year, 11, calendar.SUNDAY, 1), time(9))
    return utc + (_PDT if start <= utc < end else _PST)


@cache
def no_production_days(year: int) -> frozenset[date]:
    thanksgiving = nth_weekday(year, 11, calendar.THURSDAY, 4)
    extra = {thanksgiving + timedelta(days=1), date(year, 12, 24), date(year, 12, 31)}
    return observed_holidays(year) | frozenset(extra)


def is_production_day(day: date) -> bool:
    return day.weekday() < calendar.SATURDAY and day not in no_production_days(day.year)


def next_production_day(day: date) -> date:
    """First printing day strictly after day."""
    day += timedelta(days=1)
    while not is_production_day(day):
        day += timedelta(days=1)
    return day


def production_date(
    submitted_at: datetime, cancellation_window: timedelta = LOB_CANCELLATION_WINDOW
) -> date:
    """The Pacific civil date of the Lob production run this submission joins."""
    if cancellation_window < timedelta(0):
        raise ValueError("cancellation window cannot be negative")
    if submitted_at.utcoffset() is None:
        raise ValueError("moment must be timezone-aware")
    released = pacific(submitted_at.astimezone(UTC) + cancellation_window)
    day = released.date()
    if is_production_day(day) and released.time() < LOB_DAILY_CUTOFF:
        return day
    return next_production_day(day)
