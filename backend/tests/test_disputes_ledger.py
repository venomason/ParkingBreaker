from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.disputes import DisputeFact, DisputeStatus, dispute_fact
from parkingbreaker.ledger import LedgerLine, LineKind, LineStatus, appeal_ledger
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.oplog.appeal_events import AppealEvent, AppealEventKind, appeal_events
from test_case_service import NOW as CASE_NOW
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution
from test_case_service import stripe
from test_effect_dispatch import approved, dispatch, payment_fixture
from test_payment_stress_failures import _stripe_money
from test_stripe_payment import REF
from test_submission_coordinator import NOW
from test_webhook_signatures import signature

T0 = datetime(2026, 10, 15, 21, 0, tzinfo=UTC)
CREATED = 1_792_000_000


def _event(
    event_id: str, kind: str, obj: dict[str, object], created: int = CREATED
) -> tuple[bytes, dict[str, str]]:
    body = {"id": event_id, "type": kind, "created": created, "data": {"object": obj}}
    payload = json.dumps(body).encode()
    offset = int((CASE_NOW - NOW).total_seconds())
    return payload, {"stripe-signature": signature(payload, offset=offset)}


def _dispute(status: str = "needs_response", **extra: object) -> dict[str, object]:
    return {
        "id": "dp_Dispute123",
        "object": "dispute",
        "amount": 2999,
        "currency": "usd",
        "status": status,
        "reason": "fraudulent",
        "payment_intent": REF,
        "charge": "ch_Original123",
        "evidence": {"customer_name": "Pat Customer", "uncategorized_text": "secret"},
        **extra,
    }


def _warning() -> dict[str, object]:
    return {
        "id": "issfr_Warning123",
        "object": "radar.early_fraud_warning",
        "fraud_type": "made_with_stolen_card",
        "actionable": True,
        "payment_intent": REF,
        "charge": "ch_Original123",
    }


def _events(*kinds: AppealEventKind) -> tuple[AppealEvent, ...]:
    return tuple(AppealEvent(kind, T0) for kind in (AppealEventKind.CARD_AUTHORIZED, *kinds))


def test_dispute_facts_keep_only_identity_status_amount_and_category() -> None:
    fact = dispute_fact("charge.dispute.created", _dispute(), CREATED)
    assert (fact.kind, fact.payment_intent, fact.status, fact.amount_cents, fact.category) == (
        "dispute",
        REF,
        DisputeStatus.NEEDS_RESPONSE,
        2999,
        "fraudulent",
    )
    stored = fact.to_json()
    for secret in ("Pat Customer", "secret", "ch_Original123", "evidence"):
        assert secret not in stored
    assert DisputeFact.from_json(stored) == fact
    warning = dispute_fact("radar.early_fraud_warning.created", _warning(), CREATED)
    assert (warning.kind, warning.status, warning.amount_cents) == ("fraud_warning", None, None)
    for reason in ("Call me at 555 0100", "pat_customer"):
        odd = dispute_fact("charge.dispute.created", _dispute(reason=reason), CREATED)
        assert odd.category == "unknown"
    named = dispute_fact(
        "radar.early_fraud_warning.created", _warning() | {"fraud_type": "pat_customer"}, CREATED
    )
    assert named.category == "unknown"
    newer_id = dispute_fact("charge.dispute.created", _dispute(id="du_NewStyle123"), CREATED)
    assert newer_id.object_ref == "du_NewStyle123"
    with pytest.raises(ValueError, match="category"):
        DisputeFact.from_json(stored.replace("fraudulent", "pat_customer"))
    legacy = json.loads(stored)
    del legacy["event_created"]
    assert DisputeFact.from_json(json.dumps(legacy)).event_created is None
    with pytest.raises(ValueError, match="event time"):
        dispute_fact("charge.dispute.created", _dispute(), None)
    with pytest.raises(ValueError, match="unknown or missing"):
        DisputeFact.from_json(json.dumps(json.loads(stored) | {"note": "x"}))


@pytest.mark.parametrize(
    "obj",
    [
        _dispute(currency="eur"),
        _dispute(status="mystery"),
        _dispute(amount=0),
        _dispute(amount="2999"),
        _dispute(id="ch_NotADispute"),
        _dispute(payment_intent="pi_bad/path"),
        _dispute(payment_intent=None),
    ],
)
def test_malformed_dispute_objects_are_rejected(obj: dict[str, object]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - each variant fails a different check
        dispute_fact("charge.dispute.created", obj, CREATED)


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_dispute_webhooks_record_once_and_never_move_money_or_mail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(200, "ltr_original", evidence=LetterEvidence.PRODUCTION)])
    api, real, case = await approved(tmp_path, monkeypatch, lob)
    payments, transport = stripe(payment_fixture(), payment_fixture("succeeded"))
    object.__setattr__(real._deps, "payments", payments)
    object.__setattr__(real._deps, "stripe_webhook_secret", "secret")
    try:
        await dispatch(api)
        await dispatch(api)
        token = real._intake(case).case_token
        assert token is not None
        assert real.status(case).payment_state is ps.PaymentState.CAPTURED
        calls = list(transport.calls)
        history = real._store.transition_history(token)
        created, created_headers = _event(
            "evt_dispute_created", "charge.dispute.created", _dispute()
        )
        assert real.webhook("stripe", created, created_headers) is True
        assert real.webhook("stripe", created, created_headers) is False
        warning, warning_headers = _event(
            "evt_warning", "radar.early_fraud_warning.created", _warning()
        )
        assert real.webhook("stripe", warning, warning_headers) is True
        lost, lost_headers = _event("evt_dispute_lost", "charge.dispute.closed", _dispute("lost"))
        assert real.webhook("stripe", lost, lost_headers) is True
        await dispatch(api)
        assert transport.calls == calls
        _stripe_money(transport, captures=1, releases=0)
        assert len(lob.calls) == 1
        assert real._store.transition_history(token) == history
        assert real.status(case).state is cs.CaseState.IN_TRANSIT
        facts = real._store.dispute_facts(token)
        assert [fact.event_type for fact in facts] == [
            "charge.dispute.created",
            "radar.early_fraud_warning.created",
            "charge.dispute.closed",
        ]
        record = real._store._payment_case(token)
        ledger = appeal_ledger(
            record.amount_cents, appeal_events(record.authorized_at, history), None, facts
        )
        assert ledger.fraud_warning is True
        assert ledger.known_net_cents == 0
        assert ledger.complete is False
    finally:
        await api.aclose()


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_disputes_for_unknown_payments_or_bad_objects_store_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, real, case = await approved(tmp_path, monkeypatch, FakeLobClient())
    object.__setattr__(real._deps, "stripe_webhook_secret", "secret")
    try:
        token = real._intake(case).case_token
        assert token is not None
        foreign, foreign_headers = _event(
            "evt_foreign", "charge.dispute.created", _dispute(payment_intent="pi_SomeoneElse9")
        )
        with pytest.raises(ServiceConflict, match=r"^webhook_case_not_ready$"):
            real.webhook("stripe", foreign, foreign_headers)
        bad, bad_headers = _event("evt_bad", "charge.dispute.created", _dispute(status="?"))
        with pytest.raises(ServiceConflict, match=r"^invalid_webhook$"):
            real.webhook("stripe", bad, bad_headers)
        assert real._store.dispute_facts(token) == ()
        assert real._store._require_open().execute(
            "SELECT COUNT(*) FROM pb_webhooks WHERE provider='stripe'"
        ).fetchone() == (0,)
    finally:
        await api.aclose()


def test_ledger_counts_revenue_at_charge_and_keeps_unbilled_costs_pending() -> None:
    held = appeal_ledger(2999, _events(AppealEventKind.MAIL_SUBMITTED), None)
    assert held.lines == ()
    sale = appeal_ledger(2999, _events(AppealEventKind.MAILED, AppealEventKind.CHARGED), None)
    assert [(line.kind, line.status, line.amount_cents) for line in sale.lines] == [
        (LineKind.SALE, LineStatus.RECORDED, 2999),
        (LineKind.PROCESSOR_FEE, LineStatus.PENDING, None),
        (LineKind.MAIL_COST, LineStatus.PENDING, None),
    ]
    assert (sale.known_net_cents, sale.complete) == (2999, False)
    refunded = appeal_ledger(2999, _events(AppealEventKind.CHARGED, AppealEventKind.REFUNDED), 1900)
    assert refunded.known_net_cents == 1099
    unknown = appeal_ledger(2999, _events(AppealEventKind.CHARGED, AppealEventKind.REFUNDED), None)
    assert unknown.lines[-1] == LedgerLine(LineKind.REFUND, LineStatus.PENDING, None)
    with pytest.raises(ValueError, match="within the charged amount"):
        appeal_ledger(2999, _events(AppealEventKind.CHARGED), 3000)
    with pytest.raises(ValueError, match="requires a recorded charge"):
        appeal_ledger(2999, _events(AppealEventKind.REFUNDED), 100)


@pytest.mark.parametrize(
    ("status", "net"),
    [("warning_needs_response", 2999), ("needs_response", 0), ("won", 2999), ("lost", 0)],
)
def test_ledger_follows_the_latest_status_of_each_dispute(status: str, net: int) -> None:
    opened = dispute_fact("charge.dispute.created", _dispute("needs_response"), CREATED)
    later = dispute_fact("charge.dispute.updated", _dispute(status), CREATED + 60)
    facts = (opened, later)
    ledger = appeal_ledger(2999, _events(AppealEventKind.CHARGED), None, facts)
    assert ledger.known_net_cents == net
    # A delayed delivery of the older event cannot roll the status back.
    late_arrival = appeal_ledger(2999, _events(AppealEventKind.CHARGED), None, (later, opened))
    assert late_arrival == ledger
    if status in {"won", "lost"}:
        # A closed outcome outranks an open status stamped in the same second.
        tie = dispute_fact("charge.dispute.updated", _dispute(status), CREATED)
        assert (
            appeal_ledger(
                2999, _events(AppealEventKind.CHARGED), None, (tie, opened)
            ).known_net_cents
            == net
        )
    fee = [line for line in ledger.lines if line.kind is LineKind.DISPUTE_FEE]
    assert len(fee) == (0 if status.startswith("warning") else 1)
    with pytest.raises(ValueError, match="requires a recorded charge"):
        appeal_ledger(2999, _events(), None, facts)
