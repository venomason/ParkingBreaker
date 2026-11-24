"""Tell the customer their letter was accepted for mailing, with its tracking number (T46).

Runs inside the access-email dispatch, under the same database ownership lock. The email
names no ticket, plate, place or agency: only that the mail provider accepted the letter,
the date (Pacific, the provider's production clock), the mail class, and the USPS tracking
number when there is one. It says "accepted for mailing", not "mailed" or "delivered":
confirmation can precede the first USPS scan. A missing customer address is recorded,
never guessed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime

from parkingbreaker.email_client import EmailClient, EmailMessage, EmailOutcome, EmailReason
from parkingbreaker.lob_run import pacific
from parkingbreaker.tracking_email_store import TrackingEmail
from parkingbreaker.transaction_store import SqliteTransactionStore

SUBJECT = "Your ParkingBreaker letter was accepted for mailing"


def tracking_message(item: TrackingEmail) -> EmailMessage:
    if not item.certified:
        return EmailMessage(
            item.email,
            "Your First-Class letter was accepted for mailing",
            "Our mail provider accepted your appeal for ordinary First-Class Mail on "
            f"{pacific(item.confirmed_at).date().isoformat()}. Your appeal page has the "
            "mailing record. An acknowledgment or decision from the agency can confirm "
            "that it opened your filing.",
        )
    number = (
        f"Its USPS tracking number is {item.tracking_number}. You can look it up at usps.com."
        if item.tracking_number
        else "No USPS tracking number has been reported for it yet; your appeal page will "
        "show one if it is reported."
    )
    return EmailMessage(
        item.email,
        SUBJECT,
        "Your appeal letter was accepted by our mail provider for certified mail on "
        f"{pacific(item.confirmed_at).date().isoformat()}.\n{number}\n\n"
        "This email is not proof of mailing or delivery. Your appeal page shows every "
        "tracking update.",
    )


def idempotency_key(item: TrackingEmail) -> str:
    """Stable across retries, so the provider sends one email per letter."""
    digest = hashlib.sha256(f"{item.case_token}:{item.letter_id}".encode()).hexdigest()
    return f"pb-tracking-{digest[:40]}"


class TrackingEmailService:
    def __init__(
        self,
        store: SqliteTransactionStore,
        client: EmailClient,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._store, self._client, self._clock = store, client, clock

    def __repr__(self) -> str:
        return "TrackingEmailService(<redacted>)"

    def dispatch(self, *, limit: int = 10) -> int:
        """Queue new confirmations and send what is due; the caller holds ownership."""
        self._store.queue_tracking_emails(self._clock())
        sent = 0
        for due in self._store.due_tracking_emails(self._clock(), limit=limit):
            item = self._store.prepare_tracking_email(due, now=self._clock())
            if item is None:
                continue
            try:
                outcome = self._client.send(tracking_message(item), idempotency_key(item))
                if not isinstance(outcome, EmailOutcome):
                    raise ValueError("invalid email outcome")
            except Exception:
                outcome = EmailOutcome("uncertain", reason=EmailReason.TRANSPORT_ERROR)
            self._store.finish_tracking_email(item, outcome, now=self._clock())
            sent += 1
        return sent
