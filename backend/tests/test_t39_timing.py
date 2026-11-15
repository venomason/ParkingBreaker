"""Subsecond cutoff and adjacent-year controls for the existing pure Lob run model."""

from datetime import UTC, date, datetime, timedelta

import pytest

from parkingbreaker.lob_run import is_production_day, production_date


@pytest.mark.parametrize("window", [timedelta(0), timedelta(hours=1), timedelta(hours=4)])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_release_cutoff_is_strict_to_the_microsecond(window: timedelta, offset: int) -> None:
    # 10 a.m. Pacific on a normal production day is 17:00 UTC in October.
    submitted = datetime(2026, 10, 15, 17, tzinfo=UTC) - window
    actual = production_date(submitted + timedelta(microseconds=offset), window)
    assert actual == (date(2026, 10, 15) if offset < 0 else date(2026, 10, 16))


@pytest.mark.parametrize(
    ("year", "expected"),
    [(2021, date(2022, 1, 3)), (2022, date(2023, 1, 3)), (2026, date(2027, 1, 4))],
)
def test_year_end_runs_skip_extra_and_observed_holidays(year: int, expected: date) -> None:
    assert not is_production_day(date(year, 12, 31))
    # December 30 at 10 a.m. PST crosses the production cutoff.
    assert production_date(datetime(year, 12, 30, 18, tzinfo=UTC), timedelta(0)) == expected
    assert production_date(datetime(year, 12, 30, 14, tzinfo=UTC), timedelta(hours=4)) == expected
