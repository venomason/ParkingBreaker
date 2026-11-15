from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from parkingbreaker.case_state import (
    FIRST_ATTEMPT,
    MAILED_STATES,
    REMAIL_ATTEMPT,
    REMAIL_TRANSITIONS,
    TERMINAL_STATES,
    TRANSITIONS,
    WINDOWED_STATES,
    AuthorizationExpired,
    CaseState,
    ConfirmedNotSent,
    CustomerAnswered,
    CustomerApproved,
    CustomerNeeded,
    DeliveryProofStored,
    DestinationRefused,
    DraftReady,
    Effect,
    Event,
    HardStop,
    LetterNotSubmittable,
    NotesEdited,
    PaymentAuthorized,
    RemailRequested,
    ResearchComplete,
    ReturnClosed,
    ReturnConfirmed,
    StopEvent,
    StopFamily,
    Stopped,
    StopReason,
    SubmitConfirmed,
    SubmitRejected,
    SubmitRequested,
    TimingRefused,
    WindowExpired,
    allowed_events,
    next_attempt,
    stop_family,
    table_for,
    transition,
)
from parkingbreaker.state_table import IllegalTransition, reachable
from samples import LETTER_EVENTS

S = CaseState

SAMPLE_EVENTS = (*LETTER_EVENTS, TimingRefused())
STOP_EVENT_TYPES = (
    Stopped,
    WindowExpired,
    SubmitRejected,
    ConfirmedNotSent,
    AuthorizationExpired,
    TimingRefused,
    DestinationRefused,
    LetterNotSubmittable,
)
DEDICATED = frozenset(
    {
        StopReason.WINDOW_EXPIRED,
        StopReason.LOB_REJECTED,
        StopReason.NOT_SENT,
        StopReason.AUTHORIZATION_EXPIRED,
        StopReason.DESTINATION_NOT_CONFIRMED,
        StopReason.LETTER_NOT_SUBMITTABLE,
    }
)


def run(
    events: list[Event], start: CaseState = S.INTAKE, attempt: int = FIRST_ATTEMPT
) -> tuple[CaseState, list[Effect], int]:
    state = start
    effects: list[Effect] = []
    for event in events:
        t = transition(state, event, attempt)
        attempt = next_attempt(attempt, t)
        state = t.target
        effects.extend(t.effects)
    return state, effects, attempt


TO_IN_TRANSIT: list[Event] = [
    PaymentAuthorized(),
    ResearchComplete(),
    DraftReady(),
    CustomerApproved(),
    SubmitRequested(),
    SubmitConfirmed("ltr_1"),
]
HAPPY_PATH: list[Event] = [*TO_IN_TRANSIT, DeliveryProofStored("proof_1")]


def test_sample_events_cover_every_event_type() -> None:
    assert {type(e) for e in SAMPLE_EVENTS} == {ev for (_, ev) in TRANSITIONS}


def test_happy_path_is_linear_and_captures_once_on_entering_in_transit() -> None:
    state = S.INTAKE
    visited = [state]
    effects: list[Effect] = []
    for event in HAPPY_PATH:
        t = transition(state, event)
        state = t.target
        visited.append(state)
        effects.extend(t.effects)
    assert visited == [
        S.INTAKE,
        S.RESEARCHING,
        S.DRAFTING,
        S.AWAITING_ADOPTION,
        S.FROZEN,
        S.SUBMITTING,
        S.IN_TRANSIT,
        S.DELIVERED,
    ]
    assert effects == [Effect.SUBMIT_LETTER, Effect.CAPTURE_PAYMENT]


def test_stop_at_intake_charges_and_releases_nothing() -> None:
    state, effects, _ = run([Stopped(StopReason.UNSUPPORTED, "city not supported")])
    assert state is S.STOPPED_NO_CHARGE
    assert effects == []


def test_researching_and_needs_customer_loop() -> None:
    state, effects, _ = run([PaymentAuthorized(), CustomerNeeded("which plate?")])
    assert (state, effects) == (S.NEEDS_CUSTOMER, [])
    assert transition(state, CustomerAnswered()).target is S.RESEARCHING
    with pytest.raises(IllegalTransition):
        transition(state, ResearchComplete())


def test_customer_edits_notes_at_adoption_redrafts() -> None:
    assert transition(S.AWAITING_ADOPTION, NotesEdited()).target is S.DRAFTING


@pytest.mark.parametrize("state", sorted(WINDOWED_STATES))
def test_window_expiry_stops_every_state_before_submitting(state: CaseState) -> None:
    t = transition(state, WindowExpired())
    assert (t.target, t.effects) == (S.STOPPED_NO_CHARGE, (Effect.RELEASE_HOLD,))


def test_windowed_states_are_exactly_the_authorized_states_before_submitting() -> None:
    before_submit = reachable(TRANSITIONS, S.INTAKE, avoid=frozenset({S.SUBMITTING}))
    assert before_submit - {S.INTAKE, S.SUBMITTING, *TERMINAL_STATES} == WINDOWED_STATES
    assert WindowExpired not in allowed_events(S.INTAKE)
    for state in set(CaseState) - WINDOWED_STATES:
        assert WindowExpired not in allowed_events(state)


@pytest.mark.parametrize("state", sorted(WINDOWED_STATES))
@pytest.mark.parametrize(
    "reason",
    sorted(set(StopReason) - DEDICATED),
)
def test_case_specific_stops_release_the_hold(state: CaseState, reason: StopReason) -> None:
    t = transition(state, specific_stop(reason))
    assert (t.target, t.effects) == (S.STOPPED_NO_CHARGE, (Effect.RELEASE_HOLD,))


@pytest.mark.parametrize("reason", sorted(DEDICATED))
def test_dedicated_reasons_cannot_be_given_to_a_generic_stop(reason: StopReason) -> None:
    with pytest.raises(ValueError, match="own event"):
        Stopped(reason)


def test_submitting_stops_on_rejection_or_confirmed_not_sent() -> None:
    cases: list[tuple[SubmitRejected | ConfirmedNotSent, StopReason]] = [
        (SubmitRejected("bad"), StopReason.LOB_REJECTED),
        (ConfirmedNotSent("none"), StopReason.NOT_SENT),
    ]
    for event, reason in cases:
        t = transition(S.SUBMITTING, event)
        assert (t.target, t.effects) == (S.STOPPED_NO_CHARGE, (Effect.RELEASE_HOLD,))
        assert event.reason is reason
    assert Stopped not in allowed_events(S.SUBMITTING)
    assert WindowExpired not in allowed_events(S.SUBMITTING)


def test_every_stop_carries_a_reason_and_every_reason_is_reachable() -> None:
    stop_reasons = {specific_stop(r).reason for r in StopReason if r not in DEDICATED}
    stop_reasons |= {
        WindowExpired.reason,
        SubmitRejected.reason,
        ConfirmedNotSent.reason,
        AuthorizationExpired.reason,
        DestinationRefused.reason,
        LetterNotSubmittable.reason,
    }
    assert stop_reasons == set(StopReason)
    for table in (TRANSITIONS, REMAIL_TRANSITIONS):
        for (_, ev), rule in table.items():
            if rule.target is S.STOPPED_NO_CHARGE:
                assert ev in STOP_EVENT_TYPES


def test_stop_event_union_matches_stop_types() -> None:
    event: StopEvent = WindowExpired()
    assert isinstance(event, STOP_EVENT_TYPES)


def test_return_then_one_remail_then_delivery() -> None:
    state, effects, attempt = run([*TO_IN_TRANSIT, ReturnConfirmed("refused")])
    assert (state, attempt) == (S.RETURNED, FIRST_ATTEMPT)
    state, more, attempt = run(
        [RemailRequested(), SubmitConfirmed("ltr_2"), DeliveryProofStored("p2")],
        start=state,
        attempt=attempt,
    )
    assert (state, attempt) == (S.DELIVERED, REMAIL_ATTEMPT)
    assert effects == [Effect.SUBMIT_LETTER, Effect.CAPTURE_PAYMENT]
    assert more == [Effect.SUBMIT_REMAIL]


def test_second_remail_is_illegal() -> None:
    state, _, attempt = run(
        [*TO_IN_TRANSIT, ReturnConfirmed("refused"), RemailRequested(), SubmitConfirmed("l2")]
    )
    state, _, attempt = run([ReturnConfirmed("unclaimed")], start=state, attempt=attempt)
    assert (state, attempt) == (S.RETURNED, REMAIL_ATTEMPT)
    with pytest.raises(IllegalTransition):
        transition(state, RemailRequested(), attempt)
    assert transition(state, ReturnClosed(), attempt).target is S.CLOSED_RETURNED


def test_remail_only_from_a_confirmed_return() -> None:
    for table in (TRANSITIONS, REMAIL_TRANSITIONS):
        sources = {src for (src, ev) in table if ev is RemailRequested}
        assert sources <= {S.RETURNED}
        entries = {src for (src, ev), rule in table.items() if rule.target is S.RETURNED}
        assert entries == {S.IN_TRANSIT}
        assert {ev for (src, ev), r in table.items() if r.target is S.RETURNED} == {ReturnConfirmed}
    assert RemailRequested not in {ev for (_, ev) in REMAIL_TRANSITIONS}


def test_remail_attempt_has_no_payment_effects_and_failure_closes_as_returned() -> None:
    for rule in REMAIL_TRANSITIONS.values():
        assert not {Effect.CAPTURE_PAYMENT, Effect.RELEASE_HOLD} & set(rule.effects)
    for event in (SubmitRejected("bad"), ConfirmedNotSent("none")):
        assert transition(S.SUBMITTING, event, REMAIL_ATTEMPT).target is S.CLOSED_RETURNED


def test_remail_attempt_cannot_reach_states_before_submitting() -> None:
    after = reachable(REMAIL_TRANSITIONS, S.SUBMITTING)
    assert after == {S.SUBMITTING, S.IN_TRANSIT, S.DELIVERED, S.RETURNED, S.CLOSED_RETURNED}


def test_closing_a_return_ends_the_mailing() -> None:
    assert transition(S.RETURNED, ReturnClosed()).target is S.CLOSED_RETURNED


@pytest.mark.parametrize("attempt", [0, 3])
def test_only_two_attempts_exist(attempt: int) -> None:
    with pytest.raises(ValueError, match="attempt"):
        table_for(attempt)


def test_removed_v5_states_are_gone() -> None:
    names = {s.name for s in CaseState}
    for gone in ("HELD_RESEARCH", "HELD_SUBMISSION", "HOLD_RELEASED", "LOB_LOOKUP"):
        assert gone not in names


def test_delivery_proof_is_accepted_in_transit_without_any_payment_input() -> None:
    assert transition(S.IN_TRANSIT, DeliveryProofStored("p")).target is S.DELIVERED


@pytest.mark.parametrize("attempt", [FIRST_ATTEMPT, REMAIL_ATTEMPT])
@pytest.mark.parametrize("state", list(CaseState))
@pytest.mark.parametrize("event", SAMPLE_EVENTS, ids=lambda e: type(e).__name__)
def test_every_pair_outside_the_table_raises(state: CaseState, event: Event, attempt: int) -> None:
    if (state, type(event)) in table_for(attempt):
        assert transition(state, event, attempt).source is state
    else:
        with pytest.raises(IllegalTransition) as info:
            transition(state, event, attempt)
        assert info.value.state is state
        assert type(event).__name__ in str(info.value)


@pytest.mark.parametrize("attempt", [FIRST_ATTEMPT, REMAIL_ATTEMPT])
def test_terminal_states_accept_nothing(attempt: int) -> None:
    for state in TERMINAL_STATES:
        assert allowed_events(state, attempt) == frozenset()


def test_every_state_is_reachable_from_intake() -> None:
    assert reachable(TRANSITIONS, S.INTAKE) == set(CaseState)


def test_every_non_terminal_state_can_finish() -> None:
    for state in set(CaseState) - TERMINAL_STATES:
        assert reachable(TRANSITIONS, state) & TERMINAL_STATES, state
    for state in {src for (src, _) in REMAIL_TRANSITIONS}:
        assert reachable(REMAIL_TRANSITIONS, state) & TERMINAL_STATES, state


def test_once_submitting_a_case_never_returns_to_drafting() -> None:
    after_submit = reachable(TRANSITIONS, S.SUBMITTING)
    assert not after_submit & {S.RESEARCHING, S.DRAFTING, S.AWAITING_ADOPTION, S.FROZEN}


def test_after_submitting_only_a_confirmed_return_can_trigger_another_submit() -> None:
    after_submit = reachable(TRANSITIONS, S.SUBMITTING)
    for table in (TRANSITIONS, REMAIL_TRANSITIONS):
        for (src, ev), rule in table.items():
            if src in after_submit and {Effect.SUBMIT_LETTER, Effect.SUBMIT_REMAIL} & set(
                rule.effects
            ):
                assert (src, ev, table) == (S.RETURNED, RemailRequested, TRANSITIONS)


def test_mailed_letter_can_never_be_released_or_stopped() -> None:
    # On attempt 1 a mailed letter leaves the mailed states only by the re-mail, which
    # switches to REMAIL_TRANSITIONS; so attempt 1 is checked up to SUBMITTING.
    for state in MAILED_STATES:
        attempt_one = reachable(TRANSITIONS, state, avoid=frozenset({S.SUBMITTING}))
        assert attempt_one <= MAILED_STATES
        assert reachable(REMAIL_TRANSITIONS, state) & {S.STOPPED_NO_CHARGE} == set()
    for table in (TRANSITIONS, REMAIL_TRANSITIONS):
        for (src, _), rule in table.items():
            if src in MAILED_STATES:
                assert Effect.RELEASE_HOLD not in rule.effects


def test_capture_is_emitted_only_when_entering_in_transit_on_attempt_one() -> None:
    for (src, _), rule in TRANSITIONS.items():
        if Effect.CAPTURE_PAYMENT in rule.effects:
            assert (src, rule.target) == (S.SUBMITTING, S.IN_TRANSIT)
        if rule.target is S.IN_TRANSIT:
            assert Effect.CAPTURE_PAYMENT in rule.effects


def test_release_is_emitted_exactly_when_stopping_after_authorization() -> None:
    for (src, _), rule in TRANSITIONS.items():
        expected = rule.target is S.STOPPED_NO_CHARGE and src is not S.INTAKE
        assert (Effect.RELEASE_HOLD in rule.effects) == expected, src


@given(st.lists(st.sampled_from(SAMPLE_EVENTS), max_size=60))
def test_random_event_streams_respect_the_tables(events: list[Event]) -> None:
    state = S.INTAKE
    attempt = FIRST_ATTEMPT
    mailed = False
    submits = 0
    for event in events:
        table = table_for(attempt)
        try:
            t = transition(state, event, attempt)
        except IllegalTransition:
            assert (state, type(event)) not in table
            continue
        assert t.target is table[(state, type(event))].target
        if mailed:
            assert Effect.SUBMIT_LETTER not in t.effects
            assert Effect.RELEASE_HOLD not in t.effects
            assert Effect.CAPTURE_PAYMENT not in t.effects
        submits += len({Effect.SUBMIT_LETTER, Effect.SUBMIT_REMAIL} & set(t.effects))
        assert submits <= 2
        mailed = mailed or t.target in MAILED_STATES
        attempt = next_attempt(attempt, t)
        state = t.target


def specific_stop(reason: StopReason) -> Stopped:
    hard_stop = (
        HardStop.S3
        if reason in {StopReason.PROCESS_INCOMPATIBLE, StopReason.NOT_YET_VERIFIED}
        else None
    )
    return Stopped(reason, "Official requirement", hard_stop)


@pytest.mark.parametrize(
    ("reason", "family"),
    [
        (StopReason.PROCESS_INCOMPATIBLE, StopFamily.PROCESS_INCOMPATIBLE),
        (StopReason.NOT_FEASIBLE, StopFamily.PROCESS_INCOMPATIBLE),
        (StopReason.NOT_YET_VERIFIED, StopFamily.NOT_YET_VERIFIED),
        (StopReason.UNRESOLVABLE_FACT, StopFamily.NOT_YET_VERIFIED),
        (StopReason.BUSINESS_EXCLUSION, StopFamily.BUSINESS_EXCLUSION),
        (StopReason.UNSUPPORTED, StopFamily.BUSINESS_EXCLUSION),
        *[
            (reason, StopFamily.TRANSACTION_FAILURE)
            for reason in (
                StopReason.DECLINED,
                StopReason.WINDOW_EXPIRED,
                StopReason.ADDRESS_UNDELIVERABLE,
                StopReason.LOB_REJECTED,
                StopReason.NOT_SENT,
            )
        ],
    ],
)
def test_every_stop_reason_has_its_v7_family(reason: StopReason, family: StopFamily) -> None:
    assert stop_family(reason) is family


@pytest.mark.parametrize("reason", [StopReason.PROCESS_INCOMPATIBLE, StopReason.NOT_YET_VERIFIED])
def test_research_stop_requires_a_hard_stop(reason: StopReason) -> None:
    with pytest.raises(ValueError, match="hard_stop"):
        Stopped(reason, "Official requirement")


@pytest.mark.parametrize("detail", ["", " ", "\n\t"])
def test_process_incompatible_requires_official_requirement(detail: str) -> None:
    with pytest.raises(ValueError, match="official requirement"):
        Stopped(StopReason.PROCESS_INCOMPATIBLE, detail, HardStop.S3)


@pytest.mark.parametrize(
    "reason",
    sorted(
        set(StopReason)
        - {
            StopReason.PROCESS_INCOMPATIBLE,
            StopReason.NOT_YET_VERIFIED,
        }
    ),
)
def test_other_reasons_cannot_carry_hard_stop(reason: StopReason) -> None:
    with pytest.raises(ValueError, match="must not carry"):
        Stopped(reason, "detail", HardStop.S3)


@pytest.mark.parametrize("hard_stop", list(HardStop))
def test_v7_stop_numbers_can_reach_the_single_terminal(hard_stop: HardStop) -> None:
    event = Stopped(StopReason.NOT_YET_VERIFIED, "Bounded recovery exhausted", hard_stop)
    assert transition(S.RESEARCHING, event).target is S.STOPPED_NO_CHARGE
    assert event.hard_stop is hard_stop
    assert hard_stop.value == hard_stop.name
