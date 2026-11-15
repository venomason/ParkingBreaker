from __future__ import annotations

from dataclasses import asdict

import pytest

from parkingbreaker.oplog.appeal_events import AppealOutcome, AppealTelemetry
from parkingbreaker.oplog.cohort_report import cohort_report

CHARGED = AppealTelemetry(AppealOutcome.CHARGED, None, 1, False, 20, 30)
RELEASED = AppealTelemetry(AppealOutcome.RELEASED_NO_CHARGE, None, 0, False, None, None)


def _rows(
    cohort: str, customers: int, telemetry: AppealTelemetry = CHARGED, start: int = 0
) -> list[tuple[str, str, AppealTelemetry]]:
    return [
        (cohort, f"customer-{cohort}-{index}", telemetry)
        for index in range(start, start + customers)
    ]


@pytest.mark.parametrize(("customers", "shown"), [(9, False), (10, True), (11, True)])
def test_cohorts_below_ten_distinct_customers_are_hidden(customers: int, shown: bool) -> None:
    report, _ = cohort_report(_rows("CA", customers) + _rows("NV", 3))
    assert [row.cohort for row in report] == (["CA"] if shown else [])
    repeat = [("CA", "customer-CA-0", CHARGED)] * 30
    assert cohort_report(repeat)[0] == ()


def test_repeated_exports_do_not_reveal_one_more_customer() -> None:
    first, state = cohort_report(_rows("CA", 10) + _rows("CA", 4, RELEASED, start=10))
    assert first[0].appeals == 14
    # One more released customer: the published row must not move.
    rows = _rows("CA", 10) + _rows("CA", 5, RELEASED, start=10)
    second, state = cohort_report(rows, previous=state)
    assert second == first
    # Nine more still do not; the tenth new customer releases a new batch.
    rows += _rows("CA", 8, start=15)
    assert cohort_report(rows, previous=state)[0] == first
    rows += _rows("CA", 1, start=23)
    third, _ = cohort_report(rows, previous=state)
    assert third[0].appeals == 24
    # Five released customers is below ten: that cell and its complement are withheld.
    assert dict(third[0].outcomes)[AppealOutcome.RELEASED_NO_CHARGE] is None
    assert dict(third[0].outcomes)[AppealOutcome.CHARGED] is None


def test_unset_or_invalid_minimum_suppresses_all_detail() -> None:
    rows = _rows("CA", 40)
    for minimum in (None, 0, 9, "10", 10.0, True):
        assert cohort_report(rows, minimum)[0] == ()
    assert len(cohort_report(rows, 10)[0]) == 1


def test_only_allowed_cohort_categories_are_exported() -> None:
    for cohort in ("7ABC123", "Pat-Customer", "pat_customer", "Main St", "", "GOVERNMENT"):
        with pytest.raises(ValueError, match="allowed category"):
            cohort_report([(cohort, "customer", CHARGED)])
    report, state = cohort_report(_rows("WA", 12))
    assert [row.cohort for row in report] == ["WA"]
    assert "customer-" not in repr([asdict(row) for row in report])
    assert state["WA"].customers  # the customer keys stay in restricted state only


def test_rare_cells_are_withheld_with_their_complement() -> None:
    refunded = AppealTelemetry(AppealOutcome.REFUNDED, None, 1, True, 20, 30)
    report, _ = cohort_report(_rows("CA", 10) + _rows("CA", 1, refunded, start=10))
    outcomes = dict(report[0].outcomes)
    assert report[0].appeals == 11
    assert outcomes[AppealOutcome.REFUNDED] is None
    assert outcomes[AppealOutcome.CHARGED] is None
    assert report[0].returned is None
    assert outcomes[AppealOutcome.OPEN] == 0


def test_carried_releases_follow_the_current_policy() -> None:
    _, state = cohort_report(_rows("CA", 10))
    assert cohort_report([], 20, previous=state)[0] == ()
    assert cohort_report([], previous=state, allowed_cohorts=frozenset({"WA"}))[0] == ()
    assert len(cohort_report([], previous=state)[0]) == 1
