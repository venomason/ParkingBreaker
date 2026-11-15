"""US federal holidays and business days, computed for any year. Pure, no tables.

Rules follow 5 U.S.C. 6103 and OPM's observance policy:

- Fixed-date holidays: New Year's Day (Jan 1), Juneteenth (Jun 19, from 2021),
  Independence Day (Jul 4), Veterans Day (Nov 11), Christmas Day (Dec 25).
- Monday holidays (Uniform Monday Holiday Act, effective 1971): Martin Luther King Jr. Day
  (3rd Monday of January, from 1986), Washington's Birthday (3rd Monday of February),
  Memorial Day (last Monday of May), Labor Day (1st Monday of September), Columbus Day
  (2nd Monday of October).
- Thanksgiving: 4th Thursday of November.
- Observed: a holiday on Saturday is observed the Friday before; on Sunday, the Monday after.
  So New Year's Day on a Saturday is observed on December 31 of the previous year.

Inauguration Day (DC area only) is not included. Years before 1971 are rejected because the
Monday-holiday rules did not apply yet.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from functools import cache

FIRST_SUPPORTED_YEAR = 1971

_MONDAY = calendar.MONDAY
_THURSDAY = calendar.THURSDAY


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th (1-based) `weekday` of the month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    """The last `weekday` of the month."""
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def observed(day: date) -> date:
    """Saturday holidays move to Friday, Sunday holidays to Monday."""
    if day.weekday() == calendar.SATURDAY:
        return day - timedelta(days=1)
    if day.weekday() == calendar.SUNDAY:
        return day + timedelta(days=1)
    return day


def actual_holidays(year: int) -> dict[str, date]:
    """Federal holidays on their nominal dates for `year`."""
    if year < FIRST_SUPPORTED_YEAR:
        raise ValueError(f"years before {FIRST_SUPPORTED_YEAR} are not supported: {year}")
    days = {
        "New Year's Day": date(year, 1, 1),
        "Washington's Birthday": nth_weekday(year, 2, _MONDAY, 3),
        "Memorial Day": last_weekday(year, 5, _MONDAY),
        "Independence Day": date(year, 7, 4),
        "Labor Day": nth_weekday(year, 9, _MONDAY, 1),
        "Columbus Day": nth_weekday(year, 10, _MONDAY, 2),
        "Veterans Day": date(year, 11, 11),
        "Thanksgiving Day": nth_weekday(year, 11, _THURSDAY, 4),
        "Christmas Day": date(year, 12, 25),
    }
    if year >= 1986:
        days["Martin Luther King Jr. Day"] = nth_weekday(year, 1, _MONDAY, 3)
    if year >= 2021:
        days["Juneteenth"] = date(year, 6, 19)
    return days


@cache
def observed_holidays(year: int) -> frozenset[date]:
    """Days in calendar `year` on which federal offices are closed for a holiday.

    Includes December 31 when the next New Year's Day falls on a Saturday, and excludes
    January 1 of `year` in that case (it is observed in the previous year).
    """
    days = {observed(d) for d in actual_holidays(year).values()}
    next_new_year = observed(date(year + 1, 1, 1))
    if next_new_year.year == year:
        days.add(next_new_year)
    return frozenset(d for d in days if d.year == year)


def is_business_day(day: date) -> bool:
    """Monday to Friday and not an observed federal holiday."""
    return day.weekday() < calendar.SATURDAY and day not in observed_holidays(day.year)


def add_business_days(start: date, days: int) -> date:
    """The date `days` business days after `start` (start itself is not counted).

    With days == 0 the result is `start` if it is a business day, else the next one.
    """
    if days < 0:
        raise ValueError(f"days must be non-negative: {days}")
    current = start
    if days == 0:
        while not is_business_day(current):
            current += timedelta(days=1)
        return current
    remaining = days
    while remaining:
        current += timedelta(days=1)
        if is_business_day(current):
            remaining -= 1
    return current


def previous_business_day(day: date) -> date:
    """The closest business day strictly before `day`."""
    current = day - timedelta(days=1)
    while not is_business_day(current):
        current -= timedelta(days=1)
    return current
