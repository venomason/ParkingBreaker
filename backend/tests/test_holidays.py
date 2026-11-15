from __future__ import annotations

import calendar
from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from parkingbreaker.holidays import (
    FIRST_SUPPORTED_YEAR,
    actual_holidays,
    add_business_days,
    is_business_day,
    observed,
    observed_holidays,
)

YEARS = st.integers(min_value=FIRST_SUPPORTED_YEAR, max_value=2200)
DAYS = st.dates(min_value=date(FIRST_SUPPORTED_YEAR, 1, 1), max_value=date(2199, 12, 1))


def test_2026_matches_the_published_opm_schedule() -> None:
    assert observed_holidays(2026) == {
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),  # July 4 is a Saturday
        date(2026, 9, 7),
        date(2026, 10, 12),
        date(2026, 11, 11),
        date(2026, 11, 26),
        date(2026, 12, 25),
    }


def test_saturday_new_year_is_observed_the_previous_december() -> None:
    assert date(2022, 1, 1).weekday() == calendar.SATURDAY
    assert date(2021, 12, 31) in observed_holidays(2021)
    assert date(2022, 1, 1) not in observed_holidays(2022)
    assert date(2021, 12, 31) not in observed_holidays(2022)


def test_sunday_holidays_move_to_monday() -> None:
    assert date(2023, 1, 2) in observed_holidays(2023)  # Jan 1 2023 was a Sunday
    assert date(2022, 12, 26) in observed_holidays(2022)  # Christmas 2022 was a Sunday


def test_holidays_introduced_later_are_absent_before_their_start() -> None:
    assert "Juneteenth" not in actual_holidays(2020)
    assert "Juneteenth" in actual_holidays(2021)
    assert "Martin Luther King Jr. Day" not in actual_holidays(1985)
    assert "Martin Luther King Jr. Day" in actual_holidays(1986)


def test_years_before_monday_holiday_act_are_rejected() -> None:
    with pytest.raises(ValueError, match="not supported"):
        actual_holidays(FIRST_SUPPORTED_YEAR - 1)


@given(YEARS)
def test_floating_holidays_land_on_the_right_weekday_and_range(year: int) -> None:
    h = actual_holidays(year)
    assert h["Washington's Birthday"].weekday() == calendar.MONDAY
    assert 15 <= h["Washington's Birthday"].day <= 21
    assert h["Memorial Day"].weekday() == calendar.MONDAY
    assert 25 <= h["Memorial Day"].day <= 31
    assert h["Labor Day"].weekday() == calendar.MONDAY
    assert h["Labor Day"].day <= 7
    assert h["Columbus Day"].weekday() == calendar.MONDAY
    assert 8 <= h["Columbus Day"].day <= 14
    assert h["Thanksgiving Day"].weekday() == calendar.THURSDAY
    assert 22 <= h["Thanksgiving Day"].day <= 28
    if year >= 1986:
        assert h["Martin Luther King Jr. Day"].weekday() == calendar.MONDAY
        assert 15 <= h["Martin Luther King Jr. Day"].day <= 21


@given(YEARS)
def test_observed_holidays_are_weekdays_in_the_year_near_their_nominal_date(year: int) -> None:
    days = observed_holidays(year)
    assert all(d.weekday() < calendar.SATURDAY and d.year == year for d in days)
    nominal = [*actual_holidays(year).values(), date(year + 1, 1, 1)]
    assert all(any(abs((d - n).days) <= 1 for n in nominal) for d in days)


@given(YEARS)
def test_holiday_count_accounts_for_new_year_shifts(year: int) -> None:
    expected = len(actual_holidays(year))
    if date(year, 1, 1).weekday() == calendar.SATURDAY:
        expected -= 1  # observed on Dec 31 of the previous year
    if date(year + 1, 1, 1).weekday() == calendar.SATURDAY:
        expected += 1  # next year's New Year's Day is observed on Dec 31
    assert len(observed_holidays(year)) == expected


@given(DAYS)
def test_observed_moves_weekends_to_adjacent_weekdays(day: date) -> None:
    moved = observed(day)
    assert moved.weekday() < calendar.SATURDAY
    assert abs((moved - day).days) <= 1


@given(DAYS, st.integers(min_value=1, max_value=30))
def test_add_business_days_counts_exactly_n_business_days(start: date, n: int) -> None:
    end = add_business_days(start, n)
    assert is_business_day(end)
    span = [start + timedelta(days=i) for i in range(1, (end - start).days + 1)]
    assert sum(is_business_day(d) for d in span) == n


@given(DAYS, st.integers(min_value=0, max_value=30))
def test_add_business_days_is_monotonic_in_start(start: date, n: int) -> None:
    assert add_business_days(start, n) <= add_business_days(start + timedelta(days=1), n)


def test_add_zero_business_days_rolls_to_next_business_day() -> None:
    assert add_business_days(date(2026, 7, 3), 0) == date(2026, 7, 6)  # observed July 4
    assert add_business_days(date(2026, 7, 7), 0) == date(2026, 7, 7)


def test_add_business_days_rejects_negative() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        add_business_days(date(2026, 1, 5), -1)
