"""Redacted status-level support projection built strictly from the backend store.

The projection carries case state, payment state, mail class, tracking number (if any),
recorded return cause and remedy, stop reason/fixed copy, and recorded dates. It NEVER
includes ticket facts (citation number, plate, location, agency name, or mailing address).

Separately, SupportCaseContext extracts forbidden ticket terms privately so the
deterministic post-send guard can reject any draft that leaks a ticket detail.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from parkingbreaker.case_state import CaseState, HardStop, StopReason
from parkingbreaker.case_status import CaseStatus, case_status
from parkingbreaker.identity_store import _stored_time
from parkingbreaker.lob_run import pacific
from parkingbreaker.lob_submission import Accepted
from parkingbreaker.mail_config import MailClass
from parkingbreaker.payment_state import PaymentEffect, PaymentState
from parkingbreaker.returned_mail import RemedyKind, ReturnCause
from parkingbreaker.stop_reasons import stop_message
from parkingbreaker.transaction_store import SqliteTransactionStore


def format_cents(cents: int) -> str:
    dollars = cents / 100
    if cents % 100 == 0:
        return f"${cents // 100}"
    return f"${dollars:.2f}"


@dataclass(frozen=True)
class SupportProjection:
    """Redacted case status snapshot passed to the support reply engine/model."""

    support_code: str
    case_state: CaseState
    payment_state: PaymentState | None
    overall_status: CaseStatus
    needs_operator: bool
    amount_cents: int
    captured_cents: int | None
    refunded_cents: int | None
    refund_is_full: bool | None
    payment_link_sent: bool
    mail_class: MailClass | None
    accepted_for_mailing_date: str | None
    tracking_number: str | None
    usps_delivery_reported_date: str | None
    return_cause: ReturnCause | None
    return_remedy_kind: RemedyKind | None
    return_refund_cents: int | None
    return_remails_allowed: bool
    remails_used: int
    stop_reason: StopReason | None
    stop_hard_stop: HardStop | None
    stop_fixed_copy: str | None

    def allowed_dates(self) -> frozenset[str]:
        dates = set()
        if self.accepted_for_mailing_date:
            dates.add(self.accepted_for_mailing_date)
        if self.usps_delivery_reported_date:
            dates.add(self.usps_delivery_reported_date)
        return frozenset(dates)

    def allowed_amount_cents(self) -> frozenset[int]:
        amounts: set[int] = set()
        if self.amount_cents >= 0:
            amounts.add(self.amount_cents)
        if self.captured_cents is not None:
            amounts.add(self.captured_cents)
        if self.refunded_cents is not None:
            amounts.add(self.refunded_cents)
        if self.return_refund_cents is not None and self.return_refund_cents > 0:
            amounts.add(self.return_refund_cents)
        return frozenset(amounts)

    def to_model_dict(self) -> dict[str, Any]:
        """Exact JSON-serializable redacted view allowed in model prompts."""
        return {
            "support_code": self.support_code,
            "case_state": self.case_state.value,
            "payment_state": None if self.payment_state is None else self.payment_state.value,
            "overall_status": self.overall_status.value,
            "needs_operator": self.needs_operator,
            "amount_usd": format_cents(self.amount_cents),
            "captured_usd": None
            if self.captured_cents is None
            else format_cents(self.captured_cents),
            "refunded_usd": None
            if self.refunded_cents is None
            else format_cents(self.refunded_cents),
            "refund_is_full": self.refund_is_full,
            "payment_link_sent": self.payment_link_sent,
            "mail_class": None if self.mail_class is None else self.mail_class.value,
            "accepted_for_mailing_date": self.accepted_for_mailing_date,
            "tracking_number": self.tracking_number,
            "usps_delivery_reported_date": self.usps_delivery_reported_date,
            "return_cause": None if self.return_cause is None else self.return_cause.value,
            "return_remedy_kind": (
                None if self.return_remedy_kind is None else self.return_remedy_kind.value
            ),
            "return_refund_usd": (
                None
                if self.return_refund_cents is None or self.return_refund_cents == 0
                else format_cents(self.return_refund_cents)
            ),
            "return_remails_allowed": self.return_remails_allowed,
            "remails_used": self.remails_used,
            "stop_reason": None if self.stop_reason is None else self.stop_reason.value,
            "stop_fixed_copy": self.stop_fixed_copy,
        }


@dataclass(frozen=True, repr=False)
class SupportCaseContext:
    """Backend-only context pairing the redacted projection with routing/guard metadata."""

    case_token: str
    workspace_token: str
    owner_id: str | None
    bound_email: str | None
    projection: SupportProjection
    forbidden_ticket_terms: tuple[str, ...]

    def __repr__(self) -> str:
        return "SupportCaseContext(<redacted>)"


def _parse_iso_pacific_date(raw: str | None) -> str | None:
    if not raw:
        return None
    parsed: datetime | None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed = _stored_time(raw)
    if parsed is None or parsed.tzinfo is None:
        return None
    return pacific(parsed).date().isoformat()


def _extract_contact_and_forbidden_terms(
    store: SqliteTransactionStore, case_token: str, workspace_token: str
) -> tuple[str | None, str | None, tuple[str, ...]]:
    """Resolve the bound email, owner id, and private ticket terms for post-send leak checks."""
    db = store._require_open()
    owner_row = db.execute(
        "SELECT owner FROM pb_intake_owners WHERE workspace_token=?",
        (workspace_token,),
    ).fetchone()
    owner_id = None if owner_row is None else str(owner_row[0])

    bound_email: str | None = None
    if owner_id is not None:
        acc_row = db.execute(
            "SELECT email FROM pb_identity_accounts WHERE customer_id=?",
            (owner_id,),
        ).fetchone()
        if acc_row is not None and isinstance(acc_row[0], str) and acc_row[0].strip():
            bound_email = acc_row[0].strip().lower()

    terms: list[str] = []

    def _add_term(value: object) -> None:
        if isinstance(value, str):
            cleaned = value.strip()
            if len(cleaned) >= 4:
                terms.append(cleaned)

    intake_row = db.execute(
        "SELECT intake_json, agency_json FROM pb_intake_records "
        "WHERE case_token=? OR workspace_token=?",
        (case_token, workspace_token),
    ).fetchone()
    if intake_row is not None:
        try:
            intake_data = json.loads(intake_row[0]) if intake_row[0] else {}
        except (ValueError, TypeError):
            intake_data = {}
        if isinstance(intake_data, dict):
            contact = intake_data.get("contact")
            if isinstance(contact, dict):
                if bound_email is None and isinstance(contact.get("email"), str):
                    candidate = contact["email"].strip().lower()
                    if candidate:
                        bound_email = candidate
                _add_term(contact.get("line1"))
                _add_term(contact.get("street"))
                _add_term(contact.get("address"))
                _add_term(contact.get("city"))
            ticket = intake_data.get("ticket")
            if isinstance(ticket, dict):
                for key in (
                    "citation_number",
                    "plate",
                    "license_plate",
                    "location",
                    "violation_location",
                    "issued_city",
                    "issuer_name",
                    "agency_name",
                ):
                    _add_term(ticket.get(key))
            for key in (
                "citation_number",
                "plate",
                "license_plate",
                "location",
                "issued_city",
                "issuer_name",
                "agency_name",
                "line1",
            ):
                _add_term(intake_data.get(key))

        if intake_row[1]:
            try:
                agency_data = json.loads(intake_row[1])
            except (ValueError, TypeError):
                agency_data = {}
            if isinstance(agency_data, dict):
                for key in ("issuer_name", "name", "deciding_body", "mailing_address", "address"):
                    _add_term(agency_data.get(key))

    # Also collect destination mailing lines from prepared mail attempts
    for attempt in (1, 2):
        prepared = store.get(case_token, attempt)
        if prepared is not None:
            dest = prepared.request.recipient
            _add_term(dest.recipient)
            _add_term(dest.line1)
            _add_term(dest.city)
            ret = prepared.request.sender
            _add_term(ret.line1)

    deduped = tuple(dict.fromkeys(terms))
    return owner_id, bound_email, deduped


def _safe_stop_copy(
    reason: StopReason,
    hard_stop: HardStop | None,
    official_quote: str | None,
    forbidden_terms: tuple[str, ...],
) -> str:
    """Compute fixed stop copy via stop_reasons without leaking agency/address details."""
    if reason is StopReason.PROCESS_INCOMPATIBLE and hard_stop is not None:
        quote_clean = (official_quote or "").strip()
        lower_quote = quote_clean.lower()
        quote_leaks = not quote_clean or any(
            term.lower() in lower_quote for term in forbidden_terms
        )
        if not quote_leaks:
            return stop_message(reason, hard_stop, official_quote=quote_clean).text
        return (
            "Nothing was charged. Your work is saved. "
            "The agency requires a submission channel or condition our mailed letter "
            "cannot provide. "
            "You can check the agency's official instructions for other ways to proceed."
        )
    if reason is StopReason.NOT_YET_VERIFIED and hard_stop is not None:
        return stop_message(reason, hard_stop).text
    if reason is StopReason.BUSINESS_EXCLUSION:
        return (
            "Nothing was charged. Your work is saved. "
            "This request is outside our current service scope. "
            "You can check the agency's official instructions for other ways to proceed."
        )
    return stop_message(reason).text


def build_support_context(
    store: SqliteTransactionStore, support_code: str
) -> SupportCaseContext | None:
    """Resolve support_code -> SupportCaseContext using only authoritative store state."""
    case_token = store.case_token_for_support_code(support_code)
    if case_token is None:
        return None
    case = store.get_case(case_token)
    if case is None:
        return None

    db = store._require_open()
    owner_id, bound_email, forbidden_terms = _extract_contact_and_forbidden_terms(
        store, case_token, case.workspace_token
    )

    overall = case_status(case.letter_state, case.payment_state)
    needs_operator = overall is CaseStatus.NEEDS_OPERATOR

    # Read case history for recorded dates, stop reasons, return cause, and refund amounts
    history_rows = db.execute(
        "SELECT machine, event_name, event_json, occurred_at FROM pb_case_history "
        "WHERE case_token=? ORDER BY revision ASC",
        (case_token,),
    ).fetchall()

    accepted_date: str | None = None
    tracking_number: str | None = None
    delivered_date: str | None = None
    stop_reason: StopReason | None = None
    stop_hard_stop: HardStop | None = None
    stop_quote: str | None = None
    refunded_cents: int | None = None
    captured_cents: int | None = (
        case.amount_cents if case.payment_state is PaymentState.CAPTURED else None
    )
    return_cause: ReturnCause | None = None

    for _machine, event_name, event_json_raw, occurred_at in history_rows:
        try:
            payload = json.loads(event_json_raw) if event_json_raw else {}
        except (ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        if event_name == "SubmitConfirmed":
            accepted_date = _parse_iso_pacific_date(str(occurred_at))
            if (
                isinstance(payload.get("tracking_number"), str)
                and payload["tracking_number"].strip()
            ):
                tracking_number = payload["tracking_number"].strip()
        elif event_name == "DeliveryProofStored":
            delivered_date = _parse_iso_pacific_date(str(occurred_at))
        elif event_name == "ReturnConfirmed":
            raw_cause = payload.get("cause")
            if isinstance(raw_cause, str):
                with contextlib.suppress(ValueError):
                    return_cause = ReturnCause(raw_cause)
        elif event_name == "RefundIssued":
            raw_ref = payload.get("amount_cents")
            raw_cap = payload.get("captured_cents")
            if isinstance(raw_ref, int):
                refunded_cents = raw_ref
            if isinstance(raw_cap, int):
                captured_cents = raw_cap
        elif event_name == "CaptureSucceeded":
            captured_cents = case.amount_cents
        elif event_name in {
            "Stopped",
            "AuthorizationExpired",
            "TimingRefused",
            "DestinationRefused",
            "LetterNotSubmittable",
            "WindowExpired",
            "SubmitRejected",
            "ConfirmedNotSent",
        }:
            raw_reason = payload.get("reason")
            if isinstance(raw_reason, str):
                with contextlib.suppress(ValueError):
                    stop_reason = StopReason(raw_reason)
            elif event_name == "WindowExpired":
                stop_reason = StopReason.WINDOW_EXPIRED
            elif event_name == "AuthorizationExpired":
                stop_reason = StopReason.AUTHORIZATION_EXPIRED
            elif event_name == "DestinationRefused":
                stop_reason = StopReason.DESTINATION_NOT_CONFIRMED
            elif event_name == "LetterNotSubmittable":
                stop_reason = StopReason.LETTER_NOT_SUBMITTABLE
            elif event_name == "SubmitRejected":
                stop_reason = StopReason.LOB_REJECTED
            elif event_name == "ConfirmedNotSent":
                stop_reason = StopReason.NOT_SENT

            raw_hs = payload.get("hard_stop")
            if isinstance(raw_hs, str):
                with contextlib.suppress(ValueError):
                    stop_hard_stop = HardStop(raw_hs)
            raw_quote = payload.get("official_quote")
            if isinstance(raw_quote, str) and raw_quote.strip():
                stop_quote = raw_quote.strip()

    # Check prepared mail attempts & submission cursors for mail class and latest tracking number
    # TODO: Give support the same charge explanation as customer status. Read the basis from
    # the original paid attempt so a free re-mail's tracking history cannot explain that charge.
    # Have the notification worker send an update when a committed change needs customer action.
    # Key delivery by case, transition and notice kind so replay sends one correction request.
    mail_class: MailClass | None = None
    for attempt in (case.attempt, 1):
        prepared = store.get(case_token, attempt)
        if prepared is not None and mail_class is None:
            mail_class = prepared.request.options.mail_class
        cursor = store.submission_cursor(case_token, attempt)
        if (
            cursor is not None
            and isinstance(cursor.outcome, Accepted)
            and cursor.outcome.tracking_number
        ):
            tracking_number = cursor.outcome.tracking_number

    # Check whether a write-off payment link effect was emitted
    link_row = db.execute(
        "SELECT 1 FROM pb_pending_effects WHERE case_token=? AND effect=?",
        (case_token, PaymentEffect.SEND_PAYMENT_LINK.value),
    ).fetchone()
    payment_link_sent = link_row is not None

    # Check returned-mail remedy record
    remedy_row = db.execute(
        "SELECT responsibility, kind, refund_cents, remails FROM pb_return_remedies "
        "WHERE case_token=? ORDER BY attempt DESC LIMIT 1",
        (case_token,),
    ).fetchone()
    return_remedy_kind: RemedyKind | None = None
    return_refund_cents: int | None = None
    return_remails_allowed = False
    if remedy_row is not None:
        resp_raw, kind_raw, rem_refund_raw, remails_raw = remedy_row
        if isinstance(resp_raw, str):
            with contextlib.suppress(ValueError):
                return_cause = ReturnCause(resp_raw)
        if isinstance(kind_raw, str):
            with contextlib.suppress(ValueError):
                return_remedy_kind = RemedyKind(kind_raw)
        if isinstance(rem_refund_raw, int):
            return_refund_cents = rem_refund_raw
        return_remails_allowed = bool(remails_raw) and case.attempt < 2

    remails_used = 1 if case.attempt >= 2 else 0

    refund_is_full: bool | None = None
    if case.payment_state is PaymentState.REFUNDED and refunded_cents is not None:
        base_captured = captured_cents if captured_cents is not None else case.amount_cents
        refund_is_full = refunded_cents >= base_captured

    stop_fixed_copy: str | None = None
    if case.letter_state is CaseState.STOPPED_NO_CHARGE:
        effective_reason = stop_reason or StopReason.UNRESOLVABLE_FACT
        stop_fixed_copy = _safe_stop_copy(
            effective_reason, stop_hard_stop, stop_quote, forbidden_terms
        )

    projection = SupportProjection(
        support_code=support_code.strip().upper(),
        case_state=case.letter_state,
        payment_state=case.payment_state,
        overall_status=overall,
        needs_operator=needs_operator,
        amount_cents=case.amount_cents,
        captured_cents=captured_cents,
        refunded_cents=refunded_cents,
        refund_is_full=refund_is_full,
        payment_link_sent=payment_link_sent,
        mail_class=mail_class,
        accepted_for_mailing_date=accepted_date,
        tracking_number=tracking_number,
        usps_delivery_reported_date=delivered_date,
        return_cause=return_cause,
        return_remedy_kind=return_remedy_kind,
        return_refund_cents=return_refund_cents,
        return_remails_allowed=return_remails_allowed,
        remails_used=remails_used,
        stop_reason=stop_reason,
        stop_hard_stop=stop_hard_stop,
        stop_fixed_copy=stop_fixed_copy,
    )

    return SupportCaseContext(
        case_token=case_token,
        workspace_token=case.workspace_token,
        owner_id=owner_id,
        bound_email=bound_email,
        projection=projection,
        forbidden_ticket_terms=forbidden_terms,
    )
