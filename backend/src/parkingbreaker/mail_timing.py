"""Appeal deadline and mail-timing calculator. Pure: callers pass every date in.

Definitions:

- Clock start: the citation issue date, or a later notice date when the agency's clock runs
  from the notice.
- Date values are civil dates supplied by the caller. This pure module does not select or
  convert a time zone; a same-day time-of-day cutoff is modelled only by the conservative
  rule below.
- An unqualified "day" is a calendar day (PeriodUnit.CALENDAR). PeriodUnit.BUSINESS is used
  only when the verbatim official quote explicitly says "business days" or "working days"
  (enforced upstream in research.support); a unit is never inferred from its absence.
- Deadline date, CALENDAR: clock start + deadline_days calendar days. It is not rolled
  forward when it lands on a weekend or holiday; agencies differ on that, and not rolling is
  the safe side.
- Deadline date, BUSINESS: clock start + deadline_days business days (federal holiday
  schedule, holidays.add_business_days), then ONE BUSINESS DAY EARLIER. Local and agency
  holidays are not modelled, and an unmodelled closure would put our computed date AFTER the
  agency's real deadline; the one-business-day safety buffer keeps us on the early side.
- Time-of-day cutoff (time_cutoff, only when the quote explicitly states the cutoff, for
  example "before 5:00 PM on the 30th day"): the item must arrive by the END OF THE
  PREVIOUS BUSINESS DAY, so the deadline moves to the closest business day strictly before
  the date computed above. This is conservative and independent of the time zone.
- Send date: the day the letter is handed to Lob.
- Mailing (postmark) date: send date + Lob production business days.
- Arrival date: mailing date + USPS transit business days.
- POSTMARK rule: the mailing date must be on or before the deadline.
  RECEIPT rule: the arrival date must be on or before the deadline.
- Latest safe send date: the last calendar day whose send still satisfies the rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from parkingbreaker.holidays import add_business_days, previous_business_day
from parkingbreaker.json_types import JsonValue
from parkingbreaker.mail_config import MAIL_TIMING, MailClass


class ClockStart(StrEnum):
    ISSUE_DATE = "issue_date"
    NOTICE_DATE = "notice_date"


class DeadlineRule(StrEnum):
    POSTMARK = "postmark"
    RECEIPT = "receipt"


class PeriodUnit(StrEnum):
    CALENDAR = "calendar"
    BUSINESS = "business"


@dataclass(frozen=True)
class DeadlinePeriod:
    days: int
    unit: PeriodUnit = PeriodUnit.CALENDAR
    time_cutoff: bool = False


def parse_deadline_period(value: JsonValue) -> DeadlinePeriod | None:
    """A plain integer is a calendar-day period. An object must state every field exactly:
    {"days": int, "unit": "CALENDAR" | "BUSINESS", "time_cutoff": bool}. Anything else is None."""
    if isinstance(value, int) and not isinstance(value, bool):
        return DeadlinePeriod(value)
    if not isinstance(value, dict) or set(value) != {"days", "unit", "time_cutoff"}:
        return None
    days, unit, cutoff = value["days"], value["unit"], value["time_cutoff"]
    if (
        isinstance(days, bool)
        or not isinstance(days, int)
        or not isinstance(unit, str)
        or unit not in PeriodUnit.__members__
        or not isinstance(cutoff, bool)
    ):
        return None
    return DeadlinePeriod(days, PeriodUnit[unit], cutoff)


@dataclass(frozen=True)
class TimingInput:
    issue_date: date
    deadline_days: int
    clock_start: ClockStart
    rule: DeadlineRule
    mail_class: MailClass
    send_date: date
    notice_date: date | None = None
    period_unit: PeriodUnit = PeriodUnit.CALENDAR
    time_cutoff: bool = False


@dataclass(frozen=True)
class TimingResult:
    deadline: date
    mailing_date: date
    arrival_date: date
    latest_safe_send_date: date
    reachable: bool


def clock_start_date(issue_date: date, clock_start: ClockStart, notice_date: date | None) -> date:
    if clock_start is ClockStart.ISSUE_DATE:
        return issue_date
    if notice_date is None:
        raise ValueError("clock starts at the notice date but no notice_date was given")
    if notice_date < issue_date:
        raise ValueError(f"notice_date {notice_date} is before issue_date {issue_date}")
    return notice_date


def deadline_date(
    issue_date: date,
    deadline_days: int,
    clock_start: ClockStart,
    notice_date: date | None = None,
    period_unit: PeriodUnit = PeriodUnit.CALENDAR,
    time_cutoff: bool = False,
) -> date:
    if deadline_days < 0:
        raise ValueError(f"deadline_days must be non-negative: {deadline_days}")
    start = clock_start_date(issue_date, clock_start, notice_date)
    if period_unit is PeriodUnit.BUSINESS:
        # One business day earlier than the federal-schedule count: local holidays are not
        # modelled and could make the agency's real deadline earlier than the computed one.
        deadline = previous_business_day(add_business_days(start, deadline_days))
    else:
        deadline = start + timedelta(days=deadline_days)
    if time_cutoff:
        # Arrival is required by the end of the previous business day.
        deadline = previous_business_day(deadline)
    return deadline


def deadline_for_period(
    issue_date: date,
    period: DeadlinePeriod,
    clock_start: ClockStart,
    notice_date: date | None = None,
) -> date:
    return deadline_date(
        issue_date, period.days, clock_start, notice_date, period.unit, period.time_cutoff
    )


def mailing_date(send_date: date, mail_class: MailClass) -> date:
    return add_business_days(send_date, MAIL_TIMING[mail_class].production_business_days)


def arrival_date(send_date: date, mail_class: MailClass) -> date:
    return add_business_days(
        mailing_date(send_date, mail_class), MAIL_TIMING[mail_class].transit_business_days
    )


def _decisive_date(send_date: date, rule: DeadlineRule, mail_class: MailClass) -> date:
    if rule is DeadlineRule.POSTMARK:
        return mailing_date(send_date, mail_class)
    return arrival_date(send_date, mail_class)


def latest_safe_send_date(deadline: date, rule: DeadlineRule, mail_class: MailClass) -> date:
    """The last day a send still meets `rule` for `deadline`.

    The decisive date never decreases as the send date moves later, so walking back from the
    deadline finds the answer. The walk is bounded by production + transit business days.
    """
    candidate = deadline
    while _decisive_date(candidate, rule, mail_class) > deadline:
        candidate -= timedelta(days=1)
    return candidate


def compute(inp: TimingInput) -> TimingResult:
    deadline = deadline_date(
        inp.issue_date,
        inp.deadline_days,
        inp.clock_start,
        inp.notice_date,
        inp.period_unit,
        inp.time_cutoff,
    )
    latest = latest_safe_send_date(deadline, inp.rule, inp.mail_class)
    return TimingResult(
        deadline=deadline,
        mailing_date=mailing_date(inp.send_date, inp.mail_class),
        arrival_date=arrival_date(inp.send_date, inp.mail_class),
        latest_safe_send_date=latest,
        reachable=inp.send_date <= latest,
    )
