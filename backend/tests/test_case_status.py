from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.case_state import (
    FIRST_ATTEMPT,
    MAILED_STATES,
    TERMINAL_STATES,
    CaseState,
    Effect,
    next_attempt,
    transition,
)
from parkingbreaker.case_status import CaseStatus, case_status, payment_event_for
from parkingbreaker.payment_state import (
    CaptureFailed,
    CaptureRequested,
    PaymentEvent,
    PaymentState,
    ReleaseRequested,
    payment_transition,
)
from parkingbreaker.state_table import IllegalTransition
from samples import LETTER_EVENTS, PAYMENT_OUTCOMES

C = CaseState
P = PaymentState


def test_payment_event_mapping() -> None:
    assert payment_event_for(Effect.CAPTURE_PAYMENT) == CaptureRequested()
    assert payment_event_for(Effect.RELEASE_HOLD) == ReleaseRequested()
    for effect in set(Effect) - {Effect.CAPTURE_PAYMENT, Effect.RELEASE_HOLD}:
        assert payment_event_for(effect) is None


@pytest.mark.parametrize(
    ("letter", "payment", "expected"),
    [
        (C.STOPPED_NO_CHARGE, None, CaseStatus.DONE),
        (C.INTAKE, None, CaseStatus.IN_PROGRESS),
        (C.DELIVERED, P.CAPTURED, CaseStatus.DONE),
        (C.DELIVERED, P.WRITTEN_OFF, CaseStatus.DONE),
        (C.DELIVERED, P.CAPTURE_PENDING, CaseStatus.IN_PROGRESS),
        (C.DELIVERED, P.CAPTURE_RETRYING, CaseStatus.IN_PROGRESS),
        (C.DELIVERED, P.REFUNDED, CaseStatus.DONE),
        (C.DELIVERED, P.RELEASED, CaseStatus.NEEDS_OPERATOR),
        (C.IN_TRANSIT, P.CAPTURED, CaseStatus.IN_PROGRESS),
        (C.IN_TRANSIT, P.RELEASED, CaseStatus.NEEDS_OPERATOR),
        (C.RETURNED, P.CAPTURED, CaseStatus.IN_PROGRESS),
        (C.CLOSED_RETURNED, P.CAPTURED, CaseStatus.DONE),
        (C.CLOSED_RETURNED, P.RELEASED, CaseStatus.NEEDS_OPERATOR),
        (C.STOPPED_NO_CHARGE, P.RELEASED, CaseStatus.DONE),
        (C.STOPPED_NO_CHARGE, P.CAPTURED, CaseStatus.IN_PROGRESS),
        (C.STOPPED_NO_CHARGE, P.REFUNDED, CaseStatus.DONE),
        (C.STOPPED_NO_CHARGE, P.AUTHORIZED, CaseStatus.IN_PROGRESS),
        (C.NEEDS_CUSTOMER, P.AUTHORIZED, CaseStatus.IN_PROGRESS),
        (C.RESEARCHING, P.AUTHORIZED, CaseStatus.IN_PROGRESS),
    ],
)
def test_case_status_examples(
    letter: CaseState, payment: PaymentState | None, expected: CaseStatus
) -> None:
    assert case_status(letter, payment) is expected


@pytest.mark.parametrize(
    ("letter", "payment"), list(itertools.product(CaseState, [None, *PaymentState]))
)
def test_case_status_rules_hold_for_every_combination(
    letter: CaseState, payment: PaymentState | None
) -> None:
    status = case_status(letter, payment)
    if status is CaseStatus.NEEDS_OPERATOR:
        assert letter in MAILED_STATES
        assert payment is P.RELEASED
    if status is CaseStatus.DONE:
        assert letter in TERMINAL_STATES
        assert payment in (
            P.NO_CHARGE,
            P.CAPTURED,
            P.REFUNDED,
            P.WRITTEN_OFF,
            P.RELEASED,
        ) or (letter is C.STOPPED_NO_CHARGE and payment is None)
        if payment is P.RELEASED:
            assert letter not in MAILED_STATES


@pytest.mark.parametrize("letter", sorted(TERMINAL_STATES))
def test_free_terminal_case_is_done(letter: CaseState) -> None:
    assert case_status(letter, P.NO_CHARGE) is CaseStatus.DONE


@pytest.mark.parametrize("letter", [state for state in CaseState if state not in TERMINAL_STATES])
def test_free_nonterminal_case_is_in_progress(letter: CaseState) -> None:
    assert case_status(letter, P.NO_CHARGE) is CaseStatus.IN_PROGRESS


# --- Both machines driven together, the way the I/O layer will wire them ---------------

Step = cs.Event | PaymentEvent
_PAYMENT_TYPES = (
    ps.CaptureRequested,
    ps.CaptureSucceeded,
    ps.CaptureFailed,
    ps.RetryCapture,
    ps.CardDeclined,
    ps.AuthorizationLapsed,
    ps.ReconciliationExhausted,
    ps.ReleaseRequested,
    ps.RefundIssued,
    ps.ReturnedMailRefundRequested,
    ps.ReauthorizationNeeded,
    ps.UnexpectedCaptureConfirmed,
)


def drive(steps: list[Step]) -> list[tuple[CaseState, PaymentState | None]]:
    """Apply letter events and payment outcomes; route letter effects into payment events.

    Illegal steps are skipped, as the I/O layer would reject them.
    """
    letter: CaseState = C.INTAKE
    attempt = FIRST_ATTEMPT
    payment: PaymentState | None = None
    history = [(letter, payment)]
    for step in steps:
        try:
            if isinstance(step, _PAYMENT_TYPES):
                if payment is None:
                    continue
                payment = payment_transition(payment, step).target
            else:
                t = transition(letter, step, attempt)
                attempt = next_attempt(attempt, t)
                letter = t.target
                if isinstance(step, cs.PaymentAuthorized):
                    payment = P.AUTHORIZED
                for effect in t.effects:
                    event = payment_event_for(effect)
                    if event is not None:
                        assert payment is not None
                        payment = payment_transition(payment, event).target
        except IllegalTransition:
            continue
        history.append((letter, payment))
    return history


@given(st.lists(st.sampled_from(LETTER_EVENTS + PAYMENT_OUTCOMES), max_size=60))
def test_once_mailed_payment_is_never_released(steps: list[Step]) -> None:
    mailed = False
    for letter, payment in drive(steps):
        mailed = mailed or letter in MAILED_STATES
        if mailed:
            assert payment is not P.RELEASED
        if case_status(letter, payment) is CaseStatus.DONE and mailed:
            assert payment in (P.CAPTURED, P.REFUNDED, P.WRITTEN_OFF)


def test_capture_failure_after_proof_is_retried_automatically() -> None:
    history = drive(
        [
            cs.PaymentAuthorized(),
            cs.ResearchComplete(),
            cs.DraftReady(),
            cs.CustomerApproved(),
            cs.SubmitRequested(),
            cs.SubmitConfirmed("ltr_1"),
            cs.DeliveryProofStored("proof_1"),
            CaptureFailed("declined"),
        ]
    )
    letter, payment = history[-1]
    assert (letter, payment) == (C.DELIVERED, P.CAPTURE_RETRYING)
    assert case_status(letter, payment) is CaseStatus.IN_PROGRESS


def test_every_drive_step_was_applied() -> None:
    history = drive([cs.PaymentAuthorized(), cs.WindowExpired()])
    assert history[-1] == (C.STOPPED_NO_CHARGE, P.RELEASED)
    assert case_status(*history[-1]) is CaseStatus.DONE


def test_returned_and_remailed_letter_is_charged_once() -> None:
    history = drive(
        [
            cs.PaymentAuthorized(),
            cs.ResearchComplete(),
            cs.DraftReady(),
            cs.CustomerApproved(),
            cs.SubmitRequested(),
            cs.SubmitConfirmed("ltr_1"),
            ps.CaptureSucceeded("ch_1"),
            cs.ReturnConfirmed("refused"),
            cs.RemailRequested(),
            cs.SubmitConfirmed("ltr_2"),
            cs.ReturnConfirmed("unclaimed"),
            cs.RemailRequested(),
            cs.ReturnClosed(),
        ]
    )
    assert len(history) == 13  # the second RemailRequested was rejected
    assert history[-1] == (C.CLOSED_RETURNED, P.CAPTURED)
    assert case_status(*history[-1]) is CaseStatus.DONE
