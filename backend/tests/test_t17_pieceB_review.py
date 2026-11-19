"""T17 piece B review fixes: regressions for H1, H2, H3, M4 and M5.

Started from the independent review's executed probes
(build-kit/evidence/task-T17-2026-10-21/pieceB-review-probes/test_review_probes.py);
each test was written first and confirmed failing against the reviewed code. The
review's counterfactual send_date and In Transit probes are deliberately absent:
supervisor ruling H3 keeps those documented assumptions (flagged for T26) unchanged.

Return evidence remains an application fixture, not a Lob payload contract.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mail_samples import make_request, seed_destination, synthetic_remail_manifest
from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.address_policy import (
    FailureFacts,
    address_fingerprint,
    classify_address_failure,
    remedy_for_responsibility,
)
from parkingbreaker.case_service import CaseService
from parkingbreaker.effect_dispatch import dispatch_once
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import (
    Accepted,
    LetterEvidence,
    LobResponse,
    Uncertain,
    UspsScan,
)
from parkingbreaker.lob_webhook import scan_proof
from parkingbreaker.payment_fakes import FakePaymentClient, full_outcome
from parkingbreaker.payment_records import PaymentOutcome
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_case_service import fake_resolution, service
from test_submission_coordinator import NOW, prepared
from test_t17_capture_policy import (
    cancel_requests,
    fallback_world,
    run_dispatches,
    scan,
    signed_event,
)
from test_t17_mail_evidence import (
    CORRECTED,
    ORIGINAL,
    OfflineLob,
    current_case,
    cursor_for,
    list_response,
    new_id,
    resource,
    submit,
)
from test_transaction_lifecycle import letter

assert fake_resolution


def app_for(path: Path, payments: FakePaymentClient, now: datetime) -> CaseService:
    app = service(path, payments)
    object.__setattr__(app._deps, "clock", lambda: now)
    object.__setattr__(app._deps, "lob_webhook_secret", "secret")
    object.__setattr__(app._deps, "new_id", new_id)
    return app


def distinct_event(
    body: dict[str, object], event: str, now: datetime, event_id: str
) -> tuple[bytes, dict[str, str]]:
    payload, headers = signed_event(body, event, now)
    data = json.loads(payload)
    data["id"] = event_id
    payload = json.dumps(data).encode()
    headers["lob-signature"] = hmac.new(
        b"secret", headers["lob-signature-timestamp"].encode() + b"." + payload, hashlib.sha256
    ).hexdigest()
    return payload, headers


# --- H1: a failure webhook never releases against exact-letter USPS possession ------


@pytest.mark.parametrize("mismatch", [False, True])
def test_failure_webhook_never_releases_against_fresh_usps_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: bool
) -> None:
    wire, payments, path, case, fallback = fallback_world(tmp_path, monkeypatch, resource())
    app = app_for(path, payments, NOW)
    body = resource()
    if mismatch:
        body["metadata"] = {"submission_token": "wrong-token"}
    payload, headers = signed_event(body, "letter.failed", NOW)
    try:
        assert app.webhook("lob", payload, headers)
    finally:
        app._store.close()
    scanned = resource(tracking_events=scan())
    wire.respond = lambda _method, route: (
        list_response([scanned]) if "?" in route else (200, scanned)
    )
    run_dispatches(path, wire, payments, fallback)
    with SqliteTransactionStore(path) as store:
        current = current_case(store, case)
        outcome = cursor_for(store, case).outcome
    assert payments.releases == 0, (
        "hold released despite current exact-letter USPS possession evidence"
    )
    assert payments.captures == 1, "exact-letter scan evidence did not capture once"
    assert isinstance(outcome, Accepted)
    assert current.payment_state is ps.PaymentState.CAPTURED


def test_mismatched_failure_webhook_is_ignored_as_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure ingress keeps the scan path's exact-letter identity checks (H1a)."""
    wire, payments, path, case, fallback = fallback_world(tmp_path, monkeypatch, resource())
    app = app_for(path, payments, NOW)
    body = resource()
    body["metadata"] = {"submission_token": "wrong-token"}
    payload, headers = signed_event(body, "letter.failed", NOW)
    try:
        assert app.webhook("lob", payload, headers)
    finally:
        app._store.close()
    with SqliteTransactionStore(path) as store:
        assert not store.mail_failure_recorded(case.case_token, 1), (
            "a mismatched failure was treated as mailing evidence"
        )
        logged = json.loads(store.tracking_payloads(case.case_token)[0])
        assert logged["evidence_ignored"] == "submission_identity_mismatch"
    wire.respond = lambda _method, route: (503, {"error": {"code": "internal_server_error"}})
    run_dispatches(path, wire, payments, fallback)
    with SqliteTransactionStore(path) as store:
        assert isinstance(cursor_for(store, case).outcome, Uncertain)
    assert payments.releases == 0
    assert payments.captures == 0


def test_exact_scan_webhook_wins_over_prior_failure_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire, payments, path, _case, _ = fallback_world(tmp_path, monkeypatch, resource())
    for event_id, event in (("failure", "letter.failed"), ("scan", "letter.in_transit")):
        app = app_for(path, payments, NOW)
        payload, headers = distinct_event(resource(tracking_events=scan()), event, NOW, event_id)
        try:
            assert app.webhook("lob", payload, headers)
            assert not app.webhook("lob", payload, headers)
        finally:
            app._store.close()
    run_dispatches(path, wire, payments, NOW)
    assert payments.captures == 1
    assert payments.releases == 0


@pytest.mark.parametrize("failure", ["letter.deleted", "letter.failed"])
def test_prior_failure_never_overrides_exact_scan_or_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """H1b on the old resurrection scenario: a failure, a scan webhook, then a scan lookup."""
    payload = resource()
    wire = OfflineLob(
        monkeypatch,
        lambda _method, route: list_response([payload]) if "?" in route else (200, payload),
    )
    path = tmp_path / "cases.sqlite"
    payments = FakePaymentClient()
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        submit(store, wire, case, NOW)
    app = app_for(path, payments, NOW)
    try:
        body, headers = signed_event(resource(), failure, NOW)
        assert app.webhook("lob", body, headers)
        body, headers = distinct_event(resource(), "letter.in_transit", NOW, "evt_late")
        assert app.webhook("lob", body, headers)
    finally:
        app._store.close()
    payload["tracking_events"] = scan()
    run_dispatches(path, wire, payments, NOW + timedelta(hours=23))
    with SqliteTransactionStore(path) as store:
        assert isinstance(cursor_for(store, case).outcome, Accepted)
        assert current_case(store, case).payment_state is ps.PaymentState.CAPTURED
    assert payments.captures == 1
    assert payments.releases == 0


@pytest.mark.parametrize("conflict", ["lookup_error", "wrong_id", "wrong_token"])
def test_failure_with_unverifiable_fresh_identity_keeps_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: str
) -> None:
    wire, payments, path, case, fallback = fallback_world(tmp_path, monkeypatch, resource())
    app = app_for(path, payments, NOW)
    payload, headers = signed_event(resource(), "letter.failed", NOW)
    try:
        assert app.webhook("lob", payload, headers)
    finally:
        app._store.close()
    body = resource(tracking_events=scan())
    if conflict == "wrong_id":
        body["id"] = "ltr_Other"
    elif conflict == "wrong_token":
        body["metadata"] = {"submission_token": "other"}
    wire.respond = lambda _method, route: (
        (503, {"error": {"code": "internal_server_error"}})
        if conflict == "lookup_error"
        else list_response([body])
        if "?" in route
        else (200, body)
    )
    run_dispatches(path, wire, payments, fallback)
    with SqliteTransactionStore(path) as store:
        assert isinstance(cursor_for(store, case).outcome, Uncertain)
        assert current_case(store, case).payment_state is ps.PaymentState.AUTHORIZED
    assert payments.releases == payments.captures == 0


# --- H2: no free re-mail when the original payment was never captured ---------------


def returned_with_remail_intent(
    path: Path, lob: FakeLobClient, *, expired: bool
) -> tuple[CaseRecord, datetime]:
    """An accepted original, a confirmed return and a frozen free re-mail intent.

    The intent is written straight through the store so the dispatcher's own H2
    protection is tested without the service-level guard in front of it.
    """
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        submit(store, type("Wire", (), {"client": lob})(), case, NOW)
        case = current_case(store, case)
        assert case.payment_state is ps.PaymentState.CAPTURE_PENDING
        late = case.authorization_expires_at + timedelta(minutes=1) if expired else NOW
        case = store.apply_letter_event(
            case.case_token,
            cs.ReturnConfirmed("address"),
            case.revision,
            occurred_at=late,
            new_effect_id=new_id,
        )
        decision = seed_destination(store, case, late, CORRECTED)
        facts = FailureFacts(
            decision.case_id,
            mailed_address_fingerprint=address_fingerprint(make_request().recipient),
        )
        responsibility = classify_address_failure(
            store.frozen_destination(case.case_token, 1), facts
        )
        remedy = remedy_for_responsibility(
            responsibility,
            deadline_feasible=True,
            other_verified_address_exists=True,
            price_cents=case.amount_cents,
            mailing_cost_cents=0,
            remails_used=0,
        )
        assert remedy is not None
        assert remedy.remails
        request = replace(
            make_request(pdf=b"%PDF-1.4\ncorrected synthetic packet", token="unpaid-remail-token"),
            recipient=CORRECTED,
        )
        store.resolve_returned_mail(
            case.case_token,
            case.revision,
            responsibility,
            remedy,
            request,
            now=late,
            remail_manifest=synthetic_remail_manifest(store, case, request, late),
        )
    return case, late


def remail_lob() -> FakeLobClient:
    return FakeLobClient(
        [
            LobResponse(200, ORIGINAL, evidence=LetterEvidence.PRODUCTION),
            LobResponse(200, "ltr_Remail", evidence=LetterEvidence.RENDERED),
        ]
    )


@pytest.mark.parametrize("expired", [False, True])
def test_no_remail_when_original_payment_is_expired(tmp_path: Path, expired: bool) -> None:
    path = tmp_path / "cases.sqlite"
    lob = remail_lob()
    payments = FakePaymentClient()
    case, late = returned_with_remail_intent(path, lob, expired=expired)
    payments.object = full_outcome(PaymentOutcome("canceled", case.authorization_ref))
    for _ in range(4):
        with SqliteTransactionStore(path) as store:
            dispatch_once(store, lob, payments, lambda: late, new_id)
    with SqliteTransactionStore(path) as store:
        saved = current_case(store, case)
        assert payments.captures == 0
        assert len(lob.calls) == 1, "free re-mail was sent although the hold expired unpaid"
        assert saved.letter_state is cs.CaseState.CLOSED_RETURNED, (
            "unpaid returned case did not close truthfully"
        )
        assert store.returned_mail_refund(case.case_token) is None, (
            "nothing was charged, so nothing may be refunded"
        )


def test_remail_intent_waits_for_the_capture_then_sends_across_restarts(tmp_path: Path) -> None:
    """The dispatcher waits for a pending capture; it neither sends nor rejects meanwhile."""
    path = tmp_path / "cases.sqlite"
    lob = remail_lob()
    payments = FakePaymentClient()
    case, late = returned_with_remail_intent(path, lob, expired=False)
    waited = 0
    for _ in range(8):
        with SqliteTransactionStore(path) as store:
            dispatch_once(store, lob, payments, lambda: late, new_id)
        with SqliteTransactionStore(path) as store:
            if current_case(store, case).payment_state is not ps.PaymentState.CAPTURED:
                waited += 1
                assert len(lob.calls) == 1, "re-mail sent before the capture was confirmed"
                assert store.submission_cursor(case.case_token, 2) is None, (
                    "a pending capture permanently rejected the free re-mail"
                )
    assert waited, "the capture was never pending, so the wait was not exercised"
    with SqliteTransactionStore(path) as store:
        saved = current_case(store, case)
        assert saved.payment_state is ps.PaymentState.CAPTURED
        assert len(lob.calls) == 2
        assert saved.attempt == 2
        sent = store.submission_cursor(case.case_token, 2)
        assert sent is not None
        # Rendered, not yet scanned: sent and awaiting evidence, never rejected.
        assert isinstance(sent.outcome, Uncertain)
        assert sent.outcome.letter_id == "ltr_Remail"
    assert payments.captures == 1


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_service_does_not_remail_after_original_hold_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_case_service import Clock
    from test_effect_dispatch import approved

    assert fake_resolution
    lob = FakeLobClient(
        [
            LobResponse(200, ORIGINAL, evidence=LetterEvidence.PRODUCTION),
            LobResponse(200, "ltr_UnpaidServiceRemail", evidence=LetterEvidence.RENDERED),
        ]
    )
    client, app, intake_id = await approved(tmp_path, monkeypatch, lob)
    try:
        clock = Clock()
        object.__setattr__(app._deps, "clock", clock)
        object.__setattr__(app._deps, "lob_webhook_secret", "secret")
        intake = app._intake(intake_id)
        token = intake.case_token
        assert token is not None
        case = app._store.get_case(token)
        assert case is not None
        payments = FakePaymentClient(case.authorization_ref)
        payments.object = replace(
            payments.object,
            evidence_version=1,
            original_charge_ref="ch_Original",
            capture_before=int(case.authorization_expires_at.timestamp()),
            classification="verified_object",
        )
        object.__setattr__(app._deps, "payments", payments)
        app.dispatch(intake_id)
        case = app._store.get_case(token)
        assert case is not None
        assert case.payment_state is ps.PaymentState.CAPTURE_PENDING
        assert payments.captures == 0
        clock.now = case.authorization_expires_at + timedelta(minutes=1)
        payments.object = full_outcome(PaymentOutcome("canceled", case.authorization_ref))
        first = app._store.get(token, 1)
        assert first is not None
        payload, headers = signed_event(
            {
                "id": ORIGINAL,
                "status": "rendered",
                "metadata": {"submission_token": first.request.submission_token},
            },
            "letter.returned_to_sender",
            clock(),
        )
        assert app.webhook("lob", payload, headers)
        case = app._store.get_case(token)
        assert case is not None
        assert case.letter_state is cs.CaseState.RETURNED
        corrected = replace(first.request.recipient, line1="987 Corrected Avenue")
        seed_destination(app._store, case, clock(), corrected)
        for _ in range(4):
            result = app.advance_due()
            assert result.failures == (), result.failures
            deps = app._deps
            path = app._store.path
            app._store.close()
            app = CaseService(replace(deps, store=SqliteTransactionStore(path)))
        case = app._store.get_case(token)
        assert case is not None
        assert payments.captures == 0
        assert len(lob.calls) == 1, "CaseService spent a second postage on an unpaid original"
        assert case.letter_state is cs.CaseState.CLOSED_RETURNED
        status = app.status(intake_id)
        assert status.return_remedy is not None
        assert status.return_remedy.message == "Your letter was returned. No charge was made."
        assert status.return_remedy.refund_cents == 0
        assert status.return_remedy.refund_status is None
        assert status.return_remedy.remail is False
    finally:
        await client.aclose()
        app._store.close()


# --- H3: an unqualified not_deletable refusal is not production evidence -----------


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_pending_capture_waits_across_restarts_then_paid_remail_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_t30_remedy_followups import pending_capture_case, settle_original_capture

    async with pending_capture_case(tmp_path, monkeypatch) as mailing:
        mailing.confirm_return()
        mailing.correct_destination()
        mailing.lob.script = [
            LobResponse(200, "ltr_PaidRemail", evidence=LetterEvidence.PRODUCTION)
        ]
        for _ in range(3):
            mailing.real._resolve_return(mailing.case_id)
            mailing.real._dispatch(mailing.token)
            mailing.restart()
            assert len(mailing.lob.calls) == 1, "re-mail spent postage before capture confirmation"
            assert mailing.store.returned_mail_refund(mailing.token) is None
        assert mailing.case.letter_state is cs.CaseState.RETURNED
        status = mailing.real.status(mailing.case_id).return_remedy
        assert status is not None
        assert status.remail is False
        assert status.refund_status is None
        settle_original_capture(mailing)
        mailing.approve_remail()
        mailing.sweep(4)
        assert len(mailing.lob.calls) == 2
        settled = mailing.case  # fresh read: the earlier RETURNED assertion narrowed the old one
        assert settled.attempt == 2
        assert settled.letter_state is cs.CaseState.IN_TRANSIT
        assert mailing.payments.captures == 1


def test_unqualified_not_deletable_refusal_keeps_the_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire, payments, path, case, fallback = fallback_world(tmp_path, monkeypatch, resource())

    def respond(method: str, route: str) -> tuple[int, object]:
        if method == "DELETE":
            return 422, {
                "error": {
                    "code": "not_deletable",
                    "message": "This account cannot cancel letters; "
                    "no production-entry confirmation.",
                }
            }
        return list_response([resource()]) if "?" in route else (200, resource())

    wire.respond = respond
    run_dispatches(path, wire, payments, fallback)
    with SqliteTransactionStore(path) as store:
        assert isinstance(cursor_for(store, case).outcome, Uncertain)
        assert current_case(store, case).payment_state is ps.PaymentState.AUTHORIZED
    assert payments.captures == 0, "unqualified error code alone caused capture"
    assert payments.releases == 0


@pytest.mark.parametrize("late", [timedelta(0), timedelta(days=366)], ids=["fallback", "outage"])
@pytest.mark.parametrize("send_date", ["future", "missing"])
def test_not_deletable_refusal_without_corroboration_keeps_the_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, send_date: str, late: timedelta
) -> None:
    """H3 on the old refusal scenarios: no passed send_date and no scan means no capture."""
    payload = resource()
    if send_date == "future":
        payload["send_date"] = (NOW + timedelta(days=400)).isoformat()
    wire, payments, path, case, now = fallback_world(tmp_path, monkeypatch, payload, "refused")
    run_dispatches(path, wire, payments, now + late)
    with SqliteTransactionStore(path) as store:
        current = current_case(store, case)
        assert current.letter_state is cs.CaseState.SUBMITTING
        assert current.payment_state is ps.PaymentState.AUTHORIZED
        cursor = cursor_for(store, case)
        assert isinstance(cursor.outcome, Uncertain)
        assert cursor.outcome.letter_id == ORIGINAL
        assert cursor.next_at > now + late
    assert payments.captures == payments.releases == 0
    assert cancel_requests(wire)
    assert len(wire.creates) == 1


@pytest.mark.parametrize("send_date", [None, (NOW + timedelta(days=400)).isoformat()])
def test_shaped_not_deletable_error_still_needs_independent_corroboration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, send_date: str | None
) -> None:
    wire, payments, path, case, fallback = fallback_world(
        tmp_path, monkeypatch, resource(send_date=send_date), "refused"
    )
    run_dispatches(path, wire, payments, fallback)
    with SqliteTransactionStore(path) as store:
        assert isinstance(cursor_for(store, case).outcome, Uncertain)
        retry_at = cursor_for(store, case).next_at
    assert payments.captures == payments.releases == 0
    # The same durable fallback can recover when fresh lookup proves send_date passed.
    corroborated = resource(send_date=(NOW - timedelta(minutes=1)).isoformat())
    wire.respond = lambda _method, route: (
        list_response([corroborated]) if "?" in route else (200, corroborated)
    )
    run_dispatches(path, wire, payments, retry_at)
    assert payments.captures == 1
    assert payments.releases == 0


# --- M4: a first-scan "returned to sender" also starts returned-mail resolution -----


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize(
    "event", ["letter.returned_to_sender", "letter.certified.returned_to_sender"]
)
def test_first_return_scan_transitions_to_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, late: bool, event: str
) -> None:
    path = tmp_path / "cases.sqlite"
    wire = OfflineLob(monkeypatch, lambda *_: (200, resource()))
    payments = FakePaymentClient()
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        submit(store, wire, case, NOW)
    now = case.authorization_expires_at + timedelta(minutes=1) if late else NOW
    app = app_for(path, payments, now)
    body, headers = signed_event(
        resource(
            tracking_events=[
                {"type": "normal", "name": "Returned to Sender", "time": now.isoformat()}
            ]
        ),
        event,
        now,
    )
    try:
        assert app.webhook("lob", body, headers)
        assert not app.webhook("lob", body, headers)
    finally:
        app._store.close()
    run_dispatches(path, wire, payments, now)
    with SqliteTransactionStore(path) as store:
        saved = current_case(store, case)
        assert saved.letter_state is cs.CaseState.RETURNED, (
            "first return scan was deduplicated without applying ReturnConfirmed"
        )
        assert isinstance(cursor_for(store, case).outcome, Accepted)
        assert not late or payments.captures == 0
    assert payments.releases == 0
    assert payments.captures == (0 if late else 1)
    with SqliteTransactionStore(path) as store:
        assert (
            sum(
                item.event_name == "ReturnConfirmed"
                for item in store.transition_history(case.case_token)
            )
            == 1
        )


# --- M5: actual USPS scan proof is retained per exact attempt ----------------------


@pytest.mark.parametrize("source", ["webhook", "lookup"])
def test_usps_scan_proof_is_retained_per_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    path = tmp_path / "cases.sqlite"
    wire = OfflineLob(monkeypatch, lambda *_: (200, resource()))
    payments = FakePaymentClient()
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        submit(store, wire, case, NOW)
    scan_time = (NOW + timedelta(hours=2, seconds=17)).isoformat()
    receive_time = NOW + timedelta(hours=23)
    tracked = resource(
        tracking_events=[
            dict(
                scan()[0],
                time=scan_time,
                details={"event": "1234", "description": "Arrived at unit"},
            )
        ]
    )
    if source == "webhook":
        app = app_for(path, payments, receive_time)
        payload, headers = signed_event(tracked, "letter.in_transit", receive_time)
        try:
            assert app.webhook("lob", payload, headers)
        finally:
            app._store.close()
    else:
        wire.respond = lambda _method, route: (
            list_response([tracked]) if "?" in route else (200, tracked)
        )
        run_dispatches(path, wire, payments, receive_time)
    with SqliteTransactionStore(path) as store:
        accepted = cursor_for(store, case).outcome
        assert isinstance(accepted, Accepted)
        assert [item.time for item in accepted.scans] == [scan_time], (
            "the acceptance outcome did not keep the scan it was accepted on"
        )
        saved = "\n".join(store._require_open().iterdump())
        assert scan_time in saved, "actual USPS scan time was discarded"
        assert "Arrived at unit" in saved, "provider scan details were discarded"
        proof = store.usps_scan_proof(case.case_token)
        assert proof, "no scan proof retained for the exact attempt"
        for event_type, occurred, details in proof:
            assert event_type in {"normal", "certified"}
            assert occurred == scan_time
            assert details == {"event": "1234", "description": "Arrived at unit"}


@pytest.mark.parametrize("listed", [True, False], ids=["metadata_search", "by_id"])
def test_fallback_lookup_acceptance_retains_scan_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listed: bool
) -> None:
    """Fallback acceptance (token search, or by-id when unlisted) keeps the scan itself."""
    wire, payments, path, case, fallback = fallback_world(tmp_path, monkeypatch, resource())
    scan_time = (NOW + timedelta(hours=3, seconds=5)).isoformat()
    tracked = resource(
        tracking_events=[
            dict(scan()[0], time=scan_time, details={"event": "7", "description": "Sorted"})
        ]
    )
    wire.respond = lambda _method, route: (
        list_response([tracked] if listed else []) if "?" in route else (200, tracked)
    )
    run_dispatches(path, wire, payments, fallback)
    with SqliteTransactionStore(path) as store:
        assert isinstance(cursor_for(store, case).outcome, Accepted)
        assert store.usps_scan_proof(case.case_token) == (
            ("normal", scan_time, {"event": "7", "description": "Sorted"}),
        )
    assert payments.captures == 1
    assert payments.releases == 0


def test_only_usps_scans_are_kept_as_scan_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A printer handoff ("Mailed") is not USPS possession and is never shown as a scan."""
    assert scan_proof(
        [
            {"type": "certified", "name": "Mailed", "time": NOW.isoformat()},
            {"type": "normal", "name": "In Transit", "time": NOW.isoformat()},
            {"type": "normal"},
            "unrelated",
        ]
    ) == (UspsScan("normal", "In Transit", NOW.isoformat()),)
    assert scan_proof(None) == scan_proof("x") == ()

    path = tmp_path / "cases.sqlite"
    wire = OfflineLob(monkeypatch, lambda *_: (200, resource()))
    payments = FakePaymentClient()
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        submit(store, wire, case, NOW)
    scan_time = (NOW + timedelta(hours=2)).isoformat()
    body = resource(
        tracking_events=[
            {"type": "certified", "name": "Mailed", "time": NOW.isoformat()},
            dict(scan()[0], time=scan_time),
        ]
    )
    app = app_for(path, payments, NOW + timedelta(hours=23))
    payload, headers = signed_event(body, "letter.in_transit", NOW + timedelta(hours=23))
    try:
        assert app.webhook("lob", payload, headers)
    finally:
        app._store.close()
    with SqliteTransactionStore(path) as store:
        assert store.usps_scan_proof(case.case_token) == (("normal", scan_time, None),)


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_scan_proof_survives_restart_and_tracking_shows_each_event_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_t30_remedy_followups import pending_capture_case

    # Low-level prepared() deliberately seeds empty intake/prehold JSON, so the full
    # application fixture is used. The retained proof is read through the store
    # projection (the delivery report's source); the customer tracking list is not
    # given a second timeline of the same scans (committed PS07 contract).
    async with pending_capture_case(tmp_path, monkeypatch) as mailing:
        first = mailing.store.get(mailing.token, 1)
        assert first is not None
        cursor = mailing.store.submission_cursor(mailing.token, 1)
        assert cursor is not None
        assert isinstance(cursor.outcome, Accepted)
        scan_time = (mailing.clock() - timedelta(hours=2, seconds=17)).isoformat()
        body = resource(
            id=cursor.outcome.letter_id,
            metadata={"submission_token": first.request.submission_token},
            tracking_events=[
                dict(scan()[0], time=scan_time, details={"description": "Arrived at unit"})
            ],
        )
        object.__setattr__(mailing.real._deps, "lob_webhook_secret", "secret")
        before = [
            (event.event, event.occurred_at)
            for event in mailing.real.tracking(mailing.case_id).events
        ]
        payload, headers = signed_event(body, "letter.in_transit", mailing.clock())
        assert mailing.real.webhook("lob", payload, headers)
        mailing.restart()
        proof = mailing.store.usps_scan_proof(mailing.token, 1)
        assert [(kind, when, details) for kind, when, details in proof] == [
            ("normal", scan_time, {"description": "Arrived at unit"})
        ]
        report = mailing.real.tracking(mailing.case_id)
        shown = [(event.event, event.occurred_at) for event in report.events]
        assert shown == [*before, ("letter.in_transit", mailing.clock())]
        assert all(
            "not stored delivery proof" in event.description
            for event in report.events
            if event.event != "accepted"
        )


def test_scan_proof_keeps_later_scans_and_separates_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cases.sqlite"
    original = resource(tracking_events=scan())
    wire = OfflineLob(monkeypatch, lambda *_: (200, original))
    payments = FakePaymentClient()
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        submit(store, wire, case, NOW)
    run_dispatches(path, wire, payments, NOW)
    with SqliteTransactionStore(path) as store:
        case = current_case(store, case)
        assert case.payment_state is ps.PaymentState.CAPTURED
        case = letter(store, case, cs.ReturnConfirmed("address"))
        decision = seed_destination(store, case, NOW, CORRECTED)
        responsibility = classify_address_failure(
            store.frozen_destination(case.case_token, 1),
            FailureFacts(
                decision.case_id,
                mailed_address_fingerprint=address_fingerprint(make_request().recipient),
            ),
        )
        remedy = remedy_for_responsibility(
            responsibility,
            deadline_feasible=True,
            other_verified_address_exists=True,
            price_cents=case.amount_cents,
            mailing_cost_cents=0,
            remails_used=0,
        )
        assert remedy is not None
        assert remedy.remails
        request = replace(
            make_request(pdf=b"%PDF-1.4\ncorrected synthetic packet", token="second-token"),
            recipient=CORRECTED,
        )
        store.resolve_returned_mail(
            case.case_token,
            case.revision,
            responsibility,
            remedy,
            request,
            now=NOW,
            remail_manifest=synthetic_remail_manifest(store, case, request, NOW),
        )
    second_time = (NOW + timedelta(minutes=2)).isoformat()
    second = resource(
        id="ltr_Remail",
        metadata={"submission_token": "second-token"},
        tracking_events=[{"type": "certified", "name": "In Transit", "time": second_time}],
    )
    wire.respond = lambda *_: (200, second)
    run_dispatches(path, wire, payments, NOW)
    # Later tracking on attempt 1 must survive after attempt 2 has been accepted.
    later = resource(
        tracking_events=[
            {
                "type": "normal",
                "name": "Delivered",
                "details": {"description": "Left with agent"},
            }
        ]
    )
    app = app_for(path, payments, NOW)
    payload, headers = distinct_event(later, "letter.delivered", NOW, "later-first-attempt")
    try:
        assert app.webhook("lob", payload, headers)
        assert not app.webhook("lob", payload, headers)
    finally:
        app._store.close()
    with SqliteTransactionStore(path) as store:
        assert store.usps_scan_proof(case.case_token, 1) == (
            ("normal", NOW.isoformat(), None),
            ("normal", None, {"description": "Left with agent"}),
        )
        assert store.usps_scan_proof(case.case_token, 2) == (("certified", second_time, None),)
        assert [item.name for item in store.usps_scans(case.case_token, 1)] == [
            "In Transit",
            "Delivered",
        ]
    assert len(wire.creates) == 2
    assert payments.captures == 1
