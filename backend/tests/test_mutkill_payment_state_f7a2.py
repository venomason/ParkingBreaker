from __future__ import annotations

from datetime import timedelta

import pytest

from parkingbreaker import payment_state
from parkingbreaker.payment_state import (
    MAX_RETRY_DELAY,
    CaptureRequested,
    CaptureSucceeded,
    PaymentEffect,
    PaymentState,
    RefundIssued,
    ReturnedMailRefundRequested,
    UnexpectedCaptureConfirmed,
    retry_delay,
)
from parkingbreaker.prices import CERTIFIED_CENTS
from test_payment_state import run

PE = PaymentEffect


@pytest.mark.parametrize("authorization_ref", ["ch_1", "pi_1!"])
def test_unexpected_capture_requires_original_provider_refs(authorization_ref: str) -> None:
    with pytest.raises(ValueError, match="requires original full USD service capture"):
        UnexpectedCaptureConfirmed(authorization_ref, "ch_1")


def test_zero_capture_names_the_captured_amount() -> None:
    with pytest.raises(ValueError, match="captured amount must be positive"):
        RefundIssued("re_1", 100, 0)


def test_refund_above_capture_names_the_refund() -> None:
    with pytest.raises(ValueError, match="never above the captured amount"):
        RefundIssued("re_1", 4000, 2999)


def test_one_cent_capture_is_a_valid_full_refund() -> None:
    refund = RefundIssued("re_1", 1, 1)
    assert refund.is_full
    state, effects = run([CaptureRequested(), CaptureSucceeded("ch_1"), refund])
    assert state is PaymentState.REFUNDED
    assert effects == [PE.CAPTURE_PAYMENT]


def test_returned_mail_refund_kind_must_be_a_refund() -> None:
    with pytest.raises(ValueError, match="invalid returned-mail refund intent"):
        ReturnedMailRefundRequested("FULL", CERTIFIED_CENTS, 1)


def test_returned_mail_partial_refund_covers_both_amount_endpoints() -> None:
    low = ReturnedMailRefundRequested("PARTIAL_REFUND", 1, 1)
    high = ReturnedMailRefundRequested("PARTIAL_REFUND", CERTIFIED_CENTS, 1)
    assert (low.amount_cents, high.amount_cents) == (1, CERTIFIED_CENTS)


def test_returned_mail_full_refund_requires_the_whole_certified_amount() -> None:
    with pytest.raises(ValueError, match="invalid returned-mail refund intent"):
        ReturnedMailRefundRequested("FULL_REFUND", CERTIFIED_CENTS - 1, 1)
    full = ReturnedMailRefundRequested("FULL_REFUND", CERTIFIED_CENTS, 1)
    assert full.kind == "FULL_REFUND"


def test_returned_mail_refund_runs_on_attempts_one_and_two_only() -> None:
    assert ReturnedMailRefundRequested("PARTIAL_REFUND", 1, 2).attempt == 2
    with pytest.raises(ValueError, match="invalid returned-mail refund intent"):
        ReturnedMailRefundRequested("PARTIAL_REFUND", 1, 3)


def test_max_retry_delay_is_twelve_hours() -> None:
    assert timedelta(hours=12) == MAX_RETRY_DELAY
    assert retry_delay(10_000) == MAX_RETRY_DELAY


def test_far_attempts_stop_doubling_the_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the first delay so growth saturates on the exponent bound, not the 12h cap."""
    monkeypatch.setattr(payment_state, "FIRST_RETRY_DELAY", timedelta(microseconds=1))
    assert retry_delay(17) == retry_delay(16) * 2
    assert retry_delay(18) == retry_delay(17)
