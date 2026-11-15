"""Combined view of the letter and payment machines. Pure.

Two small functions:

- payment_event_for: the payment event the I/O layer feeds the payment machine when a
  letter transition emits CAPTURE_PAYMENT or RELEASE_HOLD. One mapping, used everywhere.
- case_status: whether a case is done, needs an operator, or is still in progress.
"""

from __future__ import annotations

from enum import StrEnum

from parkingbreaker.case_state import MAILED_STATES, TERMINAL_STATES, CaseState, Effect
from parkingbreaker.payment_state import (
    CaptureRequested,
    PaymentEvent,
    PaymentState,
    ReleaseRequested,
)


class CaseStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    NEEDS_OPERATOR = "needs_operator"
    DONE = "done"


def payment_event_for(effect: Effect) -> PaymentEvent | None:
    """The payment event a letter effect triggers, or None if it is not a payment effect."""
    if effect is Effect.CAPTURE_PAYMENT:
        return CaptureRequested()
    if effect is Effect.RELEASE_HOLD:
        return ReleaseRequested()
    return None


SETTLED_AFTER_CAPTURE = frozenset(
    {PaymentState.CAPTURED, PaymentState.REFUNDED, PaymentState.WRITTEN_OFF}
)


def case_status(letter: CaseState, payment: PaymentState | None) -> CaseStatus:
    """Overall status. `payment` is None until the card hold exists (and after a hard stop).

    - needs_operator: the two machines disagree in a way that loses money (a mailed
      letter whose hold was released). The v6 letter machine has no held-for-a-person
      states, and capture retries and write-offs are automatic, so payment alone never
      needs an operator.
    - done: the letter is terminal and payment is settled: CAPTURED, REFUNDED or
      WRITTEN_OFF, or RELEASED when nothing was mailed. STOPPED_NO_CHARGE with CAPTURED
      stays in progress until full compensation confirms REFUNDED. A hard stop with no
      payment is also done.
    - in_progress: everything else.
    """
    mailed = letter in MAILED_STATES
    if payment is PaymentState.NO_CHARGE:
        return CaseStatus.DONE if letter in TERMINAL_STATES else CaseStatus.IN_PROGRESS
    if mailed and payment is PaymentState.RELEASED:
        return CaseStatus.NEEDS_OPERATOR
    if letter not in TERMINAL_STATES:
        return CaseStatus.IN_PROGRESS
    if letter is CaseState.STOPPED_NO_CHARGE and payment is None:
        return CaseStatus.DONE
    if letter is CaseState.STOPPED_NO_CHARGE and payment is PaymentState.CAPTURED:
        return CaseStatus.IN_PROGRESS
    settled = payment in SETTLED_AFTER_CAPTURE or (payment is PaymentState.RELEASED and not mailed)
    return CaseStatus.DONE if settled else CaseStatus.IN_PROGRESS
