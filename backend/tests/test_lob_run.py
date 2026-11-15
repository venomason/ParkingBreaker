from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from parkingbreaker.lob_run import is_production_day, pacific, production_date

ZERO = timedelta(0)


@pytest.mark.parametrize(
    ("submitted", "window", "run"),
    [
        # Thursday 2026-10-15, PDT (UTC-7): 9:59 a.m. joins today's run, 10:00 does not.
        (datetime(2026, 10, 15, 16, 59, tzinfo=UTC), ZERO, date(2026, 10, 15)),
        (datetime(2026, 10, 15, 17, 0, tzinfo=UTC), ZERO, date(2026, 10, 15) + timedelta(1)),
        # The default 4-hour window moves the cutoff to 6 a.m. Pacific.
        (datetime(2026, 10, 15, 12, 59, tzinfo=UTC), timedelta(hours=4), date(2026, 10, 15)),
        (datetime(2026, 10, 15, 13, 0, tzinfo=UTC), timedelta(hours=4), date(2026, 10, 16)),
        # Friday after the cutoff skips the weekend.
        (datetime(2026, 10, 16, 18, 0, tzinfo=UTC), ZERO, date(2026, 10, 19)),
        # 8 p.m. Pacific Wednesday is already Thursday in UTC; the run is still Thursday.
        (datetime(2026, 10, 15, 3, 0, tzinfo=UTC), ZERO, date(2026, 10, 15)),
        # Wednesday before Thanksgiving after cutoff: Thu and Fri are no-production days.
        (datetime(2026, 11, 25, 19, 0, tzinfo=UTC), ZERO, date(2026, 11, 30)),
        # PST (UTC-8) in December: 17:59 UTC is 9:59 a.m.
        (datetime(2026, 12, 2, 17, 59, tzinfo=UTC), ZERO, date(2026, 12, 2)),
        (datetime(2026, 12, 2, 18, 0, tzinfo=UTC), ZERO, date(2026, 12, 3)),
        # Another zone's offset is honored.
        (
            datetime(2026, 10, 15, 13, 0, tzinfo=timezone(timedelta(hours=-4))),
            ZERO,
            date(2026, 10, 16),
        ),
    ],
)
def test_production_run_follows_the_pacific_cutoff_and_window(
    submitted: datetime, window: timedelta, run: date
) -> None:
    assert production_date(submitted, window) == run


def test_lob_2026_no_production_days_are_never_production_days() -> None:
    published = [
        (1, 1), (1, 19), (2, 16), (5, 25), (6, 19), (7, 3),
        (9, 7), (11, 26), (11, 27), (12, 24), (12, 25), (12, 31),
    ]  # fmt: skip
    for month, day in published:
        assert not is_production_day(date(2026, month, day)), (month, day)
    assert is_production_day(date(2026, 10, 15))
    assert not is_production_day(date(2026, 10, 17))


def test_daylight_saving_boundaries_and_invalid_input() -> None:
    assert pacific(datetime(2026, 3, 8, 9, 59, tzinfo=UTC)).hour == 1  # still PST
    assert pacific(datetime(2026, 3, 8, 10, 0, tzinfo=UTC)).hour == 3  # PDT begins
    assert pacific(datetime(2026, 11, 1, 8, 59, tzinfo=UTC)).hour == 1  # still PDT
    assert pacific(datetime(2026, 11, 1, 9, 0, tzinfo=UTC)).hour == 1  # PST, repeated hour
    with pytest.raises(ValueError, match="aware"):
        pacific(datetime(2026, 10, 15, 12, 0))  # deliberately naive
    with pytest.raises(ValueError, match="negative"):
        production_date(datetime(2026, 10, 15, 12, 0, tzinfo=UTC), timedelta(minutes=-1))
