"""The resolution ladder (docs/architecture-v6.md sections 5.2 and 5.3). Pure decision logic.

Every gap in a critical fact goes down the same four steps, in order, and stops at the
first that resolves it:

    1. Recover automatically: fetch retries, one broadened search, one re-extraction.
    2. Ask the customer, from the closed question list only.
    3. Apply a conservative, disclosed default (timing ambiguity only).
    4. Stop without charge, with a specific reason.

Mail-in acceptance, the mailing address and the required items are never defaulted. The
caller runs the step returned by next_step, records what happened on the Gap, and asks
again. Nothing here reads clocks, the network or storage.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from parkingbreaker.address_comparison import normalize_address as normalize_address
from parkingbreaker.case_state import HardStop


class CriticalFact(StrEnum):
    MAIL_IN_ACCEPTED = "MAIL_IN_ACCEPTED"
    MAILING_ADDRESS = "MAILING_ADDRESS"
    DEADLINE = "DEADLINE"
    COUNTING_RULE = "COUNTING_RULE"
    REQUIRED_ITEMS = "REQUIRED_ITEMS"


# Facts no default may ever fill: a gap here that recovery and questions cannot close stops.
NEVER_DEFAULTED: frozenset[CriticalFact] = frozenset(
    {CriticalFact.MAIL_IN_ACCEPTED, CriticalFact.MAILING_ADDRESS, CriticalFact.REQUIRED_ITEMS}
)


class CustomerQuestion(StrEnum):
    """The closed list. The system can ask the customer nothing else."""

    NOTICE_ISSUE_DATE = "NOTICE_ISSUE_DATE"
    NOTICE_PRINTED_DEADLINE = "NOTICE_PRINTED_DEADLINE"
    NOTICE_ADDRESS_CHOICE = "NOTICE_ADDRESS_CHOICE"
    NOTICE_VIOLATION = "NOTICE_VIOLATION"
    NOTICE_PRIOR_STEP = "NOTICE_PRIOR_STEP"
    NOTICE_ISSUING_AGENCY = "NOTICE_ISSUING_AGENCY"
    NOTICE_STAGE = "NOTICE_STAGE"
    VEHICLE_RELATIONSHIP = "VEHICLE_RELATIONSHIP"
    DRAFT_FACT = "DRAFT_FACT"
    PAID_FINE = "PAID_FINE"
    PAID_HEARING_DEPOSIT = "PAID_HEARING_DEPOSIT"
    ENTERED_PLAN = "ENTERED_PLAN"


# S3 standing question (v7 section 7), reserved for later research wiring. Ask only
# when an official rule restricts who may contest or requires proof of standing.
# It is not a gap-closer for any existing CriticalFact.
S3_STANDING_QUESTION = CustomerQuestion.VEHICLE_RELATIONSHIP


# Questions that can close a gap in each fact, in the order they are asked. The address
# choice is only offered when there are official addresses to choose between.
QUESTIONS_FOR: dict[CriticalFact, tuple[CustomerQuestion, ...]] = {
    CriticalFact.MAIL_IN_ACCEPTED: (),
    CriticalFact.MAILING_ADDRESS: (CustomerQuestion.NOTICE_ADDRESS_CHOICE,),
    CriticalFact.DEADLINE: (
        CustomerQuestion.NOTICE_ISSUE_DATE,
        CustomerQuestion.NOTICE_PRINTED_DEADLINE,
        CustomerQuestion.NOTICE_PRIOR_STEP,
    ),
    CriticalFact.COUNTING_RULE: (),
    CriticalFact.REQUIRED_ITEMS: (),
}


class GapKind(StrEnum):
    UNCONFIRMED = "UNCONFIRMED"
    CONFLICTING = "CONFLICTING"
    STEP_FAILED = "STEP_FAILED"


class DeadlineAmbiguity(StrEnum):
    """What is uncertain about a deadline gap; only the last two have a default."""

    NO_DEADLINE = "NO_DEADLINE"
    OFFICIAL_VS_NOTICE = "OFFICIAL_VS_NOTICE"
    UNCERTAIN_TRIGGER = "UNCERTAIN_TRIGGER"


class RecoveryAction(StrEnum):
    FETCH_RETRY = "FETCH_RETRY"
    RE_EXTRACT = "RE_EXTRACT"
    BROADENED_SEARCH = "BROADENED_SEARCH"


class DefaultRule(StrEnum):
    COUNT_BY_RECEIPT = "COUNT_BY_RECEIPT"
    EARLIER_DEADLINE = "EARLIER_DEADLINE"
    EARLIER_TRIGGER = "EARLIER_TRIGGER"


class StopReason(StrEnum):
    PRIOR_STEP_REQUIRED = "PRIOR_STEP_REQUIRED"
    MAIL_IN_NOT_CONFIRMED = "MAIL_IN_NOT_CONFIRMED"
    MAILING_ADDRESS_NOT_CONFIRMED = "MAILING_ADDRESS_NOT_CONFIRMED"
    ADDRESS_CHOICE_NOT_OFFICIAL = "ADDRESS_CHOICE_NOT_OFFICIAL"
    DEADLINE_NOT_ESTABLISHED = "DEADLINE_NOT_ESTABLISHED"
    REQUIRED_ITEMS_NOT_CONFIRMED = "REQUIRED_ITEMS_NOT_CONFIRMED"
    REQUIREMENT_UNFULFILLABLE = "REQUIREMENT_UNFULFILLABLE"


_STOP_FOR: dict[CriticalFact, StopReason] = {
    CriticalFact.MAIL_IN_ACCEPTED: StopReason.MAIL_IN_NOT_CONFIRMED,
    CriticalFact.MAILING_ADDRESS: StopReason.MAILING_ADDRESS_NOT_CONFIRMED,
    CriticalFact.DEADLINE: StopReason.DEADLINE_NOT_ESTABLISHED,
    CriticalFact.REQUIRED_ITEMS: StopReason.REQUIRED_ITEMS_NOT_CONFIRMED,
}


@dataclass(frozen=True)
class Gap:
    """One unresolved critical fact and what the ladder has already tried for it.

    fetch_failed / quote_guard_failed say which recoveries apply; the counters and flags
    say which are spent. asked holds every question already put to the customer for this
    fact (answered or not), so none is asked twice.
    """

    fact: CriticalFact
    kind: GapKind
    fetch_failed: bool = False
    fetch_retries_left: int = 0
    quote_guard_failed: bool = False
    re_extraction_done: bool = False
    broadened_search_done: bool = False
    asked: frozenset[CustomerQuestion] = frozenset()
    official_address_count: int = 0
    deadline_ambiguity: DeadlineAmbiguity = DeadlineAmbiguity.NO_DEADLINE

    def __post_init__(self) -> None:
        if self.fetch_retries_left < 0:
            raise ValueError("fetch_retries_left must be >= 0")
        if self.official_address_count < 0:
            raise ValueError("official_address_count must be >= 0")


@dataclass(frozen=True)
class Recover:
    action: RecoveryAction


@dataclass(frozen=True)
class Ask:
    question: CustomerQuestion


@dataclass(frozen=True)
class Default:
    rule: DefaultRule


@dataclass(frozen=True)
class Stop:
    reason: StopReason
    # None only for legacy stored ladder steps.
    hard_stop: HardStop | None = None
    official_quote: str | None = None


type LadderStep = Recover | Ask | Default | Stop


def hard_stop_for(fact: CriticalFact) -> HardStop:
    return {
        CriticalFact.MAIL_IN_ACCEPTED: HardStop.S1,
        CriticalFact.MAILING_ADDRESS: HardStop.S2,
        CriticalFact.REQUIRED_ITEMS: HardStop.S3,
        CriticalFact.DEADLINE: HardStop.S4,
        CriticalFact.COUNTING_RULE: HardStop.S4,
    }[fact]


def next_step(gap: Gap) -> LadderStep:
    """The first rung of the ladder that can still act on this gap."""
    recovery = _recovery(gap)
    if recovery is not None:
        return Recover(recovery)
    question = _question(gap)
    if question is not None:
        return Ask(question)
    rule = _default(gap)
    if rule is not None:
        return Default(rule)
    return Stop(
        _STOP_FOR[gap.fact],
        HardStop.S7 if gap.kind is GapKind.CONFLICTING else hard_stop_for(gap.fact),
    )


def _recovery(gap: Gap) -> RecoveryAction | None:
    if gap.fetch_failed and gap.fetch_retries_left > 0:
        return RecoveryAction.FETCH_RETRY
    if gap.quote_guard_failed and not gap.re_extraction_done:
        return RecoveryAction.RE_EXTRACT
    if not gap.broadened_search_done:
        return RecoveryAction.BROADENED_SEARCH
    return None


def optional_recovery(
    gap: Gap,
    remaining_time: timedelta,
    *,
    operation_time: timedelta = timedelta(seconds=60),
    review_reserve: timedelta = timedelta(minutes=5),
) -> Recover | None:
    """Use the unspent ladder allowance only when it leaves time for review/adoption."""
    if operation_time < timedelta(0) or review_reserve < timedelta(0):
        raise ValueError("recovery time allowances must be nonnegative")
    action = _recovery(gap)
    if action is None or remaining_time <= operation_time + review_reserve:
        return None
    return Recover(action)


def _question(gap: Gap) -> CustomerQuestion | None:
    if gap.fact is CriticalFact.MAILING_ADDRESS and gap.official_address_count < 2:
        return None
    for question in QUESTIONS_FOR[gap.fact]:
        if question not in gap.asked:
            return question
    return None


def _default(gap: Gap) -> DefaultRule | None:
    if gap.fact in NEVER_DEFAULTED:
        return None
    if gap.fact is CriticalFact.COUNTING_RULE:
        return DefaultRule.COUNT_BY_RECEIPT
    if gap.deadline_ambiguity is DeadlineAmbiguity.OFFICIAL_VS_NOTICE:
        return DefaultRule.EARLIER_DEADLINE
    if gap.deadline_ambiguity is DeadlineAmbiguity.UNCERTAIN_TRIGGER:
        return DefaultRule.EARLIER_TRIGGER
    return None


# --- Conservative timing defaults -------------------------------------------------------


class CountingRule(StrEnum):
    POSTMARK = "POSTMARK"
    RECEIPT = "RECEIPT"


@dataclass(frozen=True)
class CountingChoice:
    rule: CountingRule
    disclose: bool


def resolve_counting_rule(found: Collection[CountingRule]) -> CountingChoice:
    """One agreed rule is used as found; none or conflicting rules default to receipt."""
    distinct = set(found)
    if len(distinct) == 1:
        return CountingChoice(rule=next(iter(distinct)), disclose=False)
    return CountingChoice(rule=CountingRule.RECEIPT, disclose=True)


@dataclass(frozen=True)
class DeadlineChoice:
    """The deadline to work to, plus both inputs so the customer can be shown each."""

    deadline: date
    official: date | None
    notice: date | None
    disclose: bool


def earliest_deadline(official: date | None, notice: date | None) -> DeadlineChoice | None:
    """The earlier of the official-rule and notice deadlines; disclosed when they differ."""
    candidates = [d for d in (official, notice) if d is not None]
    if not candidates:
        return None
    return DeadlineChoice(
        deadline=min(candidates),
        official=official,
        notice=notice,
        disclose=official is not None and notice is not None and official != notice,
    )


@dataclass(frozen=True)
class TriggerChoice:
    trigger: date
    disclose: bool


def earliest_trigger(candidates: Sequence[date]) -> TriggerChoice:
    """The earliest candidate trigger date; disclosed when the candidates disagree."""
    if not candidates:
        raise ValueError("at least one trigger candidate is required")
    return TriggerChoice(trigger=min(candidates), disclose=len(set(candidates)) > 1)


# --- Address choice ------------------------------------------------------------------------


def match_address_choice(chosen: str, official: Sequence[str]) -> str | Stop:
    """The official address the customer's choice matches, in its official wording, or Stop.

    The customer can only choose between official addresses; anything else ends the case.
    """
    key = normalize_address(chosen)
    if key:
        for address in official:
            if normalize_address(address) == key:
                return address
    return Stop(StopReason.ADDRESS_CHOICE_NOT_OFFICIAL, HardStop.S2)
