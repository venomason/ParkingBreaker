from __future__ import annotations

from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from parkingbreaker.holidays import FIRST_SUPPORTED_YEAR
from parkingbreaker.mail_config import MailClass
from parkingbreaker.mail_timing import (
    ClockStart,
    DeadlineRule,
    TimingInput,
    arrival_date,
    compute,
    deadline_date,
    latest_safe_send_date,
    mailing_date,
)

# Start in March so walking back from a deadline never leaves the supported years.
DATES = st.dates(min_value=date(FIRST_SUPPORTED_YEAR, 3, 1), max_value=date(2199, 1, 1))
RULES = st.sampled_from(list(DeadlineRule))
CLASSES = st.sampled_from(list(MailClass))


def sf_input(send: date, rule: DeadlineRule = DeadlineRule.RECEIPT) -> TimingInput:
    return TimingInput(
        issue_date=date(2026, 10, 1),
        deadline_days=21,
        clock_start=ClockStart.ISSUE_DATE,
        rule=rule,
        mail_class=MailClass.CERTIFIED,
        send_date=send,
    )


def test_worked_example_receipt_rule_across_columbus_day() -> None:
    # Deadline Thu 2026-10-22. Receipt needs 2 + 7 = 9 business days, and Mon 2026-10-12
    # (Columbus Day) does not count, so the last safe send is Thu 2026-10-08.
    result = compute(sf_input(date(2026, 10, 8)))
    assert result.deadline == date(2026, 10, 22)
    assert result.latest_safe_send_date == date(2026, 10, 8)
    assert result.mailing_date == date(2026, 10, 13)
    assert result.arrival_date == date(2026, 10, 22)
    assert result.reachable
    assert not compute(sf_input(date(2026, 10, 9))).reachable


def test_worked_example_postmark_rule() -> None:
    result = compute(sf_input(date(2026, 10, 20), DeadlineRule.POSTMARK))
    assert result.latest_safe_send_date == date(2026, 10, 20)
    assert result.mailing_date == date(2026, 10, 22)
    assert result.reachable


def test_notice_date_clock() -> None:
    assert deadline_date(
        date(2026, 3, 1), 21, ClockStart.NOTICE_DATE, notice_date=date(2026, 3, 10)
    ) == date(2026, 3, 31)


def test_notice_clock_needs_a_valid_notice_date() -> None:
    with pytest.raises(ValueError, match="no notice_date"):
        deadline_date(date(2026, 3, 1), 21, ClockStart.NOTICE_DATE)
    with pytest.raises(ValueError, match="before issue_date"):
        deadline_date(date(2026, 3, 1), 21, ClockStart.NOTICE_DATE, date(2026, 2, 1))


def test_negative_deadline_days_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        deadline_date(date(2026, 3, 1), -1, ClockStart.ISSUE_DATE)


def test_deadline_is_not_rolled_forward_over_weekends() -> None:
    # 2026-03-01 + 20 days = Sat 2026-03-21; stays a Saturday.
    assert deadline_date(date(2026, 3, 1), 20, ClockStart.ISSUE_DATE) == date(2026, 3, 21)


def decisive(send: date, rule: DeadlineRule, mail_class: MailClass) -> date:
    if rule is DeadlineRule.POSTMARK:
        return mailing_date(send, mail_class)
    return arrival_date(send, mail_class)


@given(DATES, RULES, CLASSES)
def test_latest_safe_send_date_is_exactly_the_boundary(
    deadline: date, rule: DeadlineRule, mail_class: MailClass
) -> None:
    latest = latest_safe_send_date(deadline, rule, mail_class)
    assert latest <= deadline
    assert decisive(latest, rule, mail_class) <= deadline
    assert decisive(latest + timedelta(days=1), rule, mail_class) > deadline


@given(DATES, CLASSES)
def test_receipt_is_never_more_lenient_than_postmark(deadline: date, mail_class: MailClass) -> None:
    assert latest_safe_send_date(deadline, DeadlineRule.RECEIPT, mail_class) <= (
        latest_safe_send_date(deadline, DeadlineRule.POSTMARK, mail_class)
    )


@given(
    DATES,
    st.integers(min_value=0, max_value=90),
    st.integers(min_value=-30, max_value=120),
    RULES,
    CLASSES,
)
def test_reachable_means_the_letter_meets_the_rule(
    issue: date, days: int, send_offset: int, rule: DeadlineRule, mail_class: MailClass
) -> None:
    inp = TimingInput(
        issue_date=issue,
        deadline_days=days,
        clock_start=ClockStart.ISSUE_DATE,
        rule=rule,
        mail_class=mail_class,
        send_date=issue + timedelta(days=send_offset),
    )
    result = compute(inp)
    assert result.deadline == issue + timedelta(days=days)
    assert result.mailing_date <= result.arrival_date
    assert result.reachable == (decisive(inp.send_date, rule, mail_class) <= result.deadline)
