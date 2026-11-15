from __future__ import annotations

from parkingbreaker.mail_config import MAIL_TIMING, UNSOURCED_TRANSIT, MailClass


def test_first_class_transit_is_sourced() -> None:
    """39 CFR § 121.1 (90 FR 10857) sources First-Class transit; UNSOURCED_TRANSIT is empty."""
    assert frozenset() == UNSOURCED_TRANSIT


def test_every_mail_class_has_timing() -> None:
    assert set(MAIL_TIMING) == set(MailClass)


def test_timings_are_positive_business_day_counts() -> None:
    for timing in MAIL_TIMING.values():
        assert timing.production_business_days > 0
        assert timing.transit_business_days > 0


def test_values_use_the_slow_end_of_the_2x_quoted_ranges() -> None:
    certified = MAIL_TIMING[MailClass.CERTIFIED]
    assert (certified.production_business_days, certified.transit_business_days) == (2, 7)
