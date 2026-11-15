"""T21: explicit business/working-day periods and same-day time cutoffs.

Business days use the federal holiday schedule and are aimed one business day EARLIER than the
computed date (local holidays are not modelled). A stated time-of-day cutoff means arrival by
the end of the previous business day. Anything ambiguous stays unsupported.
"""

from __future__ import annotations

from datetime import date

import pytest

from parkingbreaker.holidays import previous_business_day
from parkingbreaker.json_types import JsonValue
from parkingbreaker.mail_config import MailClass
from parkingbreaker.mail_timing import (
    ClockStart,
    DeadlinePeriod,
    DeadlineRule,
    PeriodUnit,
    TimingInput,
    compute,
    deadline_date,
    deadline_for_period,
    latest_safe_send_date,
    parse_deadline_period,
)
from parkingbreaker.research.findings import (
    Finding,
    FindingStatus,
    apply_quote_guard,
    apply_value_bounds,
    validate_value,
)
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.resolution import (
    RecoveryProgress,
    ResearchAnswers,
    ResearchResolution,
    resolve_research,
)
from parkingbreaker.research.sources import SourceRecord
from parkingbreaker.research.support import value_supported_by_quote
from samples import source
from test_research_findings import CONTEST_TEXT, SOURCES, full_findings
from test_research_run import TICKET

K = FactKey
ISSUE = date(2026, 10, 1)  # Thursday


def biz(days: int, *, cutoff: bool = False) -> dict[str, JsonValue]:
    return {"days": days, "unit": "BUSINESS", "time_cutoff": cutoff}


def cal(days: int, *, cutoff: bool = False) -> dict[str, JsonValue]:
    return {"days": days, "unit": "CALENDAR", "time_cutoff": cutoff}


# --- mail_timing ------------------------------------------------------------------------


def test_previous_business_day_skips_weekend_and_holiday() -> None:
    assert previous_business_day(date(2026, 10, 19)) == date(2026, 10, 16)  # Mon -> Fri
    assert previous_business_day(date(2026, 10, 13)) == date(2026, 10, 9)  # Columbus Day Mon
    assert previous_business_day(date(2026, 10, 14)) == date(2026, 10, 13)


def test_thirty_business_days_across_holidays_has_one_business_day_buffer() -> None:
    # Business days after Thu 2026-10-01 skip Columbus Day (10-12), Veterans Day (11-11)
    # and weekends: the 30th is Mon 2026-11-16. One business day earlier is Fri 2026-11-13.
    got = deadline_date(
        ISSUE, 30, ClockStart.ISSUE_DATE, period_unit=PeriodUnit.BUSINESS, notice_date=None
    )
    assert got == date(2026, 11, 13)


def test_unqualified_default_stays_calendar() -> None:
    assert deadline_date(ISSUE, 30, ClockStart.ISSUE_DATE) == date(2026, 10, 31)
    assert deadline_for_period(ISSUE, DeadlinePeriod(30), ClockStart.ISSUE_DATE) == date(
        2026, 10, 31
    )


def test_notice_date_clock_with_business_days() -> None:
    # Notice Tue 2026-10-06; 5 business days later is Tue 2026-10-13 (Mon 10-12 is Columbus
    # Day: 7,8,9,13 -> 4; so 14 is the 5th). Buffer -> Tue 2026-10-13.
    got = deadline_date(
        ISSUE,
        5,
        ClockStart.NOTICE_DATE,
        date(2026, 10, 6),
        PeriodUnit.BUSINESS,
    )
    assert got == date(2026, 10, 13)


def test_time_cutoff_means_end_of_previous_business_day() -> None:
    # 21 days after Thu 2026-10-22 is Thu 11-12; Wed 11-11 is Veterans Day, so Tue 11-10.
    assert deadline_date(date(2026, 10, 22), 21, ClockStart.ISSUE_DATE, time_cutoff=True) == date(
        2026, 11, 10
    )
    # Monday deadline -> previous Friday.
    assert deadline_date(date(2026, 10, 12), 21, ClockStart.ISSUE_DATE, time_cutoff=True) == date(
        2026, 10, 30
    )


def test_business_days_with_time_cutoff_stacks_both_conservative_steps() -> None:
    # Raw 30th business day is Mon 11-16; buffer -> Fri 11-13; cutoff -> Thu 11-12.
    got = deadline_date(ISSUE, 30, ClockStart.ISSUE_DATE, None, PeriodUnit.BUSINESS, True)
    assert got == date(2026, 11, 12)


def test_latest_safe_send_date_respects_business_day_deadline() -> None:
    for unit_days in (biz(30), cal(30, cutoff=True)):
        period = parse_deadline_period(unit_days)
        assert period is not None
        deadline = deadline_for_period(ISSUE, period, ClockStart.ISSUE_DATE)
        inp = TimingInput(
            issue_date=ISSUE,
            deadline_days=period.days,
            clock_start=ClockStart.ISSUE_DATE,
            rule=DeadlineRule.RECEIPT,
            mail_class=MailClass.CERTIFIED,
            send_date=date(2026, 10, 20),
            period_unit=period.unit,
            time_cutoff=period.time_cutoff,
        )
        result = compute(inp)
        assert result.deadline == deadline
        assert result.latest_safe_send_date == latest_safe_send_date(
            deadline, DeadlineRule.RECEIPT, MailClass.CERTIFIED
        )
        assert result.arrival_date <= deadline if result.reachable else True


def test_business_period_is_earlier_than_calendar_equivalent_in_compute() -> None:
    base = TimingInput(
        issue_date=ISSUE,
        deadline_days=30,
        clock_start=ClockStart.ISSUE_DATE,
        rule=DeadlineRule.POSTMARK,
        mail_class=MailClass.CERTIFIED,
        send_date=date(2026, 10, 5),
    )
    assert compute(base).deadline == date(2026, 10, 31)
    business = compute(TimingInput(**{**base.__dict__, "period_unit": PeriodUnit.BUSINESS}))
    assert business.deadline == date(2026, 11, 13)


def test_parse_deadline_period() -> None:
    assert parse_deadline_period(21) == DeadlinePeriod(21, PeriodUnit.CALENDAR, False)
    assert parse_deadline_period(biz(30, cutoff=True)) == DeadlinePeriod(
        30, PeriodUnit.BUSINESS, True
    )
    bad_values: tuple[JsonValue, ...] = (
        True,
        "21",
        None,
        {"days": 30},
        {"days": 30, "unit": "business", "time_cutoff": False},
        {"days": 30, "unit": "BUSINESS", "time_cutoff": "no"},
        {"days": True, "unit": "BUSINESS", "time_cutoff": False},
        {"days": 30, "unit": "BUSINESS", "time_cutoff": False, "extra": 1},
    )
    for bad in bad_values:
        assert parse_deadline_period(bad) is None


# --- findings value shape ---------------------------------------------------------------


def test_validate_value_accepts_period_objects_and_integers() -> None:
    assert validate_value(K.DEADLINE_DAYS, 21) is None
    assert validate_value(K.DEADLINE_DAYS, biz(30)) is None
    assert validate_value(K.DEADLINE_DAYS, cal(30, cutoff=True)) is None


@pytest.mark.parametrize(
    "bad",
    [
        biz(0),
        biz(366),
        {"days": 30, "unit": "FORTNIGHT", "time_cutoff": False},
        {"days": 30, "unit": "BUSINESS"},
        {"days": 30.0, "unit": "BUSINESS", "time_cutoff": False},
        True,
        "30",
    ],
)
def test_validate_value_rejects_malformed_periods(bad: JsonValue) -> None:
    assert validate_value(K.DEADLINE_DAYS, bad) is not None


# --- support: explicit unit evidence ----------------------------------------------------


@pytest.mark.parametrize("unit_word", ["business", "working"])
def test_explicit_business_or_working_days_are_supported_as_business(unit_word: str) -> None:
    quote = f"Contests must be received within 30 {unit_word} days of the citation issue date."
    assert value_supported_by_quote(K.DEADLINE_DAYS, biz(30), quote)
    # The same quote never supports a calendar reading or a plain integer.
    assert not value_supported_by_quote(K.DEADLINE_DAYS, cal(30), quote)
    assert not value_supported_by_quote(K.DEADLINE_DAYS, 30, quote)


def test_unqualified_days_never_support_business_unit() -> None:
    quote = "Contests must be received within 30 days of the citation issue date."
    assert value_supported_by_quote(K.DEADLINE_DAYS, 30, quote)
    assert value_supported_by_quote(K.DEADLINE_DAYS, cal(30), quote)
    assert not value_supported_by_quote(K.DEADLINE_DAYS, biz(30), quote)


def test_wrong_number_is_not_supported_for_business_period() -> None:
    quote = "Contests must be received within 30 business days of the citation issue date."
    assert not value_supported_by_quote(K.DEADLINE_DAYS, biz(31), quote)


@pytest.mark.parametrize(
    "quote",
    [
        # Mixed units in one statement.
        "Contest within 10 business days or 30 calendar days of the citation issue date.",
        "Contest within 30 business days, which is about 45 calendar days after issue.",
        "Contest within 30 business days or 60 days of the citation issue date.",
        # Purpose is not a contest.
        "Late fees begin 30 business days after the citation issue date.",
        # Counting wording that is not an explicit unit.
        "Contest within 30 days, excluding weekends and holidays.",
        "Contest within 30 days of the citation issue date, not counting holidays.",
    ],
)
def test_ambiguous_business_wording_stays_unsupported(quote: str) -> None:
    for value in (biz(30), biz(10), biz(45), biz(60), cal(30), cal(10), cal(45), cal(60)):
        assert not value_supported_by_quote(K.DEADLINE_DAYS, value, quote)


def test_business_days_with_weekend_and_holiday_explanation_is_supported() -> None:
    quote = (
        "Contest within 30 business days (excluding weekends and holidays) of the "
        "citation issue date."
    )
    assert value_supported_by_quote(K.DEADLINE_DAYS, biz(30), quote)
    assert not value_supported_by_quote(K.DEADLINE_DAYS, cal(30), quote)


def test_business_unit_is_checked_per_clause_not_across_the_quote() -> None:
    quote = "Contest within 30 business days of issue. Payment plans last 60 days."
    assert value_supported_by_quote(K.DEADLINE_DAYS, biz(30), quote)


def test_business_days_do_not_accept_time_cutoff_claim_without_cutoff_text() -> None:
    quote = "Contests must be received within 30 business days of the citation issue date."
    assert not value_supported_by_quote(K.DEADLINE_DAYS, biz(30, cutoff=True), quote)


# --- support: explicit time cutoff ------------------------------------------------------

CUTOFF = "Appeals must be received before 5:00 PM on the 30th day after the citation issue date."


def test_explicit_cutoff_with_a_day_is_supported_only_with_cutoff_flag() -> None:
    assert value_supported_by_quote(K.DEADLINE_DAYS, cal(30, cutoff=True), CUTOFF)
    # Date-only readings of a timed quote remain vetoed, exactly as before.
    assert not value_supported_by_quote(K.DEADLINE_DAYS, 30, CUTOFF)
    assert not value_supported_by_quote(K.DEADLINE_DAYS, cal(30), CUTOFF)
    assert not value_supported_by_quote(K.DEADLINE_DAYS, biz(30, cutoff=True), CUTOFF)


def test_cutoff_with_business_days_and_zone_is_supported() -> None:
    quote = "Appeals must be filed by 5 p.m. Pacific time on the 30th business day after issue."
    assert value_supported_by_quote(K.DEADLINE_DAYS, biz(30, cutoff=True), quote)


@pytest.mark.parametrize(
    "quote",
    [
        "Appeals must be filed by 5:00 PM.",  # cutoff without a day
        "Appeals must be filed by midnight on the 30th day after issue.",  # not a stated cutoff
        "Appeals within 30 days after issue. Pay fines by 5:00 PM.",  # time elsewhere
        "Appeals within 30 days after issue; the office closes at 5 PM and opens at 8 AM.",
        "Appeals must be filed within 30 days, Pacific time.",  # zone without a cutoff
        "Fines must be paid before 5:00 PM on the 30th day after the citation issue date.",
    ],
)
def test_ambiguous_cutoff_wording_stays_unsupported(quote: str) -> None:
    for value in (cal(30, cutoff=True), biz(30, cutoff=True), cal(30), 30):
        assert not value_supported_by_quote(K.DEADLINE_DAYS, value, quote)


def test_other_deadline_keys_keep_their_time_veto() -> None:
    assert not value_supported_by_quote(K.DEADLINE_CLOCK_START, "ISSUE_DATE", CUTOFF)
    assert not value_supported_by_quote(K.DEADLINE_RULE, "RECEIPT", CUTOFF)


# --- end to end through the resolution path ---------------------------------------------

CONTEST_BIZ = source(
    CONTEST_TEXT
    + "\n\nContests must be received within 30 business days of the citation issue date."
)
CONTEST_BAD = source(CONTEST_TEXT + "\n\nContest within 30 days, excluding weekends and holidays.")


def _resolve_with_days(days: JsonValue, quote: str, src: SourceRecord) -> ResearchResolution:
    findings = [
        Finding(K.DEADLINE_DAYS, days, FindingStatus.CONFIRMED, quote, (src.sha256,))
        if f.key is K.DEADLINE_DAYS
        else f
        for f in full_findings()
    ]
    sources = [*SOURCES, src]
    guarded_findings = apply_value_bounds(apply_quote_guard(findings, sources))
    return resolve_research(
        TICKET,
        guarded_findings,
        guarded_findings,
        sources,
        RecoveryProgress(True, True),
        ResearchAnswers(),
    )


def test_supported_business_day_quote_produces_a_deadline_through_resolution() -> None:
    quote = "within 30 business days of the citation issue date"
    resolution = _resolve_with_days(biz(30), quote, CONTEST_BIZ)
    assert resolution.deadline is not None
    # 30th business day after Thu 2026-10-01 is Mon 11-16; one business day earlier.
    assert resolution.deadline.deadline == date(2026, 11, 13)
    assert resolution.official_deadline_candidates == (date(2026, 11, 13),)


def test_calendar_integer_deadline_through_resolution_is_unchanged() -> None:
    resolution = _resolve_with_days(
        21, "within 21 calendar days of the citation issue date", CONTEST_BIZ
    )
    assert resolution.deadline is not None
    assert resolution.deadline.deadline == date(2026, 10, 22)


def test_ambiguous_business_wording_still_yields_no_deadline() -> None:
    quote = "Contest within 30 days, excluding weekends and holidays."
    resolution = _resolve_with_days(biz(30), quote, CONTEST_BAD)
    assert resolution.deadline is None
    assert resolution.official_deadline_candidates == ()
    assert not resolution.ready
    assert any(FactKey.DEADLINE_DAYS in p.keys for p in resolution.pending)
