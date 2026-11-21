"""Consent-aware outcome follow-up emails (J05).

Two emails go to a customer whose letter was delivered: D+14 and D+30 after delivery, at
most one send per window. A recorded opt-out stops both; a later opt-in resumes them. When
the final window plus a fixed response grace passes without any reported outcome, the case
records UNKNOWN -- never a dismissal or a defeat on the customer's behalf.

The e-mail is strictly clerical: it asks what the agency decided and offers a private
opt-out link. It never states or implies an outcome, and it carries no legal advice.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from parkingbreaker.customer_access import token_digest
from parkingbreaker.email_client import EmailClient, EmailMessage, EmailOutcome, EmailReason
from parkingbreaker.outcome_follow_up_store import FollowUpConflict, FollowUpStoreProtocol

FIRST_WINDOW = "D14"
SECOND_WINDOW = "D30"
FIRST_WINDOW_DAYS = 14
SECOND_WINDOW_DAYS = 30
RESPONSE_GRACE_DAYS = 30
_FOLLOW_UP_DOMAIN = b"parkingbreaker:outcome-follow-up:v1\0"
_LINK_DOMAIN = b"parkingbreaker:email-access:v1\0"


def derive_follow_up_token(key: str, case_token: str) -> str:
    """A stable per-case token for the private opt-out link; never a session credential."""
    if not isinstance(case_token, str) or not case_token:
        raise ValueError("invalid case token")
    digest = hmac.new(
        _decode_key(key), _FOLLOW_UP_DOMAIN + case_token.encode(), hashlib.sha256
    ).hexdigest()
    return digest


def verify_follow_up_token(key: str, case_token: str, presented: str) -> bool:
    if not isinstance(presented, str) or len(presented) != 64:
        return False
    try:
        expected = derive_follow_up_token(key, case_token)
    except ValueError:
        return False
    return hmac.compare_digest(expected, presented)


class OutcomeLinkService:
    """The private per-case link: opt out of reminders, or report what the agency decided."""

    DECISIONS: Final = {"dismissed": "dismissed", "upheld": "upheld"}

    def __init__(
        self,
        store: FollowUpStoreProtocol,
        *,
        key: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._key = key
        self._clock = clock

    def __repr__(self) -> str:
        return "OutcomeLinkService(<redacted>)"

    def opt_out(self, case_token: str, token: str) -> bool:
        """Honour an opt-out only when the presented token binds to this exact case."""
        if not verify_follow_up_token(self._key, case_token, token):
            return False
        email = self._email_for(case_token)
        if email is None:
            return False
        allowed, revision = self._store.follow_up_consent_state(email)
        if allowed is False:
            # Already opted out: the request is satisfied, and no new row is needed.
            return True
        # The command id carries the transition, so re-clicking after a later opt-in is a
        # new decision rather than a replay that would leave the old opt-in in force.
        self._store.set_follow_up_consent(
            email,
            False,
            f"optout:{case_token}:{revision + 1}",
            "customer",
            "customer opted out through the private link",
            now=self._clock(),
        )
        return True

    def report(self, case_token: str, token: str, decision: str) -> bool:
        """Record a customer-reported decision; "pending" records nothing at all."""
        if not verify_follow_up_token(self._key, case_token, token):
            return False
        if decision == "pending":
            return True
        if decision not in self.DECISIONS:
            return False
        email = self._email_for(case_token)
        if email is None:
            return False
        try:
            self._store.record_outcome(
                case_token,
                "OUTCOME_CUSTOMER",
                f"customer_reported:{decision}",
                f"report:{case_token}:{decision}",
                "customer",
                "customer reported the agency decision",
                now=self._clock(),
            )
        except FollowUpConflict:
            # One outcome per case is structural; a repeat with the same decision is a
            # satisfied request, a different decision cannot overwrite the record.
            existing = self._store.outcome_for(case_token)
            return existing is not None and existing.detail == f"customer_reported:{decision}"
        return True

    def _email_for(self, case_token: str) -> str | None:
        raw = self._store.intake_json_for_case(case_token)
        if raw is None:
            return None
        try:
            body = json.loads(raw)
            contact = body.get("contact") if isinstance(body, dict) else None
            email = contact.get("email") if isinstance(contact, dict) else None
        except (TypeError, ValueError):
            return None
        return email if isinstance(email, str) and "@" in email else None


def _decode_key(key: str) -> bytes:
    token_digest(key)
    return base64.urlsafe_b64decode(key + "=")


@dataclass(frozen=True)
class FollowUpReport:
    sent: int = 0
    skipped_no_consent: int = 0
    unknown_recorded: int = 0
    deferred: int = 0

    def __repr__(self) -> str:
        return (
            f"FollowUpReport(sent={self.sent}, skipped_no_consent={self.skipped_no_consent}, "
            f"unknown_recorded={self.unknown_recorded}, deferred={self.deferred})"
        )


class OutcomeFollowUpService:
    """Sends the D+14 and D+30 follow-ups and records honest outcomes."""

    def __init__(
        self,
        store: FollowUpStoreProtocol,
        client: EmailClient,
        *,
        key: str,
        origin: str,
        sender: str,
        clock: Callable[[], datetime],
        limit: int = 50,
        grace_days: int = RESPONSE_GRACE_DAYS,
    ) -> None:
        self._store = store
        self._client = client
        self._key = key
        self._origin = origin.rstrip("/")
        self._sender = sender
        self._clock = clock
        self._limit = limit
        self._grace = timedelta(days=grace_days)

    def __repr__(self) -> str:
        return "OutcomeFollowUpService(<redacted>)"

    def run(self) -> FollowUpReport:
        now = self._clock()
        sent = skipped = unknown = deferred = 0
        for case_token, delivered_at in self._store.delivered_cases(limit=self._limit):
            email = self._customer_email(case_token)
            if not email:
                continue
            allowed = self._store.follow_up_allowed(email)
            due = {
                FIRST_WINDOW: delivered_at + timedelta(days=FIRST_WINDOW_DAYS),
                SECOND_WINDOW: delivered_at + timedelta(days=SECOND_WINDOW_DAYS),
            }
            for window in (FIRST_WINDOW, SECOND_WINDOW):
                if now < due[window] or self._store.follow_up_sent(case_token, window):
                    continue
                if not allowed:
                    skipped += 1
                    continue
                outcome = self._send(email, case_token, window)
                if outcome.status != "accepted":
                    deferred += 1
                    continue
                try:
                    self._store.record_follow_up_send(
                        case_token,
                        window,
                        f"followup:{window}:{case_token}",
                        "worker",
                        "scheduled follow-up",
                        now=now,
                    )
                except FollowUpConflict:
                    # A concurrent worker already recorded this window.
                    continue
                sent += 1

            if (
                self._store.outcome_for(case_token) is None
                and now >= due[SECOND_WINDOW] + self._grace
            ):
                try:
                    self._store.record_unknown_outcome(
                        case_token,
                        f"followup-unknown:{case_token}",
                        "worker",
                        "no response after the final window",
                        now=now,
                    )
                except FollowUpConflict:
                    continue
                unknown += 1
        return FollowUpReport(sent, skipped, unknown, deferred)

    # Internals -------------------------------------------------------------------------
    def _customer_email(self, case_token: str) -> str | None:
        raw = self._store.intake_json_for_case(case_token)
        if raw is None:
            return None
        try:
            body = json.loads(raw)
            contact = body.get("contact") if isinstance(body, dict) else None
            email = contact.get("email") if isinstance(contact, dict) else None
        except (TypeError, ValueError):
            return None
        return email if isinstance(email, str) and "@" in email else None

    def _message(self, email: str, case_token: str, window: str) -> EmailMessage:
        token = derive_follow_up_token(self._key, case_token)
        return EmailMessage(
            email,
            "Did the agency answer your ParkingBreaker appeal?",
            "Hello,\n\n"
            "We mailed your parking appeal. Please tell us what the agency decided, or "
            "whether you have not heard back yet, so your record stays accurate.\n\n"
            "If you have not heard back, there is nothing you need to do.\n\n"
            "We send this reminder once at day 14 and once at day 30 after delivery. "
            "To stop these reminders, open this private link:\n"
            f"{self._origin}/outcomes/opt-out?case={case_token}&token={token}\n\n"
            "ParkingBreaker prepares your appeal clerically and mails it by USPS Certified "
            "Mail. We do not give legal advice and do not promise any outcome.\n\n"
            "ParkingBreaker",
        )

    def _send(self, email: str, case_token: str, window: str) -> EmailOutcome:
        message = self._message(email, case_token, window)
        try:
            outcome = self._client.send(message, f"pb-followup-{window}-{case_token}")
            if not isinstance(outcome, EmailOutcome):
                raise ValueError("invalid email outcome")
        except Exception:
            return EmailOutcome("uncertain", reason=EmailReason.TRANSPORT_ERROR)
        return outcome
