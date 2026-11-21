from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.case_service import CaseService
from parkingbreaker.effect_dispatch import dispatch_once
from parkingbreaker.intake_store import AuthorizationAttempt
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import Canceled, LetterEvidence, LobResponse
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.research.search import SearchError
from parkingbreaker.returned_mail import RemedyKind
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_case_service import NOW as CASE_NOW
from test_case_service import Clock, researched_case, stripe
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution
from test_effect_dispatch import WORKER, approved, dispatch, payment_fixture
from test_stripe_payment import CHARGE, REF, Transport, charge, client, fixture
from test_submission_coordinator import NOW, prepared
from test_t14_authorization_recovery import BODY, RecoveryCase
from test_t14_authorization_recovery import recovery as recovery
from test_t17_capture_policy import CANCEL_ANSWERS, cancel_requests, fallback_world, run_dispatches
from test_t17_capture_policy import signed_event as scan_event
from test_t17_mail_evidence import (
    OfflineLob,
    current_case,
    cursor_for,
    list_response,
    new_id,
    resource,
)
from test_t17_ps07_scenarios import (
    assert_honest_copy,
    assert_one_capture,
    send,
    signed_event,
    usps_event,
    world,
)
from test_t20_webhook_recovery import app_for, letter_body, signed
from test_t20_webhook_recovery import prepared as timed_prepared
from test_webhook_signatures import signature


def _stripe_money(transport: Transport, captures: int, releases: int) -> None:
    assert sum(c[0] == "post" and c[1].endswith("/capture") for c in transport.calls) == captures
    assert sum(c[0] == "post" and c[1].endswith("/cancel") for c in transport.calls) == releases


def _declined() -> dict[str, object]:
    obj = fixture("requires_payment_method")
    charge(obj).update({"status": "failed", "paid": False})
    obj["last_payment_error"] = {"type": "card_error", "code": "card_declined", "charge": CHARGE}
    return obj


def _fresh_hold() -> dict[str, object]:
    obj = fixture()
    obj["id"] = "pi_FreshCard123"
    charge(obj).update({"id": "ch_FreshCard123", "payment_intent": obj["id"]})
    return obj


def _fresh_case(recovery: RecoveryCase) -> None:
    intake = recovery.real._store.get_intake(recovery.case_id)
    assert intake is not None
    assert intake.case_token is not None
    saved = recovery.real._store.get_case(intake.case_token)
    assert saved is not None
    assert saved.authorization_ref == "pi_FreshCard123"
    assert saved.letter_state is cs.CaseState.RESEARCHING
    assert saved.payment_state is ps.PaymentState.AUTHORIZED
    assert recovery.real._store._require_open().execute(
        "SELECT COUNT(*) FROM pb_cases"
    ).fetchone() == (1,)


def _stripe_event(event_id: str, kind: str, obj: dict[str, object]) -> tuple[bytes, dict[str, str]]:
    payload = json.dumps({"id": event_id, "type": kind, "data": {"object": obj}}).encode()
    offset = int((CASE_NOW - NOW).total_seconds())
    return payload, {"stripe-signature": signature(payload, offset=offset)}


def test_authorization_replay_crossing_cutoff_never_calls_stripe(
    recovery: RecoveryCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient()
    object.__setattr__(recovery.real._deps, "lob", lob)
    recovery.payments.lose_next_response = True
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    saved = recovery.attempt
    recovery.restart()
    object.__setattr__(recovery.real._deps, "lob", lob)
    recovery.clock.now = saved.created_at + timedelta(hours=22, minutes=59)
    original = recovery.real._store.claim_authorization_dispatch

    def delayed_claim(
        intake_id: str, key: str, now: datetime, replay_limit: timedelta
    ) -> AuthorizationAttempt:
        claimed = original(intake_id, key, now, replay_limit)
        recovery.clock.now += timedelta(hours=2)
        return claimed

    monkeypatch.setattr(recovery.real._store, "claim_authorization_dispatch", delayed_claim)
    recovery.payments.lose_next_response = True
    refusal: str | None = None
    try:
        recovery.real.authorize(recovery.case_id, BODY)
    except ServiceConflict as exc:
        refusal = str(exc)
    assert recovery.payments.captures == 0
    assert recovery.payments.releases == 0
    assert len(lob.calls) == 0
    recovery.assert_no_case()
    assert recovery.attempt.state == "uncertain"
    assert recovery.attempt.request_json == saved.request_json
    assert recovery.attempt.idempotency_key == saved.idempotency_key
    assert len(recovery.payments.requests) == 1, (
        "Stripe authorize replay crossed the call-time cutoff"
    )
    assert refusal == "authorization_outcome_unresolved"
    assert len(recovery.payments.authorizations) == 1


def test_402_decline_retains_identity_and_reconciles_before_new_card(
    recovery: RecoveryCase,
) -> None:
    declined = _declined()
    transport = Transport(
        {
            "error": {
                "type": "card_error",
                "code": "card_declined",
                "decline_code": "generic_decline",
                "message": "Your card was declined.",
                "payment_intent": declined,
            }
        },
        declined,
        _fresh_hold(),
        status=402,
    )
    lob = FakeLobClient()
    object.__setattr__(recovery.real._deps, "payments", client(transport))
    object.__setattr__(recovery.real._deps, "lob", lob)
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    _stripe_money(transport, captures=0, releases=0)
    assert len(lob.calls) == 0
    recovery.assert_no_case()
    assert recovery.attempt.state == "uncertain"
    assert recovery.attempt.authorization_ref == REF, (
        "HTTP 402 discarded the embedded PaymentIntent id"
    )
    replacement = s.StartAuthorization(payment_method_id="pm_fresh_card", terms_accepted=True)
    with pytest.raises(ServiceConflict, match=r"^authorization_outcome_unresolved$"):
        recovery.real.authorize(recovery.case_id, replacement)
    assert len(transport.calls) == 1
    transport.status = 200
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    assert recovery.attempt.state == "declined"
    assert transport.calls[1][:2] == ("get", f"/v1/payment_intents/{REF}")
    authorized = recovery.real.authorize(recovery.case_id, replacement)
    assert authorized.payment_intent_id == "pi_FreshCard123"
    assert authorized.status == "authorized"
    assert [call[0] for call in transport.calls] == ["post", "get", "post"]
    assert transport.calls[-1][2]["payment_method"] == ["pm_fresh_card"]
    assert transport.calls[-1][3] != transport.calls[0][3]
    assert recovery.attempt.authorization_ref == REF
    assert recovery.attempt.state == "declined"
    _fresh_case(recovery)
    _stripe_money(transport, captures=0, releases=0)
    assert len(lob.calls) == 0


def test_verified_canceled_pre_case_authorization_allows_fresh_card(recovery: RecoveryCase) -> None:
    transport = Transport(fixture(expiry=None), fixture("canceled"), _fresh_hold())
    lob = FakeLobClient()
    object.__setattr__(recovery.real._deps, "payments", client(transport))
    object.__setattr__(recovery.real._deps, "lob", lob)
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    assert recovery.attempt.authorization_ref == REF
    assert recovery.attempt.state == "uncertain"
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    assert transport.calls[1][:2] == ("get", f"/v1/payment_intents/{REF}")
    _stripe_money(transport, captures=0, releases=0)
    assert len(lob.calls) == 0
    recovery.assert_no_case()
    assert recovery.attempt.state not in {"pending", "uncertain"}, (
        "Verified canceled PaymentIntent still blocks a fresh card"
    )
    replacement = s.StartAuthorization(payment_method_id="pm_fresh_card", terms_accepted=True)
    authorized = recovery.real.authorize(recovery.case_id, replacement)
    assert authorized.status == "authorized"
    assert authorized.payment_intent_id == "pi_FreshCard123"
    assert recovery.attempt.authorization_ref == REF
    assert [call[0] for call in transport.calls] == ["post", "get", "post"]
    assert transport.calls[-1][2]["payment_method"] == ["pm_fresh_card"]
    assert transport.calls[-1][3] != transport.calls[0][3]
    _fresh_case(recovery)
    _stripe_money(transport, captures=0, releases=0)
    assert len(lob.calls) == 0


def test_confirmed_lob_cancel_then_commit_failure_recovers_one_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire, payments, path, case, now = fallback_world(tmp_path, monkeypatch, resource(), "confirmed")
    original = wire.respond
    deleted = False

    def respond(method: str, route: str) -> tuple[int, object]:
        nonlocal deleted
        if method == "DELETE":
            deleted = True
            return CANCEL_ANSWERS["confirmed"]
        if deleted:
            return list_response([]) if "?" in route else (200, resource(deleted=True))
        return original(method, route)

    wire.respond = respond
    with SqliteTransactionStore(path) as store:
        store._require_open().execute("""CREATE TRIGGER fail_cancel_commit
            BEFORE UPDATE ON pb_submission_cursors WHEN NEW.owner IS NULL
            BEGIN SELECT RAISE(ABORT, 'injected cancel commit failure'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="injected cancel commit failure"):
            dispatch_once(store, wire.client, payments, lambda: now, new_id)
        assert deleted
        assert len(cancel_requests(wire)) == 1
        assert current_case(store, case).letter_state is cs.CaseState.SUBMITTING
        assert payments.captures == payments.releases == 0
        assert cursor_for(store, case).owner is not None
        store._require_open().execute("DROP TRIGGER fail_cancel_commit")
    run_dispatches(path, wire, payments, now)
    with SqliteTransactionStore(path) as store:
        saved = current_case(store, case)
        assert saved.letter_state is cs.CaseState.STOPPED_NO_CHARGE
        assert saved.payment_state is ps.PaymentState.RELEASED
        assert isinstance(cursor_for(store, case).outcome, Canceled)
        payment = store.payment_cursor(case.case_token)
        assert payment is not None
        assert payment.done
        assert not store.pending_effects(case.case_token)
    assert len(wire.creates) == 1
    assert len(cancel_requests(wire)) == 1
    assert payments.captures == 0
    assert payments.releases == 1
    assert sum(call[0] == "release" for call in payments.calls) == 1


@pytest.mark.anyio
async def test_stripe_webhook_commit_then_dispatch_crash_recovers_from_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(200, "ltr_original", evidence=LetterEvidence.PRODUCTION)])
    api, real, case = await approved(tmp_path, monkeypatch, lob)
    payments, transport = stripe(payment_fixture(), payment_fixture(), payment_fixture("succeeded"))
    object.__setattr__(real._deps, "payments", payments)
    object.__setattr__(real._deps, "stripe_webhook_secret", "secret")
    try:
        await dispatch(api)
        token = real._intake(case).case_token
        assert token is not None
        payload, headers = _stripe_event("evt_commit_crash", "payment_intent.succeeded", fixture())

        def crash(_token: str | None) -> int:
            raise KeyboardInterrupt("after webhook commit")

        with monkeypatch.context() as patch:
            patch.setattr(real, "_dispatch", crash)
            with pytest.raises(KeyboardInterrupt, match="after webhook commit"):
                real.webhook("stripe", payload, headers)
        assert real._store._require_open().execute(
            "SELECT COUNT(*) FROM pb_webhooks WHERE provider='stripe' AND event_id=?",
            ("evt_commit_crash",),
        ).fetchone() == (1,)
        assert real.status(case).payment_state is ps.PaymentState.CAPTURE_PENDING
        _stripe_money(transport, captures=0, releases=0)
        old = real._store
        old.close()
        real._store = SqliteTransactionStore(tmp_path / "pb.sqlite")
        assert real.webhook("stripe", payload, headers) is False
        _stripe_money(transport, captures=0, releases=0)
        await dispatch(api)
        await dispatch(api)
        assert real.status(case).state is cs.CaseState.IN_TRANSIT
        assert real.status(case).payment_state is ps.PaymentState.CAPTURED
        assert not real._store.pending_effects(token)
        assert len(lob.calls) == 1
        _stripe_money(transport, captures=1, releases=0)
    finally:
        await api.aclose()
        real._store.close()


@pytest.mark.anyio
async def test_research_stop_cancel_timeout_recovers_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, real, case = await researched_case(tmp_path, monkeypatch, SearchError("rate limited"))
    clock = Clock()
    lob = FakeLobClient()
    payments, transport = stripe(
        payment_fixture(), TimeoutError("lost cancel response"), payment_fixture("canceled")
    )
    object.__setattr__(real._deps, "clock", clock)
    object.__setattr__(real._deps, "lob", lob)
    object.__setattr__(real._deps, "payments", payments)
    try:
        response = await api.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
        )
        assert response.status_code == 200
        assert response.json()["state"] == "STOPPED_NO_CHARGE"
        assert response.json()["stop"]["family"] == "NOT_YET_VERIFIED"
        await dispatch(api)
        await dispatch(api)
        token = real._intake(case).case_token
        assert token is not None
        cursor = real._store.payment_cursor(token)
        assert cursor is not None
        assert not cursor.done
        _stripe_money(transport, captures=0, releases=1)
        real._store.close()
        real._store = SqliteTransactionStore(tmp_path / "pb.sqlite")
        clock.now = cursor.next_at
        await dispatch(api)
        await dispatch(api)
        status = real.status(case)
        assert status.state is cs.CaseState.STOPPED_NO_CHARGE
        assert status.payment_state is ps.PaymentState.RELEASED
        assert status.stop is not None
        assert status.stop.family == "NOT_YET_VERIFIED"
        cursor = real._store.payment_cursor(token)
        assert cursor is not None
        assert cursor.done
        assert cursor.outcome.status == "canceled"
        assert not real._store.pending_effects(token)
        assert [call[:2] for call in transport.calls] == [
            ("get", f"/v1/payment_intents/{REF}"),
            ("post", f"/v1/payment_intents/{REF}/cancel"),
            ("get", f"/v1/payment_intents/{REF}"),
        ]
        assert len(lob.calls) == 0
        _stripe_money(transport, captures=0, releases=1)
    finally:
        await api.aclose()
        real._store.close()


@pytest.mark.anyio
async def test_out_of_order_stripe_events_use_current_original_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(200, "ltr_original")])
    api, real, case = await approved(tmp_path, monkeypatch, lob)
    transport = Transport(fixture(), fixture(), fixture("succeeded"))
    object.__setattr__(real._deps, "payments", client(transport))
    object.__setattr__(real._deps, "stripe_webhook_secret", "secret")
    object.__setattr__(real._deps, "lob_webhook_secret", "secret")
    try:
        await dispatch(api)
        token = real._intake(case).case_token
        assert token is not None
        packet = real._store.get(token, 1)
        assert packet is not None
        scan, scan_headers = scan_event(
            {
                "id": "ltr_original",
                "metadata": {"submission_token": packet.request.submission_token},
                "tracking_events": usps_event("In Transit", "normal", CASE_NOW),
            },
            "letter.in_transit",
            CASE_NOW,
        )
        assert real.webhook("lob", scan, scan_headers) is True
        assert real.status(case).payment_state is ps.PaymentState.CAPTURE_PENDING
        assert [call[:2] for call in transport.calls] == [("get", f"/v1/payment_intents/{REF}")]
        events = (
            ("evt_stale_canceled", "payment_intent.canceled", fixture("canceled")),
            ("evt_stale_failed", "payment_intent.payment_failed", _declined()),
            ("evt_stale_succeeded", "payment_intent.succeeded", fixture("succeeded")),
        )
        for index, (event_id, kind, obj) in enumerate(events):
            payload, headers = _stripe_event(event_id, kind, obj)
            assert real.webhook("stripe", payload, headers) is True
            assert real.webhook("stripe", payload, headers) is False
            assert real.status(case).payment_state is (
                ps.PaymentState.CAPTURE_PENDING if index == 0 else ps.PaymentState.CAPTURED
            )
        await dispatch(api)
        assert real.status(case).state is cs.CaseState.IN_TRANSIT
        assert real.status(case).payment_state is ps.PaymentState.CAPTURED
        assert [call[:2] for call in transport.calls] == [
            ("get", f"/v1/payment_intents/{REF}"),  # first-mail preflight
            ("get", f"/v1/payment_intents/{REF}"),
            ("post", f"/v1/payment_intents/{REF}/capture"),
        ]
        assert not real._store.pending_effects(token)
        assert len(lob.calls) == 1
        _stripe_money(transport, captures=1, releases=0)
    finally:
        await api.aclose()
        real._store.close()


def test_delayed_delivered_after_return_preserves_remedy_and_one_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailed = world(tmp_path, monkeypatch)
    transit_at = NOW + timedelta(days=1)
    returned_at = NOW + timedelta(days=2)
    try:
        transit = signed_event(
            resource(tracking_events=usps_event("In Transit", "normal", transit_at)),
            "letter.in_transit",
            event_id="evt_original_transit",
            occurred_at=transit_at,
            received_at=transit_at,
        )
        assert send(mailed, *transit, transit_at) is True
        mailed.sweep(transit_at)
        assert_one_capture(mailed)
        returned = signed_event(
            resource(
                tracking_events=usps_event(
                    "Returned to Sender", "certified", returned_at, "refused"
                )
            ),
            "letter.certified.returned_to_sender",
            event_id="evt_returned",
            occurred_at=returned_at,
            received_at=returned_at,
        )
        assert send(mailed, *returned, returned_at) is True
        mailed.app._resolve_return(mailed.case_id)
        token = mailed.case.case_token
        remedy = mailed.app._store.returned_mail_resolution(token, 1)
        assert remedy is not None
        assert remedy.remedy is not None
        assert remedy.remedy.kind is RemedyKind.PROOF_PACKET
        assert not remedy.remedy.remails
        deps = mailed.app._deps
        mailed.close()
        mailed.app = CaseService(
            replace(deps, store=SqliteTransactionStore(tmp_path / "cases.sqlite"))
        )
        for index, (kind, name) in enumerate(
            (("letter.delivered", "Delivered"), ("letter.in_transit", "In Transit")), start=1
        ):
            received = returned_at + timedelta(hours=index)
            occurred = returned_at - timedelta(hours=index)
            payload, headers = signed_event(
                resource(tracking_events=usps_event(name, "normal", occurred)),
                kind,
                event_id=f"evt_delayed_{index}",
                occurred_at=occurred,
                received_at=received,
            )
            assert send(mailed, payload, headers, received) is True
            assert send(mailed, payload, headers, received) is False
            mailed.sweep(received)
            assert mailed.app._store.returned_mail_resolution(token, 1) == remedy
            assert_one_capture(mailed)
        saved = current_case(mailed.app._store, mailed.case)
        assert saved.letter_state is cs.CaseState.CLOSED_RETURNED
        assert saved.payment_state is ps.PaymentState.CAPTURED
        assert saved.attempt == 1
        assert mailed.app._store.get(token, 2) is None
        assert (
            sum(
                row.event_name == "ReturnConfirmed"
                for row in mailed.app._store.transition_history(token)
            )
            == 1
        )
        assert len(mailed.wire.creates) == 1
        assert mailed.payments.captures == 1
        assert mailed.payments.releases == 0
        assert_honest_copy(mailed, state=cs.CaseState.CLOSED_RETURNED, delivered_received=True)
    finally:
        mailed.close()


@pytest.mark.parametrize("status", [400, 413, 422])
def test_lob_validation_rejection_releases_once_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    def reject(_method: str, _path: str) -> tuple[int, object]:
        return status, {
            "error": {"status_code": status, "code": "invalid_file", "message": "invalid"}
        }

    wire = OfflineLob(monkeypatch, reject)
    path = tmp_path / "cases.sqlite"
    payments = FakePaymentClient()
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        dispatch_once(store, wire.client, payments, lambda: NOW, new_id)
    for _ in range(4):
        with SqliteTransactionStore(path) as store:
            dispatch_once(store, wire.client, payments, lambda: NOW, new_id)
            saved = current_case(store, case)
            assert saved.letter_state is cs.CaseState.STOPPED_NO_CHARGE
            assert saved.payment_state is ps.PaymentState.RELEASED
    assert len(wire.creates) == 1
    assert payments.captures == 0
    assert payments.releases == 1
    assert sum(call[0] == "release" for call in payments.calls) == 1


@pytest.mark.anyio
async def test_unknown_webhook_types_have_explicit_safe_dispositions(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    payments = FakePaymentClient()
    lob = FakeLobClient([LobResponse(200, "ltr_early")])
    with SqliteTransactionStore(path) as store:
        case = timed_prepared(store)
        dispatch_once(store, lob, payments, lambda: NOW, new_id)
    real = app_for(path, payments)
    object.__setattr__(real._deps, "lob", lob)
    object.__setattr__(real._deps, "stripe_webhook_secret", "secret")
    try:
        body = {**letter_body(), "tracking_events": usps_event("In Transit", "normal", NOW)}
        unknown: dict[str, object] = {
            "id": "evt_unknown_lob",
            "event_type": "letter.unknown_future_type",
            "date_created": NOW.isoformat(),
            "body": body,
        }
        payload, headers = signed(unknown)
        assert real.webhook("lob", payload, headers) is True
        assert real.webhook("lob", payload, headers) is False
        saved = current_case(real._store, case)
        assert saved.letter_state is cs.CaseState.SUBMITTING
        assert saved.payment_state is ps.PaymentState.AUTHORIZED
        assert payments.captures == payments.releases == 0
        assert payments.calls == []
        unknown["id"] = "evt_unbound_unknown_lob"
        unknown["body"] = {"id": "ltr_unrelated"}
        parked, parked_headers = signed(unknown)
        assert real.webhook("lob", parked, parked_headers) is True
        assert real.webhook("lob", parked, parked_headers) is False
        assert real._store.backlog_row("lob", "evt_unbound_unknown_lob") is not None

        scan = {**unknown, "id": "evt_known_scan", "event_type": "letter.in_transit", "body": body}
        scan_payload, scan_headers = signed(scan)
        assert real.webhook("lob", scan_payload, scan_headers) is True
        assert current_case(real._store, case).payment_state is ps.PaymentState.CAPTURE_PENDING
        async with AsyncClient(
            transport=ASGITransport(app=create_app(real)), base_url="http://test"
        ) as api:
            known = json.dumps(
                {
                    "id": "evt_unknown_stripe",
                    "type": "payment_intent.unknown_future_type",
                    "data": {"object": {"id": case.authorization_ref, "status": "canceled"}},
                }
            ).encode()
            known_headers = {"stripe-signature": signature(known)}
            response = await api.post("/webhooks/stripe", content=known, headers=known_headers)
            assert response.status_code == 200
            assert response.json() == {"received": True, "duplicate": False}
            assert payments.calls == [("retrieve", case.authorization_ref, 2999, "")]
            assert current_case(real._store, case).payment_state is ps.PaymentState.CAPTURE_PENDING
            count = (
                real._store._require_open().execute("SELECT COUNT(*) FROM pb_webhooks").fetchone()
            )
            unrelated = json.dumps(
                {
                    "id": "evt_unrelated_stripe",
                    "type": "customer.unknown_future_type",
                    "data": {"object": {"id": "cus_unrelated", "object": "customer"}},
                }
            ).encode()
            rejected = await api.post(
                "/webhooks/stripe",
                content=unrelated,
                headers={"stripe-signature": signature(unrelated)},
            )
            assert rejected.status_code == 409
            assert rejected.json() == {"detail": "webhook_case_not_ready"}
            assert (
                real._store._require_open().execute("SELECT COUNT(*) FROM pb_webhooks").fetchone()
                == count
            )
            assert real._store.backlog_row("stripe", "evt_unrelated_stripe") is None
            duplicate = await api.post("/webhooks/stripe", content=known, headers=known_headers)
            assert duplicate.json() == {"received": True, "duplicate": True}
            assert payments.captures == payments.releases == 0
        real._store.close()
        real._store = SqliteTransactionStore(path)
        dispatch_once(real._store, lob, payments, lambda: NOW, new_id)
        dispatch_once(real._store, lob, payments, lambda: NOW, new_id)
        saved = current_case(real._store, case)
        assert saved.letter_state is cs.CaseState.IN_TRANSIT
        assert saved.payment_state is ps.PaymentState.CAPTURED
        assert len(lob.calls) == 1
        assert payments.captures == 1
        assert payments.releases == 0
        assert sum(call[0] == "capture" for call in payments.calls) == 1
        assert real._store.backlog_row("lob", "evt_unbound_unknown_lob") is not None
    finally:
        real._store.close()
