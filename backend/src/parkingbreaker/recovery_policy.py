"""Restore comparison and backup-room policy. Callers supply facts; no I/O."""

from parkingbreaker import prices
from parkingbreaker.lob_submission import MetadataLookup
from parkingbreaker.payment_records import PaymentOutcome
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.prices import CERTIFIED_CENTS


def payment_compatible(
    state: PaymentState, outcome: PaymentOutcome, amount_cents: int = CERTIFIED_CENTS
) -> bool:
    if (
        type(amount_cents) is not int
        or amount_cents not in prices.ALLOWED_PRICE_CENTS
        or not outcome.full_object
        or outcome.amount_cents != amount_cents
    ):
        return False
    allowed = {
        PaymentState.AUTHORIZED: {"requires_capture"},
        PaymentState.CAPTURE_PENDING: {"requires_capture", "succeeded"},
        PaymentState.CAPTURE_RETRYING: {"requires_capture", "succeeded"},
        PaymentState.CAPTURED: {"succeeded"},
        PaymentState.RELEASED: {"canceled", "expired"},
    }
    return outcome.status in allowed.get(state, set())


def letter_compatible(original_id: str | None, found: MetadataLookup) -> bool:
    if original_id is not None:
        return found.letter_id == original_id
    return found.letter_id is not None or found.authoritative


def backup_room(*, free_bytes: int, needed_bytes: int, reserve_bytes: int) -> bool:
    """A backup must leave the authority disk its reserve; refusing is safer than ENOSPC."""
    return free_bytes >= needed_bytes + reserve_bytes
