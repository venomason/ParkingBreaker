"""Owner capture policy, through real SDK parsing, signatures and reopened SQLite."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.effect_dispatch import dispatch_once
from parkingbreaker.lob_submission import (
    FALLBACK_LEAD,
    Accepted,
    Canceled,
    CancelOutcome,
    CancelRequest,
    LetterEvidence,
    MetadataLookup,
    Uncertain,
    cancel_resolution,
    fallback_resolution,
    hold_releasable,
    resolve_lookup,
)
from parkingbreaker.lob_webhook import USPS_SCAN_WEBHOOKS, has_usps_scan
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.stop_reasons import stop_message
from parkingbreaker.submission_records import decode_outcome, outcome_json
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_case_service import service
from test_submission_coordinator import NOW, prepared
from test_t17_mail_evidence import (
    ORIGINAL,
    OfflineLob,
    clock_at,
    current_case,
    cursor_for,
    list_response,
    new_id,
    resource,
    submit,
    unavailable,
)


def scan() -> list[dict[str, object]]:
    return [{"type": "normal", "name": "In Transit", "time": NOW.isoformat()}]


def test_bare_strings_never_supply_capture_evidence() -> None:
    for anchor in (None, ORIGINAL):
        assert isinstance(resolve_lookup(ORIGINAL, anchor), Uncertain)


@pytest.mark.parametrize("invalid", ["id", "token", "deleted", "failed"])
def test_lookup_scan_never_overrides_wrong_or_failed_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    payload = resource(tracking_events=scan())
    if invalid == "id":
        payload["id"] = "ltr_Other"
    elif invalid == "token":
        payload["metadata"] = {"submission_token": "other"}
    elif invalid == "deleted":
        payload["deleted"] = True
    else:
        payload["status"] = "failed"
    wire = OfflineLob(
        monkeypatch,
        lambda method, path: (
            (200, resource())
            if method == "POST"
            else list_response([payload])
            if "?" in path
            else (200, payload)
        ),
    )
    payments = FakePaymentClient()
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        submit(store, wire, case, NOW)
    with SqliteTransactionStore(path) as store:
        dispatch_once(store, wire.client, payments, lambda: NOW + timedelta(hours=23), new_id)
        assert isinstance(cursor_for(store, case).outcome, Uncertain)
        assert not payments.calls
    assert len(wire.creates) == 1


@pytest.mark.parametrize("kind", ["normal", "certified"])
@pytest.mark.parametrize("name", ["Received", "In production", "Mailed", "Issue", "unknown"])
def test_lookup_rejects_non_scan_event_types(kind: str, name: str) -> None:
    assert not has_usps_scan([{"type": kind, "name": name}])


@pytest.mark.parametrize("event", sorted(USPS_SCAN_WEBHOOKS))
def test_each_supported_scan_webhook_reaches_existing_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    wire = OfflineLob(monkeypatch, lambda *_: (200, resource()))
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        submit(store, wire, case, NOW)
    payments = FakePaymentClient()
    real = service(path, payments)
    object.__setattr__(real._deps, "clock", lambda: NOW)
    object.__setattr__(real._deps, "lob_webhook_secret", "secret")
    object.__setattr__(real._deps, "new_id", new_id)
    payload, headers = signed_event(resource(), event, NOW)
    try:
        assert real.webhook("lob", payload, headers)
    finally:
        real._store.close()
    for _ in range(3):
        with SqliteTransactionStore(path) as store:
            dispatch_once(store, wire.client, payments, lambda: NOW, new_id)
    assert payments.captures == 1
    assert len(wire.creates) == 1


def test_legacy_accepted_codec_is_not_capture_eligible() -> None:
    old = '{"kind":"accepted","letter_id":"ltr_Original"}'
    decoded = decode_outcome(old)
    assert outcome_json(decoded) == old  # Preserve canonical historical bytes.
    assert decoded != Accepted(ORIGINAL)  # New acceptance must carry explicit evidence.


@pytest.mark.parametrize("matches", [0, 1, 2])
def test_unanchored_recovery_adopts_only_one_token_match_with_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matches: int
) -> None:
    payload = resource(tracking_events=scan())
    rows = [payload, resource(id="ltr_Other", tracking_events=scan())][:matches]
    wire = OfflineLob(
        monkeypatch,
        lambda method, path: (
            unavailable(method, path)
            if method == "POST"
            else list_response(rows)
            if "?" in path
            else (200, payload)
        ),
    )
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        submit(store, wire, case, NOW)
    with SqliteTransactionStore(path) as store:
        cursor = submit(store, wire, case, NOW + timedelta(hours=23))
        assert isinstance(cursor.outcome, Accepted) is (matches == 1)
        if matches == 1:
            assert isinstance(cursor.outcome, Accepted)
            assert cursor.outcome.letter_id == ORIGINAL
    assert len(wire.creates) == 1


def test_typed_lookup_records_unique_token_identity_and_refuses_other_anchor() -> None:
    found = MetadataLookup(ORIGINAL, evidence=LetterEvidence.PRODUCTION)
    assert resolve_lookup(found) == Accepted(ORIGINAL)
    assert isinstance(resolve_lookup(found, "ltr_Other"), Uncertain)


def legacy_cursor(store: SqliteTransactionStore, *, capture_pending: bool = False) -> str:
    """Seed historical acceptance, with or without an already requested capture."""
    case = prepared(store)
    with store.submission_ownership():
        claimed, _kind = store.claim_submission(
            case.case_token, 1, case.revision, None, now=NOW, operation_id=new_id()
        )
        store.finish_submission(
            claimed,
            Accepted(ORIGINAL) if capture_pending else Uncertain("legacy render", ORIGINAL, True),
            now=NOW,
            effect_ids=(),
            new_effect_id=new_id,
        )
    db = store._require_open()
    for table in ("pb_submission_cursors", "pb_submission_operations"):
        rows = db.execute(f"SELECT rowid, outcome_json FROM {table}").fetchall()
        for rowid, encoded in rows:
            if encoded is not None:
                payload = {"kind": "accepted", "letter_id": ORIGINAL}
                db.execute(
                    f"UPDATE {table} SET outcome_json=? WHERE rowid=?",
                    (json.dumps(payload, sort_keys=True, separators=(",", ":")), rowid),
                )
    return case.case_token


def test_legacy_accepted_without_pending_capture_needs_fresh_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = resource()
    wire = OfflineLob(
        monkeypatch,
        lambda _method, path: list_response([payload]) if "?" in path else (200, payload),
    )
    payments = FakePaymentClient()
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        token = legacy_cursor(store)
    for _ in range(2):
        with SqliteTransactionStore(path) as store:
            case = store.get_case(token)
            assert case is not None
            assert case.payment_state is ps.PaymentState.AUTHORIZED
            dispatch_once(store, wire.client, payments, lambda: NOW + timedelta(minutes=1), new_id)
            assert not payments.calls
    payload["tracking_events"] = scan()
    for _ in range(3):
        with SqliteTransactionStore(path) as store:
            dispatch_once(store, wire.client, payments, lambda: NOW + timedelta(hours=1), new_id)
    assert payments.captures == 1
    assert not wire.creates


def test_legacy_accepted_without_evidence_keeps_hold_when_lookup_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire = OfflineLob(monkeypatch, unavailable)
    path = tmp_path / "cases.sqlite"
    payments = FakePaymentClient()
    with SqliteTransactionStore(path) as store:
        token = legacy_cursor(store)
    for _ in range(3):
        with SqliteTransactionStore(path) as store:
            case = store.get_case(token)
            assert case is not None
            now = case.authorization_expires_at - ps.EXPIRY_MARGIN - FALLBACK_LEAD
            dispatch_once(store, wire.client, payments, clock_at(now), new_id)
            assert current_case(store, case).letter_state is cs.CaseState.SUBMITTING
            assert current_case(store, case).payment_state is ps.PaymentState.AUTHORIZED
    assert not payments.calls or all(call[1] == "auth" for call in payments.calls)
    assert not payments.captures
    assert not payments.releases
    assert not [r for r in wire.requests if r[0] == "DELETE" and r[1].endswith(ORIGINAL)]


def test_legacy_capture_pending_without_evidence_refuses_store_open(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        token = legacy_cursor(store, capture_pending=True)
        case = store.get_case(token)
        assert case is not None
        assert case.payment_state is ps.PaymentState.CAPTURE_PENDING
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
        before = tuple(database.iterdump())
    for _ in range(2):
        with pytest.raises(StorageCorruption, match=r"^legacy capture without mail evidence$"):
            SqliteTransactionStore(path)
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
            assert tuple(database.iterdump()) == before


@pytest.mark.parametrize("boundary", ["claim", "response"])
def test_restart_after_pending_create_intent_or_response_keeps_one_letter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    payload = resource()
    wire = OfflineLob(
        monkeypatch,
        lambda _method, path: list_response([payload]) if "?" in path else (200, payload),
    )
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        if boundary == "claim":
            with store.submission_ownership():
                store.claim_submission(
                    case.case_token, 1, case.revision, None, now=NOW, operation_id=new_id()
                )
        else:

            def crash(*_args: object, **_kwargs: object) -> None:
                raise KeyboardInterrupt("crash after provider response")

            with monkeypatch.context() as patch:
                patch.setattr(store, "finish_submission", crash)
                with pytest.raises(KeyboardInterrupt):
                    submit(store, wire, case, NOW)
    payload["tracking_events"] = scan()
    payments = FakePaymentClient()
    for _ in range(3):
        with SqliteTransactionStore(path) as store:
            dispatch_once(store, wire.client, payments, lambda: NOW + timedelta(hours=23), new_id)
    assert payments.captures == 1
    assert len(wire.creates) == (1 if boundary == "response" else 0)


def test_webhook_acceptance_and_dedup_roll_back_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire = OfflineLob(monkeypatch, lambda *_: (200, resource()))
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        submit(store, wire, case, NOW)
    real = service(path, FakePaymentClient())
    object.__setattr__(real._deps, "clock", lambda: NOW)
    object.__setattr__(real._deps, "new_id", new_id)
    object.__setattr__(real._deps, "lob_webhook_secret", "secret")
    payload, headers = signed_event(resource(), "letter.in_transit", NOW)

    def crash(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt("crash inside atomic acceptance")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(real._store, "finish_submission", crash)
            with pytest.raises(KeyboardInterrupt):
                real.webhook("lob", payload, headers)
        assert isinstance(cursor_for(real._store, case).outcome, Uncertain)
        assert not real._store.tracking_payloads(case.case_token)
    finally:
        real._store.close()

    real = service(path, FakePaymentClient())
    object.__setattr__(real._deps, "clock", lambda: NOW)
    object.__setattr__(real._deps, "new_id", new_id)
    object.__setattr__(real._deps, "lob_webhook_secret", "secret")
    try:
        assert real.webhook("lob", payload, headers)
        assert not real.webhook("lob", payload, headers)
    finally:
        real._store.close()


def signed_event(
    body: dict[str, object], event_type: str, at: datetime
) -> tuple[bytes, dict[str, str]]:
    payload = json.dumps(
        {"id": "evt_scan", "event_type": {"id": event_type}, "body": body}
    ).encode()
    stamp = str(int(at.timestamp()))
    signature = hmac.new(b"secret", stamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    return payload, {"lob-signature": signature, "lob-signature-timestamp": stamp}


@pytest.mark.parametrize("invalid", [None, "other", "token", "deleted", "failed", "signature"])
def test_signed_scan_for_pending_exact_letter_captures_once_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str | None
) -> None:
    wire = OfflineLob(monkeypatch, lambda *_: (200, resource()))
    path = tmp_path / "cases.sqlite"
    payments = FakePaymentClient()
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        submit(store, wire, case, NOW)
    real = service(path, payments)
    object.__setattr__(real._deps, "clock", lambda: NOW)
    object.__setattr__(real._deps, "lob", wire.client)
    object.__setattr__(real._deps, "lob_webhook_secret", "secret")
    object.__setattr__(real._deps, "new_id", new_id)
    body = resource()
    if invalid == "other":
        body["id"] = "ltr_Other"
    elif invalid == "token":
        body["metadata"] = {"submission_token": "other"}
    elif invalid == "deleted":
        body["deleted"] = True
    elif invalid == "failed":
        body["status"] = "failed"
    payload, headers = signed_event(body, "letter.in_transit", NOW)
    if invalid == "signature":
        headers["lob-signature"] = "bad"
    try:
        if invalid:
            with suppress(ServiceConflict):
                real.webhook("lob", payload, headers)
        else:
            assert real.webhook("lob", payload, headers)
            assert not real.webhook("lob", payload, headers)
    finally:
        real._store.close()
    for _ in range(3):
        with SqliteTransactionStore(path) as store:
            dispatch_once(store, wire.client, payments, lambda: NOW, new_id)
    assert payments.captures == (0 if invalid else 1)
    assert len(wire.creates) == 1
    assert all(call[1] == "auth" and call[2] == case.amount_cents for call in payments.calls)


@pytest.mark.parametrize("event", ["letter.created", "letter.rendered_pdf", "letter.mailed"])
def test_non_scan_webhooks_never_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    wire = OfflineLob(monkeypatch, lambda *_: (200, resource()))
    path = tmp_path / "cases.sqlite"
    payments = FakePaymentClient()
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        submit(store, wire, case, NOW)
    real = service(path, payments)
    object.__setattr__(real._deps, "clock", lambda: NOW)
    object.__setattr__(real._deps, "lob_webhook_secret", "secret")
    payload, headers = signed_event(resource(), event, NOW)
    try:
        with suppress(ServiceConflict):
            real.webhook("lob", payload, headers)
        dispatch_once(real._store, wire.client, payments, lambda: NOW, new_id)
        assert not payments.captures
    finally:
        real._store.close()


DELETED = {"id": ORIGINAL, "deleted": True, "object": "letter_deleted"}
NOT_DELETABLE = (
    422,
    {"error": {"message": "not deletable", "status_code": 422, "code": "not_deletable"}},
)
CANCEL_ANSWERS: dict[str, tuple[int, object]] = {
    "confirmed": (200, DELETED),
    "refused": NOT_DELETABLE,
    "unavailable": (503, {"error": {"code": "internal_server_error"}}),
    "not_found": (404, {"error": {"code": "not_found"}}),
    "other_422": (422, {"error": {"code": "invalid"}}),
    "not_deleted": (200, {**DELETED, "deleted": False}),
    "wrong_id": (200, {**DELETED, "id": "ltr_Other"}),
}


def cancel_requests(wire: OfflineLob) -> list[str]:
    return [path for method, path, _headers, _body in wire.requests if method == "DELETE"]


def fallback_world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
    cancel: str = "unavailable",
) -> tuple[OfflineLob, FakePaymentClient, Path, CaseRecord, datetime]:
    """A rendered, unscanned original letter anchored at create; time is at the fallback."""

    def respond(method: str, path: str) -> tuple[int, object]:
        if method == "DELETE":
            return CANCEL_ANSWERS[cancel]
        if method == "POST":
            return 200, resource()
        return list_response([payload]) if "?" in path else (200, payload)

    wire = OfflineLob(monkeypatch, respond)
    payments = FakePaymentClient()
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        submit(store, wire, case, NOW)
    now = case.authorization_expires_at - ps.EXPIRY_MARGIN - FALLBACK_LEAD
    return wire, payments, path, case, now


def run_dispatches(
    path: Path, wire: OfflineLob, payments: FakePaymentClient, now: datetime, times: int = 4
) -> None:
    for _ in range(times):
        with SqliteTransactionStore(path) as store:
            dispatch_once(store, wire.client, payments, lambda: now, new_id)


def stop_event_of(store: SqliteTransactionStore, case: CaseRecord) -> str:
    return store.transition_history(case.case_token)[-2].event_name


def assert_hold_kept(
    path: Path, case: CaseRecord, wire: OfflineLob, payments: FakePaymentClient
) -> None:
    with SqliteTransactionStore(path) as store:
        current = current_case(store, case)
        assert current.letter_state is cs.CaseState.SUBMITTING
        assert current.payment_state is ps.PaymentState.AUTHORIZED
        assert isinstance(cursor_for(store, case).outcome, Uncertain)
    assert not payments.captures
    assert not payments.releases
    assert all(call[1] == "auth" for call in payments.calls)
    assert len(wire.creates) == 1


def test_no_scan_before_fallback_time_waits_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire, payments, path, case, now = fallback_world(
        tmp_path, monkeypatch, resource(send_date=(NOW + timedelta(hours=1)).isoformat())
    )
    before = now - timedelta(seconds=1)
    run_dispatches(path, wire, payments, before, 1)
    assert_hold_kept(path, case, wire, payments)
    with SqliteTransactionStore(path) as store:
        assert cursor_for(store, case).next_at == now
    assert not cancel_requests(wire)


@pytest.mark.parametrize(
    "late",
    [timedelta(0), FALLBACK_LEAD, FALLBACK_LEAD + ps.EXPIRY_MARGIN, timedelta(days=366)],
    ids=["fallback", "capture_margin", "authorization_expiry", "long_outage"],
)
@pytest.mark.parametrize("send_date", ["future", "missing"])
def test_cancel_confirmed_releases_the_hold_and_never_captures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, send_date: str, late: timedelta
) -> None:
    payload = resource()
    if send_date == "future":
        payload["send_date"] = (NOW + timedelta(days=400)).isoformat()
    wire, payments, path, case, now = fallback_world(tmp_path, monkeypatch, payload, "confirmed")
    run_dispatches(path, wire, payments, now + late)
    with SqliteTransactionStore(path) as store:
        current = current_case(store, case)
        assert current.letter_state is cs.CaseState.STOPPED_NO_CHARGE
        assert current.payment_state is ps.PaymentState.RELEASED
        assert isinstance(cursor_for(store, case).outcome, Canceled)
        assert stop_event_of(store, case) == "ConfirmedNotSent"
    assert "was not sent" in stop_message(cs.ConfirmedNotSent.reason).text
    assert not payments.captures
    assert payments.releases == 1
    # Exactly one cancel request, for exactly the original letter.
    assert cancel_requests(wire) == [f"/v1/letters/{ORIGINAL}"]
    assert len(wire.creates) == 1


@pytest.mark.parametrize(
    "cancel", ["unavailable", "not_found", "other_422", "not_deleted", "wrong_id"]
)
@pytest.mark.parametrize("late", [timedelta(0), timedelta(days=366)], ids=["fallback", "outage"])
def test_ambiguous_cancel_keeps_the_hold_and_never_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: str, late: timedelta
) -> None:
    wire, payments, path, case, now = fallback_world(tmp_path, monkeypatch, resource(), cancel)
    run_dispatches(path, wire, payments, now + late)
    assert_hold_kept(path, case, wire, payments)
    with SqliteTransactionStore(path) as store:
        outcome = cursor_for(store, case).outcome
        assert isinstance(outcome, Uncertain)
        assert outcome.letter_id == ORIGINAL
        # An unconfirmed answer is retried at the ordinary lookup pace, not every tick.
        assert cursor_for(store, case).next_at > now + late
    assert cancel_requests(wire)


@pytest.mark.parametrize("late", [timedelta(0), timedelta(days=366)], ids=["fallback", "outage"])
def test_rendered_letter_past_its_cancellation_window_captures_without_canceling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, late: timedelta
) -> None:
    payload = resource(send_date=(NOW - timedelta(hours=1)).isoformat())
    wire, payments, path, case, now = fallback_world(tmp_path, monkeypatch, payload, "confirmed")
    run_dispatches(path, wire, payments, now + late)
    with SqliteTransactionStore(path) as store:
        assert current_case(store, case).letter_state is cs.CaseState.IN_TRANSIT
        outcome = cursor_for(store, case).outcome
        assert isinstance(outcome, Accepted)
        assert outcome.evidence is LetterEvidence.EXPIRY_FALLBACK
    assert not payments.releases
    assert not cancel_requests(wire)
    assert payments.captures == (0 if late else 1)


@pytest.mark.parametrize("state", ["failed", "deleted"])
def test_failed_or_deleted_original_releases_without_a_cancel_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    payload = resource(**({"status": "failed"} if state == "failed" else {"deleted": True}))
    wire, payments, path, case, now = fallback_world(tmp_path, monkeypatch, payload)
    run_dispatches(path, wire, payments, now)
    with SqliteTransactionStore(path) as store:
        current = current_case(store, case)
        assert current.letter_state is cs.CaseState.STOPPED_NO_CHARGE
        assert current.payment_state is ps.PaymentState.RELEASED
        assert isinstance(cursor_for(store, case).outcome, Canceled)
    assert not payments.captures
    assert payments.releases == 1
    assert not cancel_requests(wire)


def test_lookup_unavailable_at_fallback_keeps_the_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire, payments, path, case, now = fallback_world(tmp_path, monkeypatch, resource())
    wire.respond = unavailable
    run_dispatches(path, wire, payments, now)
    assert_hold_kept(path, case, wire, payments)
    assert not cancel_requests(wire)


def test_scan_evidence_at_fallback_after_margin_is_never_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire, payments, path, case, now = fallback_world(
        tmp_path, monkeypatch, resource(tracking_events=scan())
    )
    run_dispatches(path, wire, payments, now + FALLBACK_LEAD + ps.EXPIRY_MARGIN)
    assert not payments.releases
    with SqliteTransactionStore(path) as store:
        assert current_case(store, case).letter_state is cs.CaseState.IN_TRANSIT
    assert not cancel_requests(wire)


def test_restart_between_cancel_request_and_outcome_never_mails_or_cancels_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deleted = {"done": False}
    payload = resource()

    def respond(method: str, path: str) -> tuple[int, object]:
        if method == "DELETE":
            deleted["done"] = True  # Lob processed it, but the response never arrived.
            return CANCEL_ANSWERS["unavailable"]
        if deleted["done"]:
            # Lob no longer lists a deleted letter; retrieval by id still shows it.
            return (200, {**payload, "deleted": True}) if "?" not in path else list_response([])
        return list_response([payload]) if "?" in path else (200, payload)

    wire = OfflineLob(monkeypatch, respond)
    payments = FakePaymentClient()
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        submit(store, wire, case, NOW)
    now = case.authorization_expires_at - ps.EXPIRY_MARGIN - FALLBACK_LEAD
    run_dispatches(path, wire, payments, now, 1)
    assert_hold_kept(path, case, wire, payments)
    assert len(cancel_requests(wire)) == 1
    with SqliteTransactionStore(path) as store:
        retry_at = cursor_for(store, case).next_at
    run_dispatches(path, wire, payments, retry_at)
    with SqliteTransactionStore(path) as store:
        current = current_case(store, case)
        assert current.letter_state is cs.CaseState.STOPPED_NO_CHARGE
        assert current.payment_state is ps.PaymentState.RELEASED
        assert isinstance(cursor_for(store, case).outcome, Canceled)
    assert len(cancel_requests(wire)) == 1
    assert len(wire.creates) == 1
    assert payments.releases == 1
    assert not payments.captures


def test_confirmed_cancel_is_the_only_release_after_a_create() -> None:
    sent = datetime(2026, 1, 1, tzinfo=NOW.tzinfo)
    safe = sent + timedelta(days=1)
    for evidence in (LetterEvidence.CREATED, LetterEvidence.RENDERED):
        step = fallback_resolution(
            MetadataLookup(ORIGINAL, evidence=evidence), ORIGINAL, sent, safe
        )
        assert step == CancelRequest(ORIGINAL)
    assert fallback_resolution(
        MetadataLookup(ORIGINAL, evidence=LetterEvidence.VOIDED), ORIGINAL, sent, safe
    ) == Canceled(ORIGINAL)
    # Another letter's id is never cancelled or released against the original anchor.
    other = MetadataLookup("ltr_Other", evidence=LetterEvidence.CREATED)
    assert isinstance(fallback_resolution(other, ORIGINAL, sent, safe), Uncertain)
    assert hold_releasable(cancel_resolution(CancelOutcome.CANCELED, ORIGINAL))
    for result in (CancelOutcome.NOT_CANCELABLE, CancelOutcome.UNKNOWN):
        assert not hold_releasable(cancel_resolution(result, ORIGINAL))
    assert decode_outcome(outcome_json(Canceled(ORIGINAL))) == Canceled(ORIGINAL)


def test_sdk_cancel_adapter_confirms_only_the_exact_deleted_letter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for answer, expected in (
        ("confirmed", CancelOutcome.CANCELED),
        ("refused", CancelOutcome.NOT_CANCELABLE),
        ("other_422", CancelOutcome.UNKNOWN),
        ("unavailable", CancelOutcome.UNKNOWN),
        ("not_found", CancelOutcome.UNKNOWN),
        ("not_deleted", CancelOutcome.UNKNOWN),
        ("wrong_id", CancelOutcome.UNKNOWN),
    ):
        wire = OfflineLob(monkeypatch, lambda *_, a=answer: CANCEL_ANSWERS[a])
        assert wire.client.cancel_letter(ORIGINAL) is expected
        assert [(m, p) for m, p, _h, _b in wire.requests] == [("DELETE", f"/v1/letters/{ORIGINAL}")]
