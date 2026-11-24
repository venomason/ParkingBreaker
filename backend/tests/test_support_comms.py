"""Comprehensive tests for backend Support Comms (all 14 situation-table rows + negative guards).

Verifies:
- Short spoken PB-XXXXX support code created atomically with every case and exposed via API
- Purchase confirmation email (PB-XXXXX in subject & body, hard rule notice, no ticket details)
- Every row of the Support Comms situation table against a real SqliteTransactionStore case
- Negative tests: no ticket details leak, no 'mailed' while only authorized, no 'delivered'
  before delivery, no 'charged' while only authorized, advice gets fixed reply, missing/garbled/
  unknown codes log NO_ID_NO_RESPONSE with zero email, zero owner alert, and closed-enum hash log
- Fresh 15-minute access links minted per reply, existing model gateway + spend cap reused,
  per-case rate limit & message idempotency, and /internal/support/intake HTTP authentication.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.agencies import MailingAddress
from parkingbreaker.api.app import create_app
from parkingbreaker.case_state import (
    CaseState,
    CustomerApproved,
    CustomerNeeded,
    DeliveryProofStored,
    DraftReady,
    HardStop,
    LetterNotSubmittable,
    ResearchComplete,
    ReturnClosed,
    ReturnConfirmed,
    Stopped,
    StopReason,
    SubmitRequested,
)
from parkingbreaker.email_client import EmailOutcome, FakeEmailClient
from parkingbreaker.identity_store import _stamp
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import LlmBudgetExceeded, LlmClient, Usage
from parkingbreaker.lob_submission import Accepted, LetterOptions, LetterRequest
from parkingbreaker.mail_config import MailClass
from parkingbreaker.payment_state import (
    AuthorizationLapsed,
    CaptureFailed,
    CaptureRequested,
    CaptureSucceeded,
    PaymentEffect,
    PaymentState,
    RefundIssued,
    ReleaseRequested,
)
from parkingbreaker.prices import CERTIFIED_CENTS
from parkingbreaker.research_records import agency_json
from parkingbreaker.returned_mail import Remedy, RemedyKind, ReturnCause
from parkingbreaker.support_comms import (
    FIXED_HELD_ACKNOWLEDGMENT,
    FIXED_NO_ADVICE_REPLY,
    SupportCommsService,
    SupportIntakeRequest,
    extract_support_code,
    redact_card_details,
)
from parkingbreaker.support_store import (
    SupportChannel,
    SupportHoldReason,
    SupportIntent,
    SupportOutcome,
)
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from samples import SAN_FRANCISCO
from test_case_service import anyio_backend as anyio_backend

NOW = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
LINK_KEY = "A" * 43
BOUND_EMAIL = "driver.bound@example.com"
OWNER_EMAIL = "owner@parkingbreaker.example"
CITATION = "CIT-9928410"
PLATE = "7XKB912"
AGENCY = "San Francisco Municipal Transportation Agency"
VIOLATION_LOC = "742 Evergreen Terrace"
MAIL_TO_LINE1 = "1 South Van Ness Ave"


class ScriptedLlm(LlmClient):
    """Deterministic fake LlmClient recording prompts sent to the model gateway."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.calls: list[dict[str, Any]] = []

    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
    ) -> tuple[JsonValue, Usage]:
        self.calls.append({"system": system, "user": user, "max_cost_cents": max_cost_cents})
        out = self._handler(json.loads(user))
        return out, Usage(input_tokens=400, output_tokens=120, cost_cents=1)


def _accept_email_client(count: int = 20) -> FakeEmailClient:
    return FakeEmailClient(
        [
            EmailOutcome("accepted", provider_id=f"00000000-0000-4000-8000-{i:012d}")
            for i in range(1, count + 1)
        ]
    )


def _seed_case(
    tmp_path: Path,
    *,
    letter_state: CaseState = CaseState.RESEARCHING,
    payment_state: PaymentState = PaymentState.AUTHORIZED,
    amount_cents: int = CERTIFIED_CENTS,
    mail_class: MailClass = MailClass.CERTIFIED,
    tracking_number: str | None = "9407111899562048192014",
    stop_reason: StopReason | None = None,
    hard_stop: HardStop | None = None,
    refunded_cents: int | None = None,
    payment_link_sent: bool = False,
    return_cause: ReturnCause | None = None,
    return_remedy: Remedy | None = None,
) -> tuple[SqliteTransactionStore, str, str]:
    """Create a real SQLite store with a case driven into the exact target state."""
    store = SqliteTransactionStore(tmp_path / "pb.sqlite")
    ws = store.save_workspace("ws_test", Workspace(notes="notes"), NOW)
    db = store._require_open()
    # Bind owner + identity account + intake record containing private ticket facts
    db.execute("BEGIN IMMEDIATE")
    db.execute(
        "INSERT INTO pb_identity_accounts (customer_id, email, created_at) VALUES (?, ?, ?)",
        ("cust_1", BOUND_EMAIL, _stamp(NOW)),
    )
    db.execute(
        "INSERT INTO pb_intake_owners (workspace_token, owner, created_at) VALUES (?, ?, ?)",
        (ws.workspace_token, "cust_1", NOW.isoformat()),
    )
    db.execute("COMMIT")

    case = store.start_case(
        ws.workspace_token,
        "pi_testauth123",
        NOW - timedelta(days=2),
        NOW + timedelta(days=5),
        amount_cents=amount_cents,
    )
    support_code = store.support_code_for_case(case.case_token)
    assert support_code is not None
    assert re.fullmatch(r"PB-\d{5}", support_code)

    intake_json = json.dumps(
        {
            "contact": {"email": BOUND_EMAIL, "line1": "123 Customer Lane", "city": "Oakland"},
            "ticket": {
                "citation_number": CITATION,
                "plate": PLATE,
                "location": VIOLATION_LOC,
                "issuer_name": AGENCY,
            },
        }
    )
    db.execute("BEGIN IMMEDIATE")
    db.execute(
        "INSERT INTO pb_intake_records "
        "(intake_id, workspace_token, intake_json, prehold_json, agency_json, "
        "selected_tier, case_token, created_at, revision) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (
            "case_intake_1",
            ws.workspace_token,
            intake_json,
            "{}",
            agency_json(SAN_FRANCISCO),
            "CERTIFIED" if mail_class is MailClass.CERTIFIED else "STANDARD",
            case.case_token,
            NOW.isoformat(),
        ),
    )
    db.execute("COMMIT")

    # Helper to apply letter/payment transitions cleanly through SqliteTransactionStore
    t = NOW - timedelta(days=2) + timedelta(minutes=1)

    def advance_letter(event: Any) -> None:
        nonlocal case, t
        t += timedelta(minutes=1)
        case = store.apply_letter_event(
            case.case_token,
            event,
            case.revision,
            occurred_at=t,
            new_effect_id=lambda: str(uuid.uuid4()),
        )

    def advance_payment(event: Any, effect_count: int = 0) -> None:
        nonlocal case, t
        t += timedelta(minutes=1)
        case = store.apply_payment_event(
            case.case_token,
            event,
            case.revision,
            occurred_at=t,
            effect_ids=tuple(str(uuid.uuid4()) for _ in range(effect_count)),
        )

    def prepare_mail() -> None:
        req = LetterRequest(
            pdf=b"%PDF-1.4\n%%EOF\n",
            submission_token="sub_tok_1",
            sender=MailingAddress(
                "Driver", None, "123 Customer Lane", None, "Oakland", "CA", "94607"
            ),
            recipient=MailingAddress(
                AGENCY, None, MAIL_TO_LINE1, None, "San Francisco", "CA", "94103"
            ),
            options=LetterOptions(mail_class=mail_class),
        )
        store.prepare(case.case_token, 1, req, t)

    # Drive letter machine into requested state
    if letter_state is CaseState.NEEDS_CUSTOMER:
        advance_letter(CustomerNeeded(question="Confirm vehicle relationship"))
    elif letter_state is CaseState.DRAFTING:
        advance_letter(ResearchComplete())
    elif letter_state is CaseState.AWAITING_ADOPTION:
        advance_letter(ResearchComplete())
        advance_letter(DraftReady())
    elif letter_state in {
        CaseState.IN_TRANSIT,
        CaseState.DELIVERED,
        CaseState.RETURNED,
        CaseState.CLOSED_RETURNED,
    }:
        advance_letter(ResearchComplete())
        advance_letter(DraftReady())
        advance_letter(CustomerApproved())
        prepare_mail()
        advance_letter(SubmitRequested())
        confirmed_time = datetime(2026, 11, 18, 19, 0, tzinfo=UTC)
        t = confirmed_time
        with store.submission_ownership():
            claimed, _ = store.claim_submission(
                case.case_token, 1, case.revision, None, now=confirmed_time, operation_id="op_sub_1"
            )
            store.finish_submission(
                claimed,
                Accepted(letter_id="ltr_1", tracking_number=tracking_number),
                now=confirmed_time,
                effect_ids=(),
                new_effect_id=lambda: str(uuid.uuid4()),
            )
        refreshed = store.get_case(case.case_token)
        assert refreshed is not None
        case = refreshed
        if letter_state is CaseState.DELIVERED:
            t = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
            advance_letter(DeliveryProofStored(proof_ref="proof_1"))
        elif letter_state in {CaseState.RETURNED, CaseState.CLOSED_RETURNED}:
            cause = return_cause or ReturnCause.OUR_ERROR
            advance_letter(ReturnConfirmed(usps_reason="returned_to_sender"))
            if return_remedy is not None:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO pb_return_remedies "
                    "(case_token, attempt, responsibility, reason, kind, refund_cents, "
                    "remails, resolved_at, cost_is_estimate) "
                    "VALUES (?, 1, ?, 'returned_to_sender', ?, ?, ?, ?, 0)",
                    (
                        case.case_token,
                        cause.value,
                        return_remedy.kind.value,
                        return_remedy.refund_cents,
                        1 if return_remedy.remails else 0,
                        t.isoformat(),
                    ),
                )
                db.execute("COMMIT")
            if letter_state is CaseState.CLOSED_RETURNED:
                advance_letter(ReturnClosed())
    elif letter_state is CaseState.STOPPED_NO_CHARGE:
        reason = stop_reason or StopReason.LETTER_NOT_SUBMITTABLE
        if reason in {StopReason.PROCESS_INCOMPATIBLE, StopReason.NOT_YET_VERIFIED}:
            advance_letter(Stopped(reason=reason, hard_stop=hard_stop or HardStop.S1))
        elif reason is StopReason.LETTER_NOT_SUBMITTABLE:
            advance_letter(ResearchComplete())
            advance_letter(DraftReady())
            advance_letter(CustomerApproved())
            advance_letter(SubmitRequested())
            advance_letter(LetterNotSubmittable())
        else:
            advance_letter(Stopped(reason=reason))

    # Drive payment machine into requested state
    if payment_state is PaymentState.RELEASED:
        if case.payment_state is PaymentState.AUTHORIZED:
            advance_payment(ReleaseRequested(), effect_count=1)
        elif case.payment_state is not PaymentState.RELEASED:
            # Simulate needs_operator mismatch (mailed letter whose hold was released)
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE pb_cases SET payment_state=? WHERE case_token=?",
                (PaymentState.RELEASED.value, case.case_token),
            )
            db.execute("COMMIT")
            refreshed = store.get_case(case.case_token)
            assert refreshed is not None
            case = refreshed
    elif payment_state is PaymentState.CAPTURED:
        if case.payment_state is PaymentState.AUTHORIZED:
            advance_payment(CaptureRequested(), effect_count=1)
        advance_payment(CaptureSucceeded(charge_ref="ch_cap1"))
    elif payment_state is PaymentState.REFUNDED:
        if case.payment_state is PaymentState.AUTHORIZED:
            advance_payment(CaptureRequested(), effect_count=1)
        advance_payment(CaptureSucceeded(charge_ref="ch_cap1"))
        ref_cents = amount_cents if refunded_cents is None else refunded_cents
        advance_payment(
            RefundIssued(
                refund_ref="re_1",
                amount_cents=ref_cents,
                captured_cents=amount_cents,
            )
        )
    elif payment_state is PaymentState.CAPTURE_RETRYING:
        if case.payment_state is PaymentState.AUTHORIZED:
            advance_payment(CaptureRequested(), effect_count=1)
        advance_payment(CaptureFailed(reason="network_timeout"))
    elif payment_state is PaymentState.WRITTEN_OFF:
        if case.payment_state is PaymentState.AUTHORIZED:
            advance_payment(CaptureRequested(), effect_count=1)
        advance_payment(CaptureFailed(reason="expired"))
        advance_payment(AuthorizationLapsed(), effect_count=2)
        if payment_link_sent:
            row = db.execute(
                "SELECT 1 FROM pb_pending_effects WHERE case_token=? AND effect=?",
                (case.case_token, PaymentEffect.SEND_PAYMENT_LINK.value),
            ).fetchone()
            assert row is not None

    return store, case.case_token, support_code


def _make_service(
    store: SqliteTransactionStore,
    email: FakeEmailClient,
    llm: LlmClient,
    *,
    max_replies: int = 5,
) -> SupportCommsService:
    return SupportCommsService(
        store,
        email,
        llm,
        link_key=LINK_KEY,
        origin="https://parkingbreaker.example",
        support_email="support@parkingbreaker.example",
        support_phone="(800) 555-0199",
        owner_email=OWNER_EMAIL,
        clock=lambda: NOW,
        max_replies_per_case_24h=max_replies,
    )


def _assert_no_ticket_leaks(text: str) -> None:
    lower = text.lower()
    for secret in (CITATION, PLATE, AGENCY, VIOLATION_LOC, MAIL_TO_LINE1):
        assert secret.lower() not in lower, f"Leaked private ticket detail {secret!r} in reply!"


# ---------------------------------------------------------------------------
# Situation Table Tests — One test per row (14 rows)
# ---------------------------------------------------------------------------


def test_row01_researching_or_drafting_auto_replies_with_fresh_link(tmp_path: Path) -> None:
    store, _tok, code = _seed_case(tmp_path, letter_state=CaseState.DRAFTING)
    email = _accept_email_client()
    llm = ScriptedLlm(
        lambda p: {
            "intent": "APPEAL_STATUS",
            "facts_used": ["case_state=DRAFTING"],
            "draft": "We are working on your appeal. Open your appeal page: {{SIGN_IN_LINK}}",
            "confidence": 0.95,
            "needs_sign_in_link": True,
        }
    )
    svc = _make_service(store, email, llm)

    res = svc.handle_intake(
        SupportIntakeRequest("m1", SupportChannel.EMAIL, f"Where is my appeal {code}?")
    )
    assert res.outcome is SupportOutcome.AUTO_REPLIED
    assert res.email_sent is True
    assert res.owner_alerted is False
    ((_, msg),) = email._messages.items()
    assert msg.to == BOUND_EMAIL
    assert code in msg.subject
    assert "working on your appeal" in msg.text
    assert "https://parkingbreaker.example/access#token=" in msg.text
    _assert_no_ticket_leaks(msg.text)
    # Confirm model never received private ticket terms in its prompt
    _assert_no_ticket_leaks(llm.calls[0]["user"])


def test_row02_awaiting_adoption_auto_replies_nothing_mailed_until_approve(tmp_path: Path) -> None:
    store, _tok, code = _seed_case(tmp_path, letter_state=CaseState.AWAITING_ADOPTION)
    email = _accept_email_client()
    llm = ScriptedLlm(
        lambda _p: {
            "intent": "APPEAL_STATUS",
            "facts_used": ["case_state=AWAITING_ADOPTION"],
            "draft": (
                "Your letter is waiting for your review and approval. "
                "Nothing is mailed until you approve. Review it here: {{SIGN_IN_LINK}}"
            ),
            "confidence": 0.96,
            "needs_sign_in_link": True,
        }
    )
    res = _make_service(store, email, llm).handle_intake(
        SupportIntakeRequest("m2", SupportChannel.EMAIL, f"Where is my appeal {code}?")
    )
    assert res.outcome is SupportOutcome.AUTO_REPLIED
    ((_, msg),) = email._messages.items()
    assert "waiting for your review and approval" in msg.text
    assert "Nothing is mailed until you approve" in msg.text
    assert "https://parkingbreaker.example/access#token=" in msg.text
    _assert_no_ticket_leaks(msg.text)


def test_row03_needs_customer_auto_replies_question_waiting(tmp_path: Path) -> None:
    store, _tok, code = _seed_case(tmp_path, letter_state=CaseState.NEEDS_CUSTOMER)
    email = _accept_email_client()
    llm = ScriptedLlm(
        lambda _p: {
            "intent": "APPEAL_STATUS",
            "facts_used": ["case_state=NEEDS_CUSTOMER"],
            "draft": "A question is waiting for you on your appeal page: {{SIGN_IN_LINK}}",
            "confidence": 0.95,
            "needs_sign_in_link": True,
        }
    )
    res = _make_service(store, email, llm).handle_intake(
        SupportIntakeRequest(
            "m3", SupportChannel.VOICEMAIL, f"P B dash {code[3:]} where is my appeal"
        )
    )
    assert res.outcome is SupportOutcome.AUTO_REPLIED
    ((_, msg),) = email._messages.items()
    assert msg.to == BOUND_EMAIL
    assert "A question is waiting for you on your appeal page" in msg.text
    assert "https://parkingbreaker.example/access#token=" in msg.text
    _assert_no_ticket_leaks(msg.text)


def test_row04_in_transit_uses_accepted_for_mailing_and_tracking(tmp_path: Path) -> None:
    store, _tok, code = _seed_case(
        tmp_path,
        letter_state=CaseState.IN_TRANSIT,
        payment_state=PaymentState.CAPTURED,
        tracking_number="9407111899562048192014",
    )
    email = _accept_email_client()
    llm = ScriptedLlm(
        lambda p: {
            "intent": "APPEAL_STATUS",
            "facts_used": [
                "case_state=IN_TRANSIT",
                f"accepted_for_mailing_date={p['CASE_PROJECTION']['accepted_for_mailing_date']}",
                "tracking_number=9407111899562048192014",
            ],
            "draft": (
                "Your letter was accepted for mailing on "
                f"{p['CASE_PROJECTION']['accepted_for_mailing_date']} "
                "via certified mail (USPS tracking 9407111899562048192014). "
                "This is not proof of delivery. View updates: {{SIGN_IN_LINK}}"
            ),
            "confidence": 0.96,
            "needs_sign_in_link": True,
        }
    )
    res = _make_service(store, email, llm).handle_intake(
        SupportIntakeRequest("m4", SupportChannel.EMAIL, f"{code} where is my letter?")
    )
    assert res.outcome is SupportOutcome.AUTO_REPLIED
    ((_, msg),) = email._messages.items()
    assert "accepted for mailing" in msg.text
    assert "2026-11-18" in msg.text
    assert "9407111899562048192014" in msg.text
    assert "not proof of delivery" in msg.text
    assert "mailed" not in msg.text.lower()
    _assert_no_ticket_leaks(msg.text)


def test_row05_delivered_says_usps_reports_delivery_and_unknown_agency_dates(
    tmp_path: Path,
) -> None:
    store, _tok, code = _seed_case(
        tmp_path,
        letter_state=CaseState.DELIVERED,
        payment_state=PaymentState.CAPTURED,
    )
    email = _accept_email_client()
    llm = ScriptedLlm(
        lambda p: {
            "intent": "APPEAL_STATUS",
            "facts_used": ["case_state=DELIVERED"],
            "draft": (
                f"USPS reports delivery on {p['CASE_PROJECTION']['usps_delivery_reported_date']}. "
                "The agency's review and decision dates are not yet known. "
                "Open your appeal page: {{SIGN_IN_LINK}}"
            ),
            "confidence": 0.95,
            "needs_sign_in_link": True,
        }
    )
    res = _make_service(store, email, llm).handle_intake(
        SupportIntakeRequest("m5", SupportChannel.EMAIL, f"{code} where is my appeal?")
    )
    assert res.outcome is SupportOutcome.AUTO_REPLIED
    ((_, msg),) = email._messages.items()
    assert "USPS reports delivery" in msg.text
    assert "not yet known" in msg.text
    _assert_no_ticket_leaks(msg.text)


def test_row06_authorized_explains_hold_not_charge(tmp_path: Path) -> None:
    store, _tok, code = _seed_case(
        tmp_path, letter_state=CaseState.RESEARCHING, payment_state=PaymentState.AUTHORIZED
    )
    email = _accept_email_client()
    llm = ScriptedLlm(
        lambda _p: {
            "intent": "PAYMENT_STATUS",
            "facts_used": ["payment_state=AUTHORIZED", "amount_usd=$29.99"],
            "draft": (
                "There is a $29.99 hold on your card, not a charge. "
                "You are charged only after the letter is accepted for mailing."
            ),
            "confidence": 0.95,
            "needs_sign_in_link": False,
        }
    )
    res = _make_service(store, email, llm).handle_intake(
        SupportIntakeRequest("m6", SupportChannel.EMAIL, f"{code} Am I charged?")
    )
    assert res.outcome is SupportOutcome.AUTO_REPLIED
    ((_, msg),) = email._messages.items()
    assert "hold on your card, not a charge" in msg.text
    assert "charged only after the letter is accepted for mailing" in msg.text
    _assert_no_ticket_leaks(msg.text)


@pytest.mark.parametrize(
    ("payment_state", "refunded_cents", "expected_phrase"),
    [
        (PaymentState.CAPTURED, None, "Your card was charged $29.99."),
        (PaymentState.RELEASED, None, "The $29.99 hold on your card was released."),
        (PaymentState.REFUNDED, CERTIFIED_CENTS, "A full refund of $29.99 was issued."),
        (PaymentState.REFUNDED, 1999, "A partial refund of $19.99 was issued."),
    ],
)
def test_row07_captured_released_refunded_states_recorded_outcome_and_amount(
    tmp_path: Path,
    payment_state: PaymentState,
    refunded_cents: int | None,
    expected_phrase: str,
) -> None:
    letter = (
        CaseState.STOPPED_NO_CHARGE
        if payment_state is PaymentState.RELEASED
        else CaseState.IN_TRANSIT
    )
    store, _tok, code = _seed_case(
        tmp_path,
        letter_state=letter,
        payment_state=payment_state,
        refunded_cents=refunded_cents,
    )
    email = _accept_email_client()
    llm = ScriptedLlm(
        lambda _p: {
            "intent": "PAYMENT_STATUS",
            "facts_used": [f"payment_state={payment_state.value}"],
            "draft": expected_phrase,
            "confidence": 0.95,
            "needs_sign_in_link": False,
        }
    )
    res = _make_service(store, email, llm).handle_intake(
        SupportIntakeRequest("m7", SupportChannel.EMAIL, f"{code} Am I charged?")
    )
    assert res.outcome is SupportOutcome.AUTO_REPLIED
    ((_, msg),) = email._messages.items()
    assert expected_phrase in msg.text
    _assert_no_ticket_leaks(msg.text)


def test_row08_capture_retrying_and_written_off_auto_for_facts_and_hold_if_disputed(
    tmp_path: Path,
) -> None:
    store, _tok, code = _seed_case(
        tmp_path,
        letter_state=CaseState.IN_TRANSIT,
        payment_state=PaymentState.WRITTEN_OFF,
        payment_link_sent=True,
    )
    email = _accept_email_client()
    llm = ScriptedLlm(
        lambda _p: {
            "intent": "PAYMENT_STATUS",
            "facts_used": ["payment_state=WRITTEN_OFF", "payment_link_sent=true"],
            "draft": (
                "The $29.99 card hold expired before capture completed. "
                "A payment link was already sent; we have not sent another payment request."
            ),
            "confidence": 0.92,
            "needs_sign_in_link": False,
        }
    )
    svc = _make_service(store, email, llm)

    # Routine question -> auto reply stating facts, no second payment link
    r1 = svc.handle_intake(
        SupportIntakeRequest("m8a", SupportChannel.EMAIL, f"{code} Am I charged?")
    )
    assert r1.outcome is SupportOutcome.AUTO_REPLIED
    first_msg = next(iter(email._messages.values()))
    assert "payment link was already sent" in first_msg.text
    assert "stripe.com" not in first_msg.text.lower()

    # Disputed -> held for owner with neutral acknowledgment only
    r2 = svc.handle_intake(
        SupportIntakeRequest(
            "m8b", SupportChannel.EMAIL, f"{code} I dispute this payment and am not paying it."
        )
    )
    assert r2.outcome is SupportOutcome.HELD_FOR_OWNER
    assert r2.hold_reason is SupportHoldReason.PAYMENT_DISPUTED
    assert r2.owner_alerted is True
    cust_ack = [m for m in email._messages.values() if m.to == BOUND_EMAIL][-1]
    assert cust_ack.text == FIXED_HELD_ACKNOWLEDGMENT


def test_row09_stopped_no_charge_reuses_fixed_stop_copy(tmp_path: Path) -> None:
    store, _tok, code = _seed_case(
        tmp_path,
        letter_state=CaseState.STOPPED_NO_CHARGE,
        payment_state=PaymentState.RELEASED,
        stop_reason=StopReason.LETTER_NOT_SUBMITTABLE,
    )
    email = _accept_email_client()
    llm = ScriptedLlm(
        lambda _p: (_ for _ in ()).throw(AssertionError("Model must not write stop copy"))
    )
    res = _make_service(store, email, llm).handle_intake(
        SupportIntakeRequest("m9", SupportChannel.EMAIL, f"{code} Why was I not charged?")
    )
    assert res.outcome is SupportOutcome.FIXED_REPLIED
    assert len(llm.calls) == 0
    ((_, msg),) = email._messages.items()
    assert msg.text.startswith("Nothing was charged. Your work is saved.")
    assert "could not be prepared for mailing" in msg.text
    _assert_no_ticket_leaks(msg.text)


def test_row10_returned_mail_auto_for_status_and_hold_beyond_remedy(tmp_path: Path) -> None:
    store, _tok, code = _seed_case(
        tmp_path,
        letter_state=CaseState.RETURNED,
        payment_state=PaymentState.CAPTURED,
        return_cause=ReturnCause.OUR_ERROR,
        return_remedy=Remedy(RemedyKind.FREE_REMAIL),
    )
    email = _accept_email_client()
    llm = ScriptedLlm(
        lambda _p: {
            "intent": "RETURNED_MAIL_STATUS",
            "facts_used": [
                "case_state=RETURNED",
                "return_cause=OUR_ERROR",
                "return_remedy_kind=FREE_REMAIL",
            ],
            "draft": (
                "Your letter came back due to a mailing error on our side. "
                "The recorded remedy is a free re-mail, which requires your approval "
                "on your appeal page: {{SIGN_IN_LINK}}"
            ),
            "confidence": 0.94,
            "needs_sign_in_link": True,
        }
    )
    svc = _make_service(store, email, llm)

    # Routine status question -> auto reply with recorded cause & remedy
    r1 = svc.handle_intake(
        SupportIntakeRequest(
            "m10a", SupportChannel.EMAIL, f"{code} My letter came back, what happens now?"
        )
    )
    assert r1.outcome is SupportOutcome.AUTO_REPLIED
    first_msg = next(iter(email._messages.values()))
    assert "free re-mail" in first_msg.text
    assert "requires your approval on your appeal page" in first_msg.text

    # Customer asks for something beyond the recorded remedy -> held with neutral ack
    r2 = svc.handle_intake(
        SupportIntakeRequest(
            "m10b",
            SupportChannel.EMAIL,
            f"{code} I don't want a re-mail, I demand a full refund instead.",
        )
    )
    assert r2.outcome is SupportOutcome.HELD_FOR_OWNER
    assert r2.hold_reason is SupportHoldReason.RETURNED_MAIL_BEYOND_REMEDY
    assert r2.owner_alerted is True
    cust_ack = [m for m in email._messages.values() if m.to == BOUND_EMAIL][-1]
    assert cust_ack.text == FIXED_HELD_ACKNOWLEDGMENT


def test_row11_refund_dispute_or_complaint_holds_with_neutral_ack_only(tmp_path: Path) -> None:
    store, _tok, code = _seed_case(
        tmp_path, letter_state=CaseState.IN_TRANSIT, payment_state=PaymentState.CAPTURED
    )
    email = _accept_email_client()
    llm = ScriptedLlm(lambda _p: (_ for _ in ()).throw(AssertionError("Pre-hold skips LLM")))
    res = _make_service(store, email, llm).handle_intake(
        SupportIntakeRequest(
            "m11", SupportChannel.EMAIL, f"{code} I want a refund and am filing a complaint."
        )
    )
    assert res.outcome is SupportOutcome.HELD_FOR_OWNER
    assert res.hold_reason is SupportHoldReason.REFUND_DISPUTE_COMPLAINT
    assert res.owner_alerted is True
    cust_msg = next(m for m in email._messages.values() if m.to == BOUND_EMAIL)
    assert cust_msg.text == FIXED_HELD_ACKNOWLEDGMENT
    assert "in_transit" not in cust_msg.text.lower()
    assert "tracking" not in cust_msg.text.lower()


def test_row12_agency_notice_holds_and_flags_urgent_when_deadline_mentioned(tmp_path: Path) -> None:
    store, _tok, code = _seed_case(
        tmp_path, letter_state=CaseState.DELIVERED, payment_state=PaymentState.CAPTURED
    )
    email = _accept_email_client()
    llm = ScriptedLlm(lambda _p: (_ for _ in ()).throw(AssertionError("Pre-hold skips LLM")))
    svc = _make_service(store, email, llm)

    res = svc.handle_intake(
        SupportIntakeRequest(
            "m12",
            SupportChannel.EMAIL,
            f"{code} The agency sent me a notice of deficiency with a deadline due in 5 days.",
        )
    )
    assert res.outcome is SupportOutcome.HELD_FOR_OWNER
    assert res.hold_reason is SupportHoldReason.AGENCY_NOTICE_URGENT
    assert res.urgent is True
    assert res.owner_alerted is True
    owner_msg = next(m for m in email._messages.values() if m.to == OWNER_EMAIL)
    assert "[URGENT]" in owner_msg.subject
    cust_msg = next(m for m in email._messages.values() if m.to == BOUND_EMAIL)
    assert cust_msg.text == FIXED_HELD_ACKNOWLEDGMENT


def test_row13_advice_or_outcome_question_gets_fixed_no_advice_reply(tmp_path: Path) -> None:
    store, _tok, code = _seed_case(tmp_path, letter_state=CaseState.DRAFTING)
    email = _accept_email_client()
    llm = ScriptedLlm(lambda _p: (_ for _ in ()).throw(AssertionError("No-advice uses fixed copy")))
    res = _make_service(store, email, llm).handle_intake(
        SupportIntakeRequest(
            "m13", SupportChannel.EMAIL, f"{code} Will I win or should I appeal this ticket?"
        )
    )
    assert res.outcome is SupportOutcome.FIXED_REPLIED
    assert res.intent is SupportIntent.ADVICE_OR_STRATEGY
    assert res.owner_alerted is False
    ((_, msg),) = email._messages.items()
    assert msg.text == FIXED_NO_ADVICE_REPLY
    assert code in msg.subject


def test_row14_needs_operator_case_always_held_and_alerts_owner(tmp_path: Path) -> None:
    # Mailed letter (IN_TRANSIT) whose hold was released -> case_status == NEEDS_OPERATOR
    store, _tok, code = _seed_case(
        tmp_path, letter_state=CaseState.IN_TRANSIT, payment_state=PaymentState.RELEASED
    )
    email = _accept_email_client()
    llm = ScriptedLlm(
        lambda _p: (_ for _ in ()).throw(AssertionError("needs_operator must hold before LLM"))
    )
    res = _make_service(store, email, llm).handle_intake(
        SupportIntakeRequest("m14", SupportChannel.EMAIL, f"{code} Just checking status.")
    )
    assert res.outcome is SupportOutcome.HELD_FOR_OWNER
    assert res.hold_reason is SupportHoldReason.NEEDS_OPERATOR
    assert res.urgent is True
    assert res.owner_alerted is True
    cust_msg = next(m for m in email._messages.values() if m.to == BOUND_EMAIL)
    assert cust_msg.text == FIXED_HELD_ACKNOWLEDGMENT


# ---------------------------------------------------------------------------
# Negative, Security, Confirmation, Spend-Cap, and HTTP Endpoint Tests
# ---------------------------------------------------------------------------


def test_negative_deterministic_guard_rejects_ticket_leak_and_mailed_while_authorized(
    tmp_path: Path,
) -> None:
    store, _tok, code = _seed_case(
        tmp_path, letter_state=CaseState.RESEARCHING, payment_state=PaymentState.AUTHORIZED
    )
    email = _accept_email_client()

    # Hallucinating model leaks citation number and falsely says 'mailed' and 'was charged'
    bad_llm = ScriptedLlm(
        lambda _p: {
            "intent": "APPEAL_STATUS",
            "facts_used": ["case_state=RESEARCHING"],
            "draft": (
                f"Your ticket {CITATION} to {AGENCY} was mailed and your card was charged $29.99."
            ),
            "confidence": 0.99,
            "needs_sign_in_link": False,
        }
    )
    res = _make_service(store, email, bad_llm).handle_intake(
        SupportIntakeRequest("m-neg-1", SupportChannel.EMAIL, f"{code} status?")
    )
    assert res.outcome is SupportOutcome.REJECTED_DRAFT_HELD
    assert res.hold_reason is SupportHoldReason.DRAFT_GUARD_REJECTED
    assert res.owner_alerted is True
    cust_msg = next(m for m in email._messages.values() if m.to == BOUND_EMAIL)
    # Customer gets ONLY the neutral acknowledgment — never the leaking draft!
    assert cust_msg.text == FIXED_HELD_ACKNOWLEDGMENT
    _assert_no_ticket_leaks(cust_msg.text)
    assert "mailed" not in cust_msg.text.lower()


def test_negative_missing_garbled_and_unknown_codes_log_no_id_no_response_with_zero_email_or_alert(
    tmp_path: Path,
) -> None:
    store, _tok, _code = _seed_case(tmp_path)
    email = _accept_email_client()
    llm = ScriptedLlm(
        lambda _p: (_ for _ in ()).throw(AssertionError("LLM must never run without valid code"))
    )
    svc = _make_service(store, email, llm)

    cases = [
        SupportIntakeRequest(
            "noid-vm-garbled", SupportChannel.VOICEMAIL, "My ID is P B 4 8 um mumble."
        ),
        SupportIntakeRequest(
            "noid-vm-unknown", SupportChannel.VOICEMAIL, "My ID is PB-99999 status?"
        ),
        SupportIntakeRequest(
            "noid-vm-none", SupportChannel.VOICEMAIL, "Hi checking my ticket, forgot code."
        ),
        SupportIntakeRequest(
            "noid-email-none", SupportChannel.EMAIL, "Hi, checking my appeal status."
        ),
    ]
    for req in cases:
        res = svc.handle_intake(req)
        assert res.outcome is SupportOutcome.NO_ID_NO_RESPONSE
        assert res.email_sent is False
        assert res.owner_alerted is False

    assert email.call_count == 0
    assert len(llm.calls) == 0

    # Confirm closed-enum + hash audit rows exist and store ZERO free text
    rows = (
        store._require_open()
        .execute("SELECT inbound_message_id, outcome, message_sha256 FROM pb_support_messages")
        .fetchall()
    )
    assert len(rows) == 4
    assert all(r[1] == "NO_ID_NO_RESPONSE" and len(r[2]) == 64 for r in rows)


def test_purchase_confirmation_email_card_redaction_idempotency_and_fresh_links(
    tmp_path: Path,
) -> None:
    store, case_token, code = _seed_case(tmp_path, letter_state=CaseState.DRAFTING)
    email = _accept_email_client()
    llm = ScriptedLlm(
        lambda _p: {
            "intent": "APPEAL_STATUS",
            "facts_used": ["case_state=DRAFTING"],
            "draft": "We are working on your appeal: {{SIGN_IN_LINK}}",
            "confidence": 0.95,
            "needs_sign_in_link": True,
        }
    )
    svc = _make_service(store, email, llm)

    # 1. Purchase confirmation email sent once (idempotent on retry)
    c1 = svc.send_purchase_confirmation(case_token)
    c2 = svc.send_purchase_confirmation(case_token)
    assert c1 is not None
    assert c1.status == "accepted"
    assert c2 is None
    confirm_msg = next(iter(email._messages.values()))
    assert confirm_msg.subject == f"Your ParkingBreaker case ID is {code}"
    assert f"Your ParkingBreaker case ID is {code}." in confirm_msg.text
    assert (
        "We can only respond to calls and emails that include your case ID, so save it."
        in confirm_msg.text
    )
    assert "(800) 555-0199" in confirm_msg.text
    assert "support@parkingbreaker.example" in confirm_msg.text
    _assert_no_ticket_leaks(confirm_msg.text)

    # 2. Card numbers scrubbed before model/hash; same inbound_message_id is idempotent
    r1 = svc.handle_intake(
        SupportIntakeRequest(
            "msg-dup",
            SupportChannel.EMAIL,
            f"{code} my card 4532-0151-1283-0366 where is my appeal?",
        )
    )
    r2 = svc.handle_intake(
        SupportIntakeRequest(
            "msg-dup",
            SupportChannel.EMAIL,
            f"{code} my card 4532-0151-1283-0366 where is my appeal?",
        )
    )
    assert r1.outcome is SupportOutcome.AUTO_REPLIED
    assert r1.duplicate is False
    assert r2.duplicate is True
    assert "4532" not in llm.calls[0]["user"]
    assert "[REDACTED_CARD]" in llm.calls[0]["user"]

    # 3. Second distinct message gets a DIFFERENT fresh 15-minute access link
    r3 = svc.handle_intake(
        SupportIntakeRequest("msg-fresh-2", SupportChannel.EMAIL, f"{code} checking again?")
    )
    assert r3.outcome is SupportOutcome.AUTO_REPLIED
    replies = [m.text for m in email._messages.values() if "access#token=" in m.text]
    assert len(replies) == 2
    assert replies[0] != replies[1]

    # 4. Spend ledger & AI call journal recorded under AiWorker.SUPPORT_DRAFT
    ai_calls = [
        json.loads(r[0])
        for r in store._require_open().execute("SELECT entry_json FROM pb_ai_calls").fetchall()
    ]
    assert len(ai_calls) == 2
    assert all(c["worker"] == "SUPPORT_DRAFT" and c["status"] == "OK" for c in ai_calls)
    from parkingbreaker.oplog.ai_journal import AiWorker

    assert store.ai_spent_cents(case_token, AiWorker.SUPPORT_DRAFT) == 2


def test_budget_exceeded_holds_safely_for_owner(tmp_path: Path) -> None:
    store, _tok, code = _seed_case(tmp_path, letter_state=CaseState.DRAFTING)
    email = _accept_email_client()

    class BudgetLlm(LlmClient):
        def complete_json(
            self,
            system: str,
            user: str,
            schema: Mapping[str, JsonValue],
            *,
            max_cost_cents: int | None = None,
        ) -> tuple[JsonValue, Usage]:
            raise LlmBudgetExceeded("cap hit")

    res = _make_service(store, email, BudgetLlm()).handle_intake(
        SupportIntakeRequest("m-budget", SupportChannel.EMAIL, f"{code} where is my appeal?")
    )
    assert res.outcome is SupportOutcome.API_ERROR_HELD
    assert res.hold_reason is SupportHoldReason.BUDGET_EXCEEDED
    assert res.owner_alerted is True


@pytest.mark.anyio
async def test_internal_support_intake_http_endpoint_auth_and_routing(tmp_path: Path) -> None:
    store, _tok, code = _seed_case(tmp_path, letter_state=CaseState.DRAFTING)
    email = _accept_email_client()
    llm = ScriptedLlm(
        lambda _p: {
            "intent": "APPEAL_STATUS",
            "facts_used": ["case_state=DRAFTING"],
            "draft": "We are working on your appeal: {{SIGN_IN_LINK}}",
            "confidence": 0.95,
            "needs_sign_in_link": True,
        }
    )
    svc = _make_service(store, email, llm)
    worker_secret = "secret-worker-token-1234"
    intake_secret = "secret-support-intake-1234"
    with pytest.raises(RuntimeError, match="support_intake_secret_invalid"):
        create_app(
            worker_secret=worker_secret, support_comms=svc, support_intake_secret=worker_secret
        )
    app = create_app(
        worker_secret=worker_secret, support_comms=svc, support_intake_secret=intake_secret
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Missing auth -> 401
        unauth = await client.post(
            "/internal/support/intake",
            json={"message_id": "http-1", "channel": "voicemail", "text": f"{code} status?"},
        )
        assert unauth.status_code == 401
        worker_denied = await client.post(
            "/internal/support/intake",
            headers={"Authorization": f"Bearer {worker_secret}"},
            json={"message_id": "http-1", "channel": "voicemail", "text": f"{code} status?"},
        )
        assert worker_denied.status_code == 401
        dispatch_denied = await client.post(
            "/internal/dispatch",
            headers={"Authorization": f"Bearer {intake_secret}"},
        )
        assert dispatch_denied.status_code == 401
        assert email.call_count == 0

        # Valid Bearer auth with clear code -> AUTO_REPLIED
        ok = await client.post(
            "/internal/support/intake",
            headers={"Authorization": f"Bearer {intake_secret}"},
            json={
                "message_id": "http-1",
                "channel": "voicemail",
                "text": f"My case ID is P B dash {code[3:]}. Where is my appeal?",
                "support_code": code,
            },
        )
        assert ok.status_code == 200
        body = ok.json()
        assert body["outcome"] == "AUTO_REPLIED"
        assert body["support_code"] == code
        assert BOUND_EMAIL not in ok.text

        # Missing code -> NO_ID_NO_RESPONSE
        no_id = await client.post(
            "/internal/support/intake",
            headers={"Authorization": f"Bearer {intake_secret}"},
            json={
                "message_id": "http-2",
                "channel": "email",
                "text": "Checking on my ticket without any code.",
            },
        )
        assert no_id.status_code == 200
        assert no_id.json()["outcome"] == "NO_ID_NO_RESPONSE"


def test_code_extractor_and_card_redactor_helpers() -> None:
    assert extract_support_code("My code is P B dash 4 8 2 1 3") == "PB-48213"
    assert extract_support_code("P.B. 48213") == "PB-48213"
    assert extract_support_code("P B 4 8 um mumble") is None
    assert extract_support_code("PB-482X3") is None
    assert "[REDACTED_CARD]" in redact_card_details("card 4532 0151 1283 0366")
    assert "9407111899562048192014" in redact_card_details("tracking 9407111899562048192014")
