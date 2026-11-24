"""Consent-aware outcome follow-up emails (J05).

Certified-mail follow-ups are due 14 and 30 days after confirmed delivery. First-class
pilot reminders use a dated USPS scan or documented printer handoff. Both
respect the same email consent and record sends independently from agency outcomes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from urllib.parse import urlencode

from parkingbreaker.customer_access import token_digest
from parkingbreaker.email_client import EmailClient, EmailMessage, EmailOutcome, EmailReason
from parkingbreaker.outcome_follow_up_store import (
    FollowUpConflict,
    FollowUpStoreProtocol,
    PilotDisposition,
    PilotFeedback,
)
from parkingbreaker.tracking_email_store import SAFE_RETRY_WINDOW

FIRST_WINDOW = "D14"
SECOND_WINDOW = "D30"
FIRST_WINDOW_DAYS = 14
SECOND_WINDOW_DAYS = 30
RESPONSE_GRACE_DAYS = 30
_FOLLOW_UP_DOMAIN = b"parkingbreaker:outcome-follow-up:v1\0"
_PILOT_FEEDBACK_DOMAIN = b"parkingbreaker:pilot-feedback:v1\0"
_LINK_DOMAIN = b"parkingbreaker:email-access:v1\0"


def derive_follow_up_token(key: str, case_token: str) -> str:
    """A stable per-case token for the private opt-out link; never a session credential."""
    return _derive_token(key, case_token, _FOLLOW_UP_DOMAIN)


def derive_pilot_feedback_token(key: str, case_token: str) -> str:
    """A pilot-feedback credential with its own scope."""
    return _derive_token(key, case_token, _PILOT_FEEDBACK_DOMAIN)


def _derive_token(key: str, case_token: str, domain: bytes) -> str:
    if not isinstance(case_token, str) or not case_token:
        raise ValueError("invalid case token")
    return hmac.new(_decode_key(key), domain + case_token.encode(), hashlib.sha256).hexdigest()


def verify_follow_up_token(key: str, case_token: str, presented: str) -> bool:
    return _verify_token(key, case_token, presented, _FOLLOW_UP_DOMAIN)


def _verify_token(key: str, case_token: str, presented: str, domain: bytes) -> bool:
    if not isinstance(presented, str) or len(presented) != 64 or not presented.isascii():
        return False
    try:
        expected = _derive_token(key, case_token, domain)
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
        email = _customer_email(self._store, case_token)
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
        if self._store.is_pilot_case(case_token):
            return False
        if decision == "pending":
            return True
        if decision not in self.DECISIONS:
            return False
        email = _customer_email(self._store, case_token)
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

    def pilot_feedback(self, case_token: str, token: str) -> PilotFeedback | None:
        if not _verify_token(self._key, case_token, token, _PILOT_FEEDBACK_DOMAIN):
            return None
        feedback = self._store.pilot_feedback(case_token)
        return feedback if feedback.eligible else None

    def report_pilot_feedback(
        self,
        case_token: str,
        token: str,
        request_id: str,
        expected_revision: int,
        values: Mapping[str, object],
    ) -> PilotFeedback | None:
        if not _verify_token(self._key, case_token, token, _PILOT_FEEDBACK_DOMAIN):
            return None
        try:
            return self._store.record_pilot_feedback(
                case_token, request_id, expected_revision, values, now=self._clock()
            )
        except FollowUpConflict as exc:
            if str(exc) == "pilot_feedback_not_eligible":
                return None
            raise


def _customer_email(store: FollowUpStoreProtocol, case_token: str) -> str | None:
    raw = store.intake_json_for_case(case_token)
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


def _due_windows(anchor: datetime) -> dict[str, datetime]:
    return {
        FIRST_WINDOW: anchor + timedelta(days=FIRST_WINDOW_DAYS),
        SECOND_WINDOW: anchor + timedelta(days=SECOND_WINDOW_DAYS),
    }


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
            if self._store.outcome_for(case_token) is not None:
                continue
            email = _customer_email(self._store, case_token)
            if not email:
                continue
            allowed = self._store.follow_up_allowed(email)
            due = _due_windows(delivered_at)
            for window in (FIRST_WINDOW, SECOND_WINDOW):
                if now < due[window] or self._store.follow_up_sent(case_token, window):
                    continue
                if not allowed:
                    skipped += 1
                    continue
                outcome = self._send(
                    self._message(email, case_token), f"pb-followup-{window}-{case_token}"
                )
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
        pilot = self._run_pilot(now)
        return FollowUpReport(
            sent + pilot.sent,
            skipped + pilot.skipped_no_consent,
            unknown,
            deferred + pilot.deferred,
        )

    # Internals -------------------------------------------------------------------------
    def _run_pilot(self, now: datetime) -> FollowUpReport:
        sent = skipped = deferred = processed = 0
        for case_token in self._store.pilot_follow_up_candidates():
            if processed >= self._limit:
                break
            feedback = self._store.pilot_feedback(case_token)
            postal_scan = feedback.first_postal_scan_at
            handoff = feedback.printer_handoff_at
            use_handoff = handoff is not None and (postal_scan is None or handoff <= postal_scan)
            anchor = handoff if use_handoff else postal_scan
            if anchor is None or feedback.disposition not in {None, PilotDisposition.PENDING}:
                continue
            timing_basis = "printer handoff" if use_handoff else "USPS scan"
            email = _customer_email(self._store, case_token)
            if not email:
                continue
            windows = _due_windows(anchor)
            # After an outage, send only the current reminder for this case.
            window = SECOND_WINDOW if now >= windows[SECOND_WINDOW] else FIRST_WINDOW
            if now < windows[window] or self._store.follow_up_sent(case_token, window):
                continue
            if not self._store.follow_up_allowed(email):
                skipped += 1
                continue
            started = self._store.start_pilot_follow_up(case_token, window, now=now)
            if now - started.recorded_at >= SAFE_RETRY_WINDOW:
                continue
            processed += 1
            outcome = self._send(
                self._pilot_message(email, case_token),
                f"pb-pilot-followup-{window}-{case_token}",
            )
            if outcome.status != "accepted":
                deferred += 1
                continue
            try:
                recorded = self._store.record_follow_up_send(
                    case_token,
                    window,
                    f"pilot-followup:{window}:{case_token}",
                    "worker",
                    f"scheduled from {timing_basis} at {anchor.isoformat()}",
                    now=now,
                )
            except FollowUpConflict:
                continue
            sent += int(not recorded.replayed)
        return FollowUpReport(sent=sent, skipped_no_consent=skipped, deferred=deferred)

    def _message(self, email: str, case_token: str) -> EmailMessage:
        token = derive_follow_up_token(self._key, case_token)
        fragment = urlencode({"case": case_token, "token": token})
        return EmailMessage(
            email,
            "Did the agency answer your ParkingBreaker appeal?",
            "Hello,\n\n"
            "We mailed your parking appeal. Please tell us what the agency decided, or "
            "whether you have not heard back yet, so your record stays accurate.\n\n"
            "If you have not heard back, there is nothing you need to do.\n\n"
            "We send this reminder once at day 14 and once at day 30 after delivery. "
            "To stop these reminders, open this private link:\n"
            f"{self._origin}/outcomes/opt-out#{fragment}\n\n"
            "ParkingBreaker prepares your appeal clerically and mails it by USPS Certified "
            "Mail. We do not give legal advice and do not promise any outcome.\n\n"
            "ParkingBreaker",
        )

    def _pilot_message(self, email: str, case_token: str) -> EmailMessage:
        opt_out_token = derive_follow_up_token(self._key, case_token)
        feedback = urlencode(
            {
                "case": case_token,
                "token": derive_pilot_feedback_token(self._key, case_token),
                "opt_out_token": opt_out_token,
            }
        )
        opt_out = urlencode({"case": case_token, "token": opt_out_token})
        return EmailMessage(
            email,
            "Any updates on your ParkingBreaker appeal?",
            "Hello,\n\n"
            "Has the agency responded to your First-Class Mail appeal? Please tell us if you "
            "learned that the letter was returned or sent to a different office, if the agency "
            "acknowledged your appeal or asked for more information, and if it sent a decision.\n\n"
            "You can share what you know now and add an update later. If you have a notice, "
            "you can upload a photo in your ParkingBreaker account.\n\n"
            f"Share a private update: {self._origin}/outcomes/pilot#{feedback}\n\n"
            "We schedule reminders 14 and 30 days after the first documented mailing handoff "
            "or USPS scan. To stop them, "
            f"open {self._origin}/outcomes/opt-out#{opt_out}\n\n"
            "ParkingBreaker",
        )

    def _send(self, message: EmailMessage, key: str) -> EmailOutcome:
        try:
            outcome = self._client.send(message, key)
            if not isinstance(outcome, EmailOutcome):
                raise ValueError("invalid email outcome")
        except Exception:
            return EmailOutcome("uncertain", reason=EmailReason.TRANSPORT_ERROR)
        return outcome
