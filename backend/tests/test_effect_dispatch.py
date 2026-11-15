"""C7 transaction proofs, using the API, Lob fake and the real Stripe SDK transport fake."""

import hashlib
import hmac
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from httpx2 import ASGITransport, AsyncClient

from mail_samples import RECIPIENT, seed_destination
from parkingbreaker.api.app import create_app
from parkingbreaker.case_service import CaseService
from parkingbreaker.case_state import CaseState
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.session_window import DEFAULT_WINDOW
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_case_service import (
    ALICE,
    INTAKE,
    NOW,
    Clock,
    fake_resolution,
    researched_case,
    stripe,
)
from test_stripe_payment import fixture

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fake_resolution")]
# Re-export the existing intake fixture; no second research/intake path.
assert fake_resolution
WORKER = {"Authorization": "Bearer fake-worker"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lob: FakeLobClient
) -> tuple[AsyncClient, CaseService, str]:
    from parkingbreaker.appeal.drafting import Draft, DraftSentence
    from parkingbreaker.appeal.fakes import FakeDrafter
    from test_tier_acceptance import extract_case, model_answers

    result, _llm, _searcher = extract_case(model_answers())
    client, real, case = await researched_case(tmp_path, monkeypatch, result)
    statement = INTAKE["customer_position"]["statement"]
    object.__setattr__(
        real._deps,
        "drafter",
        FakeDrafter(lambda *_: Draft((DraftSentence(statement, (f"note:0-{len(statement)}",)),))),
    )
    research = await client.post(
        f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
    )
    assert research.status_code == 200
    if research.json()["state"] == "NEEDS_CUSTOMER":
        research = await client.post(
            f"/cases/{case}/answers",
            headers=ALICE,
            json={"question_id": "VEHICLE_RELATIONSHIP", "answer": "REGISTERED_OWNER"},
        )
    assert research.json()["state"] == "DRAFTING"
    object.__setattr__(real._deps, "lob", lob)
    object.__setattr__(real._deps, "new_id", lambda: uuid4().hex)
    status = await client.post(
        f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "draft"}
    )
    assert status.json()["state"] == "AWAITING_ADOPTION"
    draft = (await client.get(f"/cases/{case}/draft", headers=ALICE)).json()
    adoption = await client.post(
        f"/cases/{case}/adoption",
        headers=ALICE,
        json={"text": draft["text"], "text_sha256": draft["text_sha256"]},
    )
    assert adoption.status_code == 200
    packet = await client.post(
        f"/cases/{case}/packet-preview",
        headers=ALICE,
        json={"adopted_sha256": adoption.json()["adopted_sha256"]},
    )
    assert packet.status_code == 200, packet.text
    response = await client.post(
        f"/cases/{case}/approval",
        headers=ALICE,
        json={
            "adopted_sha256": adoption.json()["adopted_sha256"],
            "packet_sha256": packet.json()["packet_sha256"],
            "envelope_sha256": packet.json()["envelope_sha256"],
        },
    )
    assert response.json()["state"] == "FROZEN"
    assert not lob.calls
    return client, real, case


def payment_fixture(status: str = "requires_capture") -> dict[str, object]:
    return fixture(status)


async def dispatch(client: AsyncClient) -> dict[str, Any]:
    response = await client.post("/internal/dispatch", headers=WORKER)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def test_full_api_transaction_captures_only_after_lob_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient(
        [
            LobResponse(
                200, "ltr_original", tracking_number="940000001", evidence=LetterEvidence.PRODUCTION
            )
        ]
    )
    client, real, case = await approved(tmp_path, monkeypatch, lob)
    payments, transport = stripe(payment_fixture(), payment_fixture("succeeded"))
    object.__setattr__(real._deps, "payments", payments)
    try:
        first = await client.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "dispatch"}
        )
        assert first.status_code == 200
        assert first.json()["state"] == "IN_TRANSIT"
        assert len(lob.calls) == 1
        assert transport.calls[0][0] == "get"
        assert real.status(case).payment_state is PaymentState.CAPTURE_PENDING
        await dispatch(client)
        assert transport.calls[1][1].endswith("/capture")
        status = (await client.get(f"/cases/{case}/status", headers=ALICE)).json()
        assert (status["state"], status["payment_state"]) == ("IN_TRANSIT", "CAPTURED")
        track = (await client.get(f"/cases/{case}/tracking", headers=ALICE)).json()
        assert track["tracking_number"] == "940000001"
        assert track["fake"] is False
        assert track["events"][0]["event"] == "accepted"
        await dispatch(client)
        assert len(lob.calls) == 1
        assert len(transport.calls) == 2
        token = real._intake(case).case_token
        assert token is not None
        frozen = real._store.get(token, 1)
        assert frozen is not None
        assert lob.calls[0][1].pdf == frozen.request.pdf
        real._store._payment_case(token)
    finally:
        await client.aclose()


async def test_lob_5xx_then_metadata_lookup_never_mails_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(503)])
    client, real, case = await approved(tmp_path, monkeypatch, lob)
    clock = Clock()
    object.__setattr__(real._deps, "clock", clock)
    payments, transport = stripe(payment_fixture())
    object.__setattr__(real._deps, "payments", payments)
    try:
        await dispatch(client)
        assert not transport.calls
        token = real._intake(case).case_token
        assert token is not None
        prepared = real._store.get(token, 1)
        assert prepared is not None
        lob.letters[prepared.request.submission_token] = "ltr_lost_response"
        clock.now += timedelta(hours=23)
        await dispatch(client)  # same key final replay, then lookup branch
        cursor = real._store.submission_cursor(token, 1)
        assert cursor is not None
        clock.now = max(clock.now, cursor.next_at)
        await dispatch(client)
        assert lob.lookups == [prepared.request.submission_token]
        assert real.status(case).state is CaseState.IN_TRANSIT
        assert real.tracking(case).events[0].occurred_at == clock.now
        assert len({key for key, _ in lob.calls}) == 1
        assert len(lob.letters) == 1
        assert len(transport.calls) == 1
    finally:
        await client.aclose()


async def test_definitive_lob_reject_stops_and_releases_without_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(422)])
    client, real, case = await approved(tmp_path, monkeypatch, lob)
    payments, transport = stripe(payment_fixture(), payment_fixture("canceled"))
    object.__setattr__(real._deps, "payments", payments)
    try:
        await dispatch(client)
        await dispatch(client)
        assert real.status(case).state is CaseState.STOPPED_NO_CHARGE
        assert real.status(case).payment_state is PaymentState.RELEASED
        assert transport.calls[-1][1].endswith("/cancel")
        assert not any(call[1].endswith("/capture") for call in transport.calls)
        await dispatch(client)
        assert len(lob.calls) == 1
        assert len(transport.calls) == 2
    finally:
        await client.aclose()


async def test_scheduler_window_expiry_releases_without_customer_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(200, "ltr_never", evidence=LetterEvidence.PRODUCTION)])
    client, real, case = await approved(tmp_path, monkeypatch, lob)
    clock = Clock()
    clock.now += DEFAULT_WINDOW + timedelta(minutes=1)
    object.__setattr__(real._deps, "clock", clock)
    payments, transport = stripe(payment_fixture(), payment_fixture("canceled"))
    object.__setattr__(real._deps, "payments", payments)
    try:
        await dispatch(client)
        await dispatch(client)
        assert not lob.calls
        assert transport.calls[-1][1].endswith("/cancel")
        assert real.status(case).stop is not None
        assert real.status(case).payment_state is PaymentState.RELEASED
    finally:
        await client.aclose()


async def test_crash_between_outbox_claim_and_complete_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(200, "ltr_once", evidence=LetterEvidence.PRODUCTION)])
    client, real, case = await approved(tmp_path, monkeypatch, lob)
    token = real._intake(case).case_token
    assert token is not None
    payments, _transport = stripe(payment_fixture(), payment_fixture("succeeded"))
    object.__setattr__(real._deps, "payments", payments)
    original = real._store.complete_effect

    def crash(*_args: object, **_kwargs: object) -> Any:
        raise KeyboardInterrupt("simulated process exit")

    monkeypatch.setattr(real._store, "complete_effect", crash)
    try:
        with pytest.raises(KeyboardInterrupt):
            real.dispatch(case)
        monkeypatch.setattr(real._store, "complete_effect", original)
        old = real._store
        real._store = SqliteTransactionStore(tmp_path / "pb.sqlite")
        old.close()
        await dispatch(client)
        await dispatch(client)
        assert len(lob.calls) == 1
        assert real.status(case).payment_state is PaymentState.CAPTURED
        assert not real._store.pending_effects(token)
    finally:
        await client.aclose()


async def test_crash_after_claim_before_provider_call_is_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(200, "ltr_once", evidence=LetterEvidence.PRODUCTION)])
    client, real, case = await approved(tmp_path, monkeypatch, lob)
    original = real._store.claim_effect

    def crash(effect_id: str, at: Any) -> Any:
        original(effect_id, at)
        raise KeyboardInterrupt("simulated process exit")

    monkeypatch.setattr(real._store, "claim_effect", crash)
    payments, _transport = stripe(payment_fixture())
    object.__setattr__(real._deps, "payments", payments)
    try:
        with pytest.raises(KeyboardInterrupt):
            real.dispatch(case)
        monkeypatch.setattr(real._store, "claim_effect", original)
        await dispatch(client)
        assert len(lob.calls) == 1
        assert real.status(case).state is CaseState.IN_TRANSIT
    finally:
        await client.aclose()


@pytest.mark.parametrize("provider", ["lob", "stripe"])
async def test_bad_webhook_signature_refused_and_event_replay_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    lob = FakeLobClient([LobResponse(200, "ltr_original", evidence=LetterEvidence.PRODUCTION)])
    client, real, case = await approved(tmp_path, monkeypatch, lob)
    object.__setattr__(real._deps, "stripe_webhook_secret", "secret")
    object.__setattr__(real._deps, "lob_webhook_secret", "secret")
    payments, _transport = stripe(payment_fixture(), payment_fixture("succeeded"))
    object.__setattr__(real._deps, "payments", payments)
    try:
        await dispatch(client)
        await dispatch(client)
        data: dict[str, Any] = (
            {
                "id": "evt_unique",
                "date_created": NOW.isoformat(),
                "event_type": {"id": "letter.in_transit"},
                "body": {"id": "ltr_original", "tracking_number": "9400123"},
            }
            if provider == "lob"
            else {
                "id": "evt_unique",
                "object": "event",
                "type": "payment_intent.succeeded",
                "data": {"object": payment_fixture("succeeded")},
            }
        )
        payload = json.dumps(data).encode()
        at = str(int(NOW.timestamp()))
        digest = hmac.new(b"secret", at.encode() + b"." + payload, hashlib.sha256).hexdigest()
        headers = (
            {"Lob-Signature": digest, "Lob-Signature-Timestamp": at}
            if provider == "lob"
            else {
                "Stripe-Signature": f"t={at},v1={digest}",
            }
        )
        path = f"/webhooks/{provider}"
        assert (await client.post(path, content=payload)).status_code == 400
        first = await client.post(path, content=payload, headers=headers)
        assert first.status_code == 200, first.text
        assert first.json() == {"received": True, "duplicate": False}
        old = real._store
        real._store = SqliteTransactionStore(tmp_path / "pb.sqlite")
        old.close()
        second = await client.post(path, content=payload, headers=headers)
        assert second.json() == {"received": True, "duplicate": True}
        if provider == "lob":
            tracking = real.tracking(case)
            assert tracking.tracking_number == "9400123"
            assert [e.event for e in tracking.events] == ["accepted", "letter.in_transit"]
        assert real.status(case).payment_state is PaymentState.CAPTURED
    finally:
        await client.aclose()


async def test_concurrent_dispatch_does_not_steal_live_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(200, "ltr_once", evidence=LetterEvidence.PRODUCTION)])
    client, _real, _case = await approved(tmp_path, monkeypatch, lob)
    other = SqliteTransactionStore(tmp_path / "pb.sqlite")
    try:
        with other.submission_ownership():
            assert await dispatch(client) == {"processed": 0, "failed": 0}
        assert not lob.calls
    finally:
        other.close()
        await client.aclose()


async def test_dispatch_worker_secret_is_injected_and_enforced(tmp_path: Path) -> None:
    from test_case_service import service

    app = create_app(service(tmp_path / "pb.sqlite"), worker_secret="scheduler-secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/internal/dispatch", headers=WORKER)).status_code == 401


async def test_unexpected_capture_on_no_mail_stop_is_fully_compensated(tmp_path: Path) -> None:
    from parkingbreaker.effect_dispatch import dispatch_once
    from test_payment_coordinator import compensation_client, stopped_compensation
    from test_transaction_lifecycle import NOW as PAYMENT_NOW

    payments = compensation_client()
    lob = FakeLobClient()
    with SqliteTransactionStore(tmp_path / "pb.sqlite") as store:
        case = stopped_compensation(store)
        clock = Clock()
        for _ in range(4):
            cursor = store.payment_cursor(case.case_token)
            clock.now = PAYMENT_NOW if cursor is None else cursor.next_at
            dispatch_once(store, lob, payments, clock, lambda: uuid4().hex)
        current = store.get_case(case.case_token)
        assert current is not None
        assert current.letter_state is CaseState.STOPPED_NO_CHARGE
        assert current.payment_state is PaymentState.REFUNDED
        assert payments.refund_calls == 1
        assert payments.captures == 0
        assert not lob.calls
        assert not store.pending_effects(case.case_token)


async def test_webhook_reconciles_capture_through_existing_coordinator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(200, "ltr_original", evidence=LetterEvidence.PRODUCTION)])
    client, real, case = await approved(tmp_path, monkeypatch, lob)
    object.__setattr__(real._deps, "stripe_webhook_secret", "secret")
    payments, transport = stripe(payment_fixture(), payment_fixture("succeeded"))
    object.__setattr__(real._deps, "payments", payments)
    try:
        await dispatch(client)
        token = real._intake(case).case_token
        assert token is not None
        original = real._store.finish_payment

        def crash(*_args: object, **_kwargs: object) -> Any:
            raise KeyboardInterrupt("simulated exit after capture")

        monkeypatch.setattr(real._store, "finish_payment", crash)
        with pytest.raises(KeyboardInterrupt):
            real.dispatch(case)
        monkeypatch.setattr(real._store, "finish_payment", original)
        assert len([c for c in transport.calls if c[1].endswith("/capture")]) == 1
        # Recovery retrieves the existing original object; never repeats the capture.
        payments, retrieved = stripe(payment_fixture("succeeded"))
        object.__setattr__(real._deps, "payments", payments)
        payload = json.dumps(
            {
                "id": "evt_captured",
                "type": "payment_intent.succeeded",
                "data": {"object": payment_fixture("succeeded")},
            }
        ).encode()
        timestamp = str(int(NOW.timestamp()))
        digest = hmac.new(
            b"secret", timestamp.encode() + b"." + payload, hashlib.sha256
        ).hexdigest()
        response = await client.post(
            "/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": f"t={timestamp},v1={digest}"},
        )
        assert response.status_code == 200
        assert real.status(case).payment_state is PaymentState.CAPTURED
        assert len(retrieved.calls) == 1
        assert retrieved.calls[0][0] == "get"
        assert not real._store.pending_effects(token)
    finally:
        await client.aclose()


async def test_certified_return_and_free_remail_never_capture_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    from parkingbreaker.approval_manifest import build_remail_approval_manifest
    from parkingbreaker.case_state import RemailRequested

    lob = FakeLobClient(
        [
            LobResponse(200, "ltr_original", evidence=LetterEvidence.PRODUCTION),
            LobResponse(200, "ltr_remail", evidence=LetterEvidence.PRODUCTION),
        ]
    )
    client, real, case = await approved(tmp_path, monkeypatch, lob)
    object.__setattr__(real._deps, "lob_webhook_secret", "secret")
    payments, transport = stripe(payment_fixture(), payment_fixture("succeeded"))
    object.__setattr__(real._deps, "payments", payments)
    try:
        await dispatch(client)
        await dispatch(client)
        token = real._intake(case).case_token
        assert token is not None
        at = str(int(NOW.timestamp()))

        async def returned(event_id: str) -> None:
            payload = json.dumps(
                {
                    "id": event_id,
                    "event_type": {"id": "letter.certified.returned_to_sender"},
                    "date_created": NOW.isoformat(),
                    "body": {"id": "ltr_original"},
                }
            ).encode()
            digest = hmac.new(b"secret", at.encode() + b"." + payload, hashlib.sha256).hexdigest()
            response = await client.post(
                "/webhooks/lob",
                content=payload,
                headers={"Lob-Signature": digest, "Lob-Signature-Timestamp": at},
            )
            assert response.status_code == 200

        await returned("evt_returned")
        assert real.status(case).state is CaseState.RETURNED
        original = real._store.get(token, 1)
        assert original is not None
        # The free re-mail needs a persisted SYSTEM_VERIFIED destination that differs
        # from the returned letter's address.
        corrected = replace(RECIPIENT, line1="987 Corrected Avenue")
        returned_case = real._store.get_case(token)
        assert returned_case is not None
        seed_destination(real._store, returned_case, NOW, corrected)
        remail = real._store.prepare(
            token,
            2,
            replace(original.request, submission_token="sub_remail", recipient=corrected),
            NOW,
        )
        original_manifest = real._store.get_approval_manifest(token, 1)
        assert original_manifest is not None
        real._store.record_approval_manifest(
            build_remail_approval_manifest(
                original_manifest, remail.request, approved_at=remail.prepared_at
            )
        )
        real._apply(token, RemailRequested())
        await dispatch(client)
        assert real.status(case).state is CaseState.IN_TRANSIT
        assert real.status(case).payment_state is PaymentState.CAPTURED
        assert len(transport.calls) == 2
        assert len(lob.calls) == 2
        assert lob.calls[0][0] != lob.calls[1][0]
        await returned("evt_late_first_attempt_return")
        assert real.status(case).state is CaseState.IN_TRANSIT
        await dispatch(client)
        assert len(lob.calls) == 2
        assert len(transport.calls) == 2
    finally:
        await client.aclose()
