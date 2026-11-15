from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from parkingbreaker.payment_state import (
    EXPIRY_MARGIN,
    MAX_RETRY_DELAY,
    PAYMENT_TRANSITIONS,
    TERMINAL_PAYMENT_STATES,
    AuthorizationLapsed,
    CaptureFailed,
    CaptureRequested,
    CaptureSucceeded,
    CardDeclined,
    PaymentEffect,
    PaymentEvent,
    PaymentState,
    ReauthorizationNeeded,
    RefundIssued,
    ReleaseRequested,
    RetryCapture,
    RetryDecision,
    UnexpectedCaptureConfirmed,
    allowed_payment_events,
    next_capture_retry,
    payment_transition,
    retry_delay,
)
from parkingbreaker.state_table import IllegalTransition, reachable
from samples import PAYMENT_EVENTS

P = PaymentState
PE = PaymentEffect

SAMPLE_EVENTS = PAYMENT_EVENTS
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
AUTH_EXPIRES = NOW + timedelta(days=7)


def run(
    events: list[PaymentEvent], start: PaymentState = P.AUTHORIZED
) -> tuple[PaymentState, list[PaymentEffect]]:
    state = start
    effects: list[PaymentEffect] = []
    for event in events:
        t = payment_transition(state, event)
        state = t.target
        effects.extend(t.effects)
    return state, effects


def test_sample_events_cover_every_table_event_plus_the_stub() -> None:
    assert {type(e) for e in SAMPLE_EVENTS} == {ev for (_, ev) in PAYMENT_TRANSITIONS} | {
        ReauthorizationNeeded
    }


def test_first_capture_succeeds() -> None:
    state, effects = run([CaptureRequested(), CaptureSucceeded("ch_1")])
    assert state is P.CAPTURED
    assert effects == [PE.CAPTURE_PAYMENT]


def test_transient_failures_retry_until_capture_succeeds() -> None:
    state, effects = run(
        [
            CaptureRequested(),
            CaptureFailed("processing_error"),
            RetryCapture(),
            CaptureFailed("processing_error"),
            RetryCapture(),
            CaptureSucceeded("ch_1"),
        ]
    )
    assert state is P.CAPTURED
    assert effects == [PE.CAPTURE_PAYMENT] * 3


@pytest.mark.parametrize("ending", [CardDeclined("do_not_honor"), AuthorizationLapsed()])
def test_write_off_is_automatic_with_payment_link_and_log(ending: PaymentEvent) -> None:
    state, effects = run([CaptureRequested(), CaptureFailed("x"), ending])
    assert state is P.WRITTEN_OFF
    assert effects == [PE.CAPTURE_PAYMENT, PE.SEND_PAYMENT_LINK, PE.LOG_WRITE_OFF]


def test_write_off_only_from_capture_retrying() -> None:
    for state in set(PaymentState) - {P.CAPTURE_RETRYING}:
        for event in (CardDeclined("x"), AuthorizationLapsed()):
            with pytest.raises(IllegalTransition):
                payment_transition(state, event)


def test_release_only_from_authorized() -> None:
    state, effects = run([ReleaseRequested()])
    assert state is P.RELEASED
    assert effects == [PE.RELEASE_HOLD]
    for other in set(PaymentState) - {P.AUTHORIZED}:
        with pytest.raises(IllegalTransition):
            payment_transition(other, ReleaseRequested())


@pytest.mark.parametrize("amount", [1, 1950, 2999])
def test_refund_full_or_partial_records_amount(amount: int) -> None:
    refund = RefundIssued("re_1", amount, 2999)
    state, effects = run([CaptureRequested(), CaptureSucceeded("ch_1"), refund])
    assert state is P.REFUNDED
    assert effects == [PE.CAPTURE_PAYMENT]
    assert refund.amount_cents == amount
    assert refund.is_full is (amount == 2999)


@pytest.mark.parametrize(("amount", "captured"), [(3000, 2999), (0, 2999), (-1, 2999), (1, 0)])
def test_refund_never_above_captured_amount(amount: int, captured: int) -> None:
    with pytest.raises(ValueError, match=r"positive|captured"):
        RefundIssued("re_1", amount, captured)


def test_refund_only_from_captured() -> None:
    for state in set(PaymentState) - {P.CAPTURED}:
        with pytest.raises(IllegalTransition):
            payment_transition(state, RefundIssued("re_1", 100, 2999))


def test_reauthorization_is_a_stub_until_f4() -> None:
    for state in PaymentState:
        with pytest.raises(IllegalTransition):
            payment_transition(state, ReauthorizationNeeded())


@pytest.mark.parametrize("state", list(PaymentState))
@pytest.mark.parametrize("event", SAMPLE_EVENTS, ids=lambda e: type(e).__name__)
def test_every_pair_outside_the_table_raises(state: PaymentState, event: PaymentEvent) -> None:
    if (state, type(event)) in PAYMENT_TRANSITIONS:
        assert payment_transition(state, event).source is state
    else:
        with pytest.raises(IllegalTransition):
            payment_transition(state, event)


def test_terminal_states_accept_nothing() -> None:
    for state in TERMINAL_PAYMENT_STATES:
        assert allowed_payment_events(state) == frozenset()


def test_every_state_reachable_and_every_state_can_finish() -> None:
    paid_states = set(PaymentState) - {P.NO_CHARGE}
    assert reachable(PAYMENT_TRANSITIONS, P.AUTHORIZED) == paid_states
    assert P.NO_CHARGE in TERMINAL_PAYMENT_STATES
    assert reachable(PAYMENT_TRANSITIONS, P.NO_CHARGE) == {P.NO_CHARGE}
    assert reachable(PAYMENT_TRANSITIONS, P.AUTHORIZED) | reachable(
        PAYMENT_TRANSITIONS, P.NO_CHARGE
    ) == set(PaymentState)
    for state in set(PaymentState) - TERMINAL_PAYMENT_STATES:
        assert reachable(PAYMENT_TRANSITIONS, state) & TERMINAL_PAYMENT_STATES


def test_once_capture_is_requested_release_is_unreachable() -> None:
    assert P.RELEASED not in reachable(PAYMENT_TRANSITIONS, P.CAPTURE_PENDING)


def test_capture_effect_only_on_capture_request_or_retry() -> None:
    for (_, event), rule in PAYMENT_TRANSITIONS.items():
        assert (PE.CAPTURE_PAYMENT in rule.effects) == (event in (CaptureRequested, RetryCapture))


def test_no_operator_write_off_event_exists() -> None:
    names = {ev.__name__ for (_, ev) in PAYMENT_TRANSITIONS}
    assert not any("Operator" in name for name in names)


@given(st.lists(st.sampled_from(SAMPLE_EVENTS), max_size=40))
def test_no_path_from_capture_pending_reaches_released(events: list[PaymentEvent]) -> None:
    state = P.CAPTURE_PENDING
    for event in events:
        try:
            state = payment_transition(state, event).target
        except IllegalTransition:
            assert (state, type(event)) not in PAYMENT_TRANSITIONS
            continue
        assert state is not P.RELEASED


@given(st.lists(st.sampled_from(SAMPLE_EVENTS), max_size=40))
def test_random_streams_respect_the_table(events: list[PaymentEvent]) -> None:
    state = P.AUTHORIZED
    capture_requested = False
    for event in events:
        try:
            t = payment_transition(state, event)
        except IllegalTransition:
            assert (state, type(event)) not in PAYMENT_TRANSITIONS
            continue
        if isinstance(event, CaptureRequested):
            capture_requested = True
        state = t.target
        if capture_requested:
            assert state is not P.RELEASED


# --- Retry schedule --------------------------------------------------------------------


def test_retry_delay_backs_off_and_caps() -> None:
    assert [retry_delay(n) for n in (1, 2, 3)] == [
        timedelta(minutes=15),
        timedelta(minutes=30),
        timedelta(hours=1),
    ]
    assert retry_delay(10_000) == MAX_RETRY_DELAY
    with pytest.raises(ValueError, match="at least 1"):
        retry_delay(0)


def test_next_retry_is_now_plus_backoff_when_far_from_expiry() -> None:
    assert next_capture_retry(1, NOW, AUTH_EXPIRES) == NOW + timedelta(minutes=15)


def test_next_retry_is_pulled_in_before_expiry() -> None:
    now = AUTH_EXPIRES - timedelta(hours=1)
    assert next_capture_retry(5, now, AUTH_EXPIRES) == AUTH_EXPIRES - EXPIRY_MARGIN


@pytest.mark.parametrize("past", [timedelta(0), timedelta(minutes=1), timedelta(days=1)])
def test_write_off_when_past_auth_expiry(past: timedelta) -> None:
    assert next_capture_retry(1, AUTH_EXPIRES + past, AUTH_EXPIRES) is RetryDecision.WRITE_OFF
    assert next_capture_retry(1, AUTH_EXPIRES - EXPIRY_MARGIN, AUTH_EXPIRES) is (
        RetryDecision.WRITE_OFF
    )


@given(
    st.integers(min_value=1, max_value=200),
    st.timedeltas(min_value=timedelta(days=-2), max_value=timedelta(days=8)),
)
def test_retry_is_always_after_now_and_before_expiry(attempts: int, to_expiry: timedelta) -> None:
    decision = next_capture_retry(attempts, NOW, NOW + to_expiry)
    if to_expiry <= EXPIRY_MARGIN:
        assert decision is RetryDecision.WRITE_OFF
    else:
        assert isinstance(decision, datetime)
        assert NOW < decision <= NOW + to_expiry - EXPIRY_MARGIN
        assert decision - NOW <= MAX_RETRY_DELAY


def test_unexpected_capture_requests_exact_compensation() -> None:
    t = payment_transition(P.RELEASED, UnexpectedCaptureConfirmed("pi_1", "ch_1"))
    assert t.target is P.CAPTURED
    assert t.effects == (PE.REFUND_FULL, PE.LOG_COMPENSATION)
    assert P.RELEASED not in TERMINAL_PAYMENT_STATES
    assert allowed_payment_events(P.RELEASED) == {UnexpectedCaptureConfirmed}
