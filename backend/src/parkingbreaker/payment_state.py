"""Payment state machine (architecture v6, section 8.2), separate from the letter machine.

Pure: no I/O, no Stripe calls, no clocks. Callers pass `now` and the authorization expiry.

    AUTHORIZED --CaptureRequested--> CAPTURE_PENDING --CaptureSucceeded--> CAPTURED
        |                                 |                                   |
        | ReleaseRequested                | CaptureFailed       RefundIssued  |
        v                                 v                                   v
    RELEASED                      CAPTURE_RETRYING --CaptureSucceeded-->  REFUNDED
                                     |  ^      |
                     CaptureFailed,  |  |      | CardDeclined, AuthorizationLapsed
                     RetryCapture    +--+      v
                                            WRITTEN_OFF

CaptureRequested is sent only once Lob has accepted the letter (the letter machine's
CAPTURE_PAYMENT effect, see case_status.payment_event_for). Release is only possible from
AUTHORIZED, so a mailed letter's payment can never be RELEASED. There is no operator
write-off: WRITTEN_OFF is reached automatically when the card definitively declines or the
authorization lapses, and it tells the customer (payment link) and logs the write-off.
ReconciliationExhausted also closes collection as WRITTEN_OFF, but logs only: the original
money outcome is still unknown, so another payment request could double-charge the customer.
RELEASED is normally settled, but original capture evidence on a legitimate no-charge stop
uses UnexpectedCaptureConfirmed to record CAPTURED and request full compensation plus a
pending significant log. Only confirmed RefundIssued completes that exceptional path.

REFUNDED means the requested refund settled, full or partial; RefundIssued retains both
refunded and captured cents. It never asserts that the entire charge was returned.
ReturnedMailRefundRequested keeps CAPTURED until a complete original-charge refund lookup
confirms the exact amount. Its single durable effect represents the pending refund.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from parkingbreaker import prices
from parkingbreaker.prices import CERTIFIED_CENTS
from parkingbreaker.state_table import Rule, Table, Transition, allowed, apply


class PaymentState(StrEnum):
    NO_CHARGE = "NO_CHARGE"
    AUTHORIZED = "AUTHORIZED"
    CAPTURE_PENDING = "CAPTURE_PENDING"
    CAPTURE_RETRYING = "CAPTURE_RETRYING"
    CAPTURED = "CAPTURED"
    WRITTEN_OFF = "WRITTEN_OFF"
    RELEASED = "RELEASED"
    REFUNDED = "REFUNDED"


TERMINAL_PAYMENT_STATES: frozenset[PaymentState] = frozenset(
    {PaymentState.NO_CHARGE, PaymentState.WRITTEN_OFF, PaymentState.REFUNDED}
)


class PaymentEffect(StrEnum):
    """Calls the I/O layer makes after a transition."""

    CAPTURE_PAYMENT = "CAPTURE_PAYMENT"
    RELEASE_HOLD = "RELEASE_HOLD"
    SEND_PAYMENT_LINK = "SEND_PAYMENT_LINK"
    LOG_WRITE_OFF = "LOG_WRITE_OFF"
    REFUND_FULL = "REFUND_FULL"
    REFUND_RETURNED_MAIL = "REFUND_RETURNED_MAIL"
    LOG_COMPENSATION = "LOG_COMPENSATION"


@dataclass(frozen=True)
class CaptureRequested:
    """Lob accepted the letter; capture the authorized amount (own idempotency key)."""


@dataclass(frozen=True)
class CaptureSucceeded:
    charge_ref: str


@dataclass(frozen=True)
class CaptureFailed:
    """Capture or bounded settlement failed; reason distinguishes provider evidence
    from reconciliation exhaustion. A first failure moves to CAPTURE_RETRYING; the
    caller then schedules a retry or sends the appropriate terminal event."""

    reason: str


@dataclass(frozen=True)
class RetryCapture:
    """The retry time chosen by next_capture_retry has arrived."""


@dataclass(frozen=True)
class CardDeclined:
    """The card definitively declined; no retry can succeed."""

    reason: str


@dataclass(frozen=True)
class AuthorizationLapsed:
    """Provider positively confirmed the original authorization canceled or expired."""


@dataclass(frozen=True)
class ReconciliationExhausted:
    """Terminal unreconciled accounting outcome; never requests another payment."""


@dataclass(frozen=True)
class ReleaseRequested:
    """Nothing was mailed; release the hold."""


@dataclass(frozen=True)
class UnexpectedCaptureConfirmed:
    """Authoritative original capture on a legitimate no-charge stop (note 20)."""

    authorization_ref: str
    charge_ref: str
    amount_cents: int = CERTIFIED_CENTS
    currency: str = "usd"

    def __post_init__(self) -> None:
        if (
            not self.authorization_ref.startswith("pi_")
            or not self.authorization_ref[3:].isalnum()
            or not self.charge_ref.startswith("ch_")
            or not self.charge_ref[3:].isalnum()
            or type(self.amount_cents) is not int
            or self.amount_cents not in prices.ALLOWED_PRICE_CENTS
            or self.currency != "usd"
        ):
            raise ValueError("requires original full USD service capture")


@dataclass(frozen=True)
class RefundIssued:
    """A refund was issued against the capture, full or partial. Amounts in cents."""

    refund_ref: str
    amount_cents: int
    captured_cents: int

    def __post_init__(self) -> None:
        if self.captured_cents <= 0:
            raise ValueError("captured amount must be positive")
        if not 0 < self.amount_cents <= self.captured_cents:
            raise ValueError("refund must be positive and never above the captured amount")

    @property
    def is_full(self) -> bool:
        return self.amount_cents == self.captured_cents


@dataclass(frozen=True)
class ReturnedMailRefundRequested:
    """Immutable clerical remedy intent; CAPTURED remains true until reconciliation."""

    kind: str
    amount_cents: int
    attempt: int

    def __post_init__(self) -> None:
        if (
            self.kind not in {"FULL_REFUND", "PARTIAL_REFUND", "NEVER_MAILED"}
            or type(self.amount_cents) is not int
            or not any(1 <= self.amount_cents <= price for price in prices.ALLOWED_PRICE_CENTS)
            or (
                self.kind != "PARTIAL_REFUND"
                and self.amount_cents not in prices.ALLOWED_PRICE_CENTS
            )
            or type(self.attempt) is not int
            or self.attempt not in {1, 2}
            or (self.kind == "NEVER_MAILED" and self.attempt != 1)
        ):
            raise ValueError("invalid returned-mail refund intent")


@dataclass(frozen=True)
class ReauthorizationNeeded:
    """The card hold is about to lapse (about 7 days) before the letter is sent.

    TODO(F4): re-authorize before sending, never after. Not in the table yet, so it raises
    IllegalTransition in every state.
    """


PaymentEvent = (
    CaptureRequested
    | CaptureSucceeded
    | CaptureFailed
    | RetryCapture
    | CardDeclined
    | AuthorizationLapsed
    | ReconciliationExhausted
    | ReleaseRequested
    | UnexpectedCaptureConfirmed
    | RefundIssued
    | ReturnedMailRefundRequested
    | ReauthorizationNeeded
)

P = PaymentState
PE = PaymentEffect
_WRITE_OFF = Rule(P.WRITTEN_OFF, (PE.SEND_PAYMENT_LINK, PE.LOG_WRITE_OFF))

PAYMENT_TRANSITIONS: Table[PaymentState, PaymentEvent, PaymentEffect] = {
    (P.AUTHORIZED, CaptureRequested): Rule(P.CAPTURE_PENDING, (PE.CAPTURE_PAYMENT,)),
    (P.AUTHORIZED, ReleaseRequested): Rule(P.RELEASED, (PE.RELEASE_HOLD,)),
    (P.CAPTURE_PENDING, CaptureSucceeded): Rule(P.CAPTURED),
    (P.CAPTURE_PENDING, CaptureFailed): Rule(P.CAPTURE_RETRYING),
    (P.CAPTURE_RETRYING, RetryCapture): Rule(P.CAPTURE_RETRYING, (PE.CAPTURE_PAYMENT,)),
    (P.CAPTURE_RETRYING, CaptureFailed): Rule(P.CAPTURE_RETRYING),
    (P.CAPTURE_RETRYING, CaptureSucceeded): Rule(P.CAPTURED),
    (P.CAPTURE_RETRYING, CardDeclined): _WRITE_OFF,
    (P.CAPTURE_RETRYING, AuthorizationLapsed): _WRITE_OFF,
    (P.CAPTURE_RETRYING, ReconciliationExhausted): Rule(P.WRITTEN_OFF, (PE.LOG_WRITE_OFF,)),
    (P.RELEASED, UnexpectedCaptureConfirmed): Rule(
        P.CAPTURED, (PE.REFUND_FULL, PE.LOG_COMPENSATION)
    ),
    (P.CAPTURED, RefundIssued): Rule(P.REFUNDED),
    (P.CAPTURED, ReturnedMailRefundRequested): Rule(P.CAPTURED, (PE.REFUND_RETURNED_MAIL,)),
}

del P, PE


def payment_transition(
    state: PaymentState, event: PaymentEvent
) -> Transition[PaymentState, PaymentEvent, PaymentEffect]:
    """Return the transition for `event` in `state`, or raise IllegalTransition."""
    return apply(PAYMENT_TRANSITIONS, state, event)


def allowed_payment_events(state: PaymentState) -> frozenset[type[PaymentEvent]]:
    return allowed(PAYMENT_TRANSITIONS, state)


# --- Capture retry schedule ------------------------------------------------------------


class RetryDecision(StrEnum):
    WRITE_OFF = "WRITE_OFF"


FIRST_RETRY_DELAY = timedelta(minutes=15)
MAX_RETRY_DELAY = timedelta(hours=12)
# Never schedule a capture this close to the authorization expiry.
EXPIRY_MARGIN = timedelta(minutes=30)
# Use T16's existing 30-day uncertainty envelope as a conservative settlement grace
# after the later of recorded capture_before and the first durable uncertain call,
# allowing delayed original-object evidence during provider outages. This accounting
# bound is not a Stripe guarantee: a final real retrieve precedes log-only exhaustion,
# retaining its actual outcome rather than asserting Stripe canceled or never captured.
CAPTURE_RECONCILIATION_GRACE = timedelta(days=30)
CAPTURE_RECONCILIATION_EXHAUSTED = "capture reconciliation exhausted; provider outcome retained"


def retry_delay(failed_attempts: int) -> timedelta:
    """Exponential backoff: 15 min, 30 min, 1 h, ... capped at 12 h."""
    if failed_attempts < 1:
        raise ValueError("failed_attempts must be at least 1")
    return min(FIRST_RETRY_DELAY * (1 << min(failed_attempts - 1, 16)), MAX_RETRY_DELAY)


def next_capture_retry(
    failed_attempts: int, now: datetime, auth_expires_at: datetime
) -> datetime | RetryDecision:
    """When to retry capture after `failed_attempts` transient failures, or WRITE_OFF.

    The retry is pulled in so it lands at least EXPIRY_MARGIN before the authorization
    expires. If no such time is left, the answer is WRITE_OFF (send AuthorizationLapsed).
    """
    last_safe = auth_expires_at - EXPIRY_MARGIN
    if now >= last_safe:
        return RetryDecision.WRITE_OFF
    return min(now + retry_delay(failed_attempts), last_safe)
