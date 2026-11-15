from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from parkingbreaker.case_state import HardStop
from parkingbreaker.research.ladder import (
    NEVER_DEFAULTED,
    QUESTIONS_FOR,
    S3_STANDING_QUESTION,
    Ask,
    CountingChoice,
    CountingRule,
    CriticalFact,
    CustomerQuestion,
    DeadlineAmbiguity,
    Default,
    DefaultRule,
    Gap,
    GapKind,
    LadderStep,
    Recover,
    RecoveryAction,
    Stop,
    StopReason,
    earliest_deadline,
    earliest_trigger,
    hard_stop_for,
    match_address_choice,
    next_step,
    normalize_address,
    resolve_counting_rule,
)

F = CriticalFact
Q = CustomerQuestion


def spent(
    fact: CriticalFact,
    *,
    kind: GapKind = GapKind.UNCONFIRMED,
    asked: frozenset[CustomerQuestion] = frozenset(),
    official_address_count: int = 0,
    deadline_ambiguity: DeadlineAmbiguity = DeadlineAmbiguity.NO_DEADLINE,
) -> Gap:
    """A gap whose automatic recovery is exhausted."""
    return Gap(
        fact=fact,
        kind=kind,
        broadened_search_done=True,
        asked=asked,
        official_address_count=official_address_count,
        deadline_ambiguity=deadline_ambiguity,
    )


def all_asked(fact: CriticalFact) -> frozenset[CustomerQuestion]:
    return frozenset(QUESTIONS_FOR[fact])


# --- Step 1: automatic recovery -------------------------------------------------------


def test_fetch_retry_comes_first_while_retries_remain() -> None:
    gap = Gap(
        fact=F.MAIL_IN_ACCEPTED,
        kind=GapKind.STEP_FAILED,
        fetch_failed=True,
        fetch_retries_left=2,
        quote_guard_failed=True,
    )
    assert next_step(gap) == Recover(RecoveryAction.FETCH_RETRY)


def test_no_fetch_retry_without_a_failed_fetch() -> None:
    gap = Gap(fact=F.MAIL_IN_ACCEPTED, kind=GapKind.UNCONFIRMED, fetch_retries_left=3)
    assert next_step(gap) == Recover(RecoveryAction.BROADENED_SEARCH)


def test_re_extraction_once_after_quote_guard_failure() -> None:
    gap = Gap(fact=F.DEADLINE, kind=GapKind.STEP_FAILED, quote_guard_failed=True)
    assert next_step(gap) == Recover(RecoveryAction.RE_EXTRACT)
    done = Gap(
        fact=F.DEADLINE, kind=GapKind.STEP_FAILED, quote_guard_failed=True, re_extraction_done=True
    )
    assert next_step(done) == Recover(RecoveryAction.BROADENED_SEARCH)


def test_exhausted_retries_fall_through_to_broadened_search() -> None:
    gap = Gap(fact=F.REQUIRED_ITEMS, kind=GapKind.STEP_FAILED, fetch_failed=True)
    assert next_step(gap) == Recover(RecoveryAction.BROADENED_SEARCH)


def test_broadened_search_only_once() -> None:
    assert not isinstance(next_step(spent(F.REQUIRED_ITEMS)), Recover)


@pytest.mark.parametrize("fact", list(CriticalFact))
@pytest.mark.parametrize("kind", list(GapKind))
def test_every_gap_starts_with_recovery(fact: CriticalFact, kind: GapKind) -> None:
    assert isinstance(next_step(Gap(fact=fact, kind=kind)), Recover)


# --- Step 2: closed customer questions -------------------------------------------------


def test_deadline_questions_in_order_then_none_repeated() -> None:
    asked: frozenset[CustomerQuestion] = frozenset()
    seen = []
    while True:
        step = next_step(spent(F.DEADLINE, asked=asked))
        if not isinstance(step, Ask):
            break
        seen.append(step.question)
        asked = asked | {step.question}
    assert seen == [Q.NOTICE_ISSUE_DATE, Q.NOTICE_PRINTED_DEADLINE, Q.NOTICE_PRIOR_STEP]


def test_address_choice_asked_only_between_official_addresses() -> None:
    gap = spent(F.MAILING_ADDRESS, kind=GapKind.CONFLICTING, official_address_count=2)
    assert next_step(gap) == Ask(Q.NOTICE_ADDRESS_CHOICE)


@pytest.mark.parametrize("count", [0, 1])
def test_no_address_choice_without_two_official_addresses(count: int) -> None:
    gap = spent(F.MAILING_ADDRESS, official_address_count=count)
    assert next_step(gap) == Stop(StopReason.MAILING_ADDRESS_NOT_CONFIRMED, HardStop.S2)


def test_address_question_not_repeated() -> None:
    gap = spent(
        F.MAILING_ADDRESS,
        official_address_count=3,
        asked=frozenset({Q.NOTICE_ADDRESS_CHOICE}),
    )
    assert next_step(gap) == Stop(StopReason.MAILING_ADDRESS_NOT_CONFIRMED, HardStop.S2)


@pytest.mark.parametrize("fact", [F.MAIL_IN_ACCEPTED, F.COUNTING_RULE, F.REQUIRED_ITEMS])
def test_customer_cannot_supply(fact: CriticalFact) -> None:
    assert not isinstance(next_step(spent(fact)), Ask)


def test_questions_come_from_the_closed_list() -> None:
    for questions in QUESTIONS_FOR.values():
        assert set(questions) <= set(CustomerQuestion)
    assert set(QUESTIONS_FOR) == set(CriticalFact)


def test_standing_question_is_reserved_and_existing_gap_questions_are_unchanged() -> None:
    assert CustomerQuestion("VEHICLE_RELATIONSHIP") is Q.VEHICLE_RELATIONSHIP
    assert S3_STANDING_QUESTION is Q.VEHICLE_RELATIONSHIP
    assert QUESTIONS_FOR == {
        F.MAIL_IN_ACCEPTED: (),
        F.MAILING_ADDRESS: (Q.NOTICE_ADDRESS_CHOICE,),
        F.DEADLINE: (
            Q.NOTICE_ISSUE_DATE,
            Q.NOTICE_PRINTED_DEADLINE,
            Q.NOTICE_PRIOR_STEP,
        ),
        F.COUNTING_RULE: (),
        F.REQUIRED_ITEMS: (),
    }


# --- Step 3: conservative defaults -----------------------------------------------------


def test_counting_rule_defaults_to_receipt() -> None:
    assert next_step(spent(F.COUNTING_RULE)) == Default(DefaultRule.COUNT_BY_RECEIPT)
    conflicting = spent(F.COUNTING_RULE, kind=GapKind.CONFLICTING)
    assert next_step(conflicting) == Default(DefaultRule.COUNT_BY_RECEIPT)


def test_deadline_conflict_defaults_to_earlier_after_questions() -> None:
    gap = spent(F.DEADLINE, deadline_ambiguity=DeadlineAmbiguity.OFFICIAL_VS_NOTICE)
    assert next_step(gap) == Ask(Q.NOTICE_ISSUE_DATE)
    done = spent(
        F.DEADLINE,
        deadline_ambiguity=DeadlineAmbiguity.OFFICIAL_VS_NOTICE,
        asked=all_asked(F.DEADLINE),
    )
    assert next_step(done) == Default(DefaultRule.EARLIER_DEADLINE)


def test_uncertain_trigger_defaults_to_earlier_candidate() -> None:
    gap = spent(
        F.DEADLINE,
        deadline_ambiguity=DeadlineAmbiguity.UNCERTAIN_TRIGGER,
        asked=all_asked(F.DEADLINE),
    )
    assert next_step(gap) == Default(DefaultRule.EARLIER_TRIGGER)


def test_no_deadline_at_all_stops() -> None:
    gap = spent(F.DEADLINE, asked=all_asked(F.DEADLINE))
    assert next_step(gap) == Stop(StopReason.DEADLINE_NOT_ESTABLISHED, HardStop.S4)


@pytest.mark.parametrize(
    ("fact", "reason"),
    [
        (F.MAIL_IN_ACCEPTED, StopReason.MAIL_IN_NOT_CONFIRMED),
        (F.MAILING_ADDRESS, StopReason.MAILING_ADDRESS_NOT_CONFIRMED),
        (F.REQUIRED_ITEMS, StopReason.REQUIRED_ITEMS_NOT_CONFIRMED),
    ],
)
@pytest.mark.parametrize("ambiguity", list(DeadlineAmbiguity))
def test_never_defaulted_facts_stop(
    fact: CriticalFact, reason: StopReason, ambiguity: DeadlineAmbiguity
) -> None:
    gap = spent(fact, asked=frozenset(CustomerQuestion), deadline_ambiguity=ambiguity)
    assert next_step(gap) == Stop(reason, hard_stop_for(fact))


def test_never_defaulted_set() -> None:
    assert {F.MAIL_IN_ACCEPTED, F.MAILING_ADDRESS, F.REQUIRED_ITEMS} == NEVER_DEFAULTED


gaps = st.builds(
    Gap,
    fact=st.sampled_from(CriticalFact),
    kind=st.sampled_from(GapKind),
    fetch_failed=st.booleans(),
    fetch_retries_left=st.integers(min_value=0, max_value=5),
    quote_guard_failed=st.booleans(),
    re_extraction_done=st.booleans(),
    broadened_search_done=st.booleans(),
    asked=st.frozensets(st.sampled_from(CustomerQuestion)),
    official_address_count=st.integers(min_value=0, max_value=4),
    deadline_ambiguity=st.sampled_from(DeadlineAmbiguity),
)


@given(gaps)
def test_ladder_invariants(gap: Gap) -> None:
    step: LadderStep = next_step(gap)
    if isinstance(step, Default):
        assert gap.fact not in NEVER_DEFAULTED
    if isinstance(step, Ask):
        assert step.question in QUESTIONS_FOR[gap.fact]
        assert step.question not in gap.asked
    if isinstance(step, Stop):
        assert gap.fact is not F.COUNTING_RULE
    if gap.broadened_search_done is False:
        assert isinstance(step, Recover)


@given(gaps)
def test_ladder_always_terminates(gap: Gap) -> None:
    """Applying each step's bookkeeping reaches Default or Stop in bounded steps."""
    for _ in range(20):
        step = next_step(gap)
        if isinstance(step, (Default, Stop)):
            return
        if isinstance(step, Recover):
            retry = step.action is RecoveryAction.FETCH_RETRY
            gap = replace(
                gap,
                fetch_retries_left=gap.fetch_retries_left - 1 if retry else gap.fetch_retries_left,
                re_extraction_done=gap.re_extraction_done
                or step.action is RecoveryAction.RE_EXTRACT,
                broadened_search_done=gap.broadened_search_done
                or step.action is RecoveryAction.BROADENED_SEARCH,
            )
        else:
            gap = replace(gap, asked=gap.asked | {step.question})
    pytest.fail("ladder did not terminate")


def test_gap_rejects_negative_counts() -> None:
    with pytest.raises(ValueError, match="fetch_retries_left"):
        Gap(fact=F.DEADLINE, kind=GapKind.UNCONFIRMED, fetch_retries_left=-1)
    with pytest.raises(ValueError, match="official_address_count"):
        Gap(fact=F.DEADLINE, kind=GapKind.UNCONFIRMED, official_address_count=-1)


# --- Timing defaults -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("found", "expected"),
    [
        ([], CountingChoice(CountingRule.RECEIPT, disclose=True)),
        ([CountingRule.POSTMARK], CountingChoice(CountingRule.POSTMARK, disclose=False)),
        ([CountingRule.RECEIPT], CountingChoice(CountingRule.RECEIPT, disclose=False)),
        (
            [CountingRule.POSTMARK, CountingRule.POSTMARK],
            CountingChoice(CountingRule.POSTMARK, disclose=False),
        ),
        (
            [CountingRule.POSTMARK, CountingRule.RECEIPT],
            CountingChoice(CountingRule.RECEIPT, disclose=True),
        ),
    ],
)
def test_resolve_counting_rule(found: list[CountingRule], expected: CountingChoice) -> None:
    assert resolve_counting_rule(found) == expected


def test_earliest_deadline_picks_earlier_and_discloses() -> None:
    official, notice = date(2026, 11, 3), date(2026, 10, 30)
    choice = earliest_deadline(official, notice)
    assert choice is not None
    assert choice.deadline == notice
    assert (choice.official, choice.notice) == (official, notice)
    assert choice.disclose
    flipped = earliest_deadline(notice, official)
    assert flipped is not None
    assert flipped.deadline == notice
    assert flipped.disclose


def test_earliest_deadline_agreeing_or_single_source_not_disclosed() -> None:
    d = date(2026, 11, 3)
    same = earliest_deadline(d, d)
    assert same is not None
    assert (same.deadline, same.disclose) == (d, False)
    only_official = earliest_deadline(d, None)
    assert only_official is not None
    assert (only_official.deadline, only_official.disclose) == (d, False)
    only_notice = earliest_deadline(None, d)
    assert only_notice is not None
    assert (only_notice.deadline, only_notice.disclose) == (d, False)
    assert earliest_deadline(None, None) is None


@given(st.dates(), st.dates())
def test_earliest_deadline_never_later_than_either(a: date, b: date) -> None:
    choice = earliest_deadline(a, b)
    assert choice is not None
    assert choice.deadline <= a
    assert choice.deadline <= b
    assert choice.disclose == (a != b)


def test_earliest_trigger() -> None:
    a, b = date(2026, 10, 1), date(2026, 10, 4)
    assert earliest_trigger([b, a]).trigger == a
    assert earliest_trigger([b, a]).disclose
    assert not earliest_trigger([a, a]).disclose
    assert not earliest_trigger([a]).disclose
    with pytest.raises(ValueError, match="trigger"):
        earliest_trigger([])


# --- Address choice --------------------------------------------------------------------

OFFICIAL = (
    "Parking Citation Assistance Center, P.O. Box 30247, Los Angeles, CA 90030",
    "100 North Main Street, Suite 200, Springfield, IL 62701",
)


@pytest.mark.parametrize(
    "chosen",
    [
        "100 North Main Street, Suite 200, Springfield, IL 62701",
        "100 N. Main St., Ste 200, Springfield IL 62701",
        "  100 n main st ste 200 springfield il 62701 ",
    ],
)
def test_address_choice_matches_official_wording(chosen: str) -> None:
    assert match_address_choice(chosen, OFFICIAL) == OFFICIAL[1]


def test_po_box_spellings_match() -> None:
    chosen = "parking citation assistance center post office box 30247 los angeles ca 90030"
    assert match_address_choice(chosen, OFFICIAL) == OFFICIAL[0]
    spaced = "Parking Citation Assistance Center, P. O. Box 30247, Los Angeles, CA 90030"
    assert match_address_choice(spaced, OFFICIAL) == OFFICIAL[0]


@pytest.mark.parametrize(
    "chosen",
    [
        "100 North Main Street, Suite 201, Springfield, IL 62701",
        "1 Elm St, Springfield, IL 62701",
        "",
        "   ",
    ],
)
def test_address_choice_not_official_stops(chosen: str) -> None:
    assert match_address_choice(chosen, OFFICIAL) == Stop(
        StopReason.ADDRESS_CHOICE_NOT_OFFICIAL, HardStop.S2
    )


def test_address_choice_with_no_official_addresses_stops() -> None:
    assert match_address_choice(OFFICIAL[0], ()) == Stop(
        StopReason.ADDRESS_CHOICE_NOT_OFFICIAL, HardStop.S2
    )


def test_normalize_address() -> None:
    assert normalize_address("12 West Avenue, Floor 3") == "12 w ave fl 3"
    assert normalize_address("") == ""


@pytest.mark.parametrize("seconds", [0, 300, 360, 361])
def test_optional_planner_reserves_review_time_and_obeys_spent_ladder(seconds: int) -> None:
    from datetime import timedelta

    from parkingbreaker.research.ladder import optional_recovery

    gap = Gap(F.REQUIRED_ITEMS, GapKind.UNCONFIRMED)
    planned = optional_recovery(gap, timedelta(seconds=seconds))
    assert planned == (Recover(RecoveryAction.BROADENED_SEARCH) if seconds > 360 else None)
    assert optional_recovery(replace(gap, broadened_search_done=True), timedelta(hours=1)) is None
