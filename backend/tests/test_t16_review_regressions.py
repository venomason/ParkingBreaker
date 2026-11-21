"""T16 review regressions at real provider seams and durable dispatch boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

import test_case_service
from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.effect_dispatch import dispatch_once
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse, Uncertain
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_case_service import Clock
from test_case_service import fake_resolution as fake_resolution
from test_effect_dispatch import approved
from test_stripe_payment import CHARGE, EXPIRY, REF, Transport, client, fixture, make_case
from test_t16_settlement import advance
from test_transaction_lifecycle import NOW


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class LostCaptureTransport(Transport):
    """Stripe captures successfully, but its mutation response is lost in transit."""

    captured = False

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        post_data: object = None,
    ) -> tuple[str, int, Mapping[str, str]]:
        if method == "post" and url.endswith("/capture"):
            self.captured = True
        return super().request(method, url, headers, post_data)


@pytest.mark.parametrize(
    "lookup",
    ["timeout", "401", "404", "unreadable", "identity", "charge", "expiry", "requires_capture"],
)
def test_lost_capture_uncertain_final_retrieve_logs_only_and_stops(
    tmp_path: Path, lookup: str
) -> None:
    response: object = TimeoutError("lookup unavailable")
    if lookup in {"401", "404"}:
        response = {"error": {"type": "api_error", "message": "unavailable"}}
    elif lookup == "unreadable":
        response = "not json"
    elif lookup == "identity":
        response = {**fixture("succeeded"), "id": "pi_Wrong"}
    elif lookup == "charge":
        changed = fixture("succeeded")
        charge = changed["latest_charge"]
        assert isinstance(charge, dict)
        charge["id"] = "ch_Wrong"
        response = changed
    elif lookup == "expiry":
        response = fixture(expiry=EXPIRY + 1)
    elif lookup == "requires_capture":
        response = fixture()
    transport = LostCaptureTransport(fixture(), TimeoutError("lost successful capture"), response)
    adapter = client(transport)
    path = tmp_path / "review.sqlite"
    with SqliteTransactionStore(path) as store:
        case = make_case(store, datetime.fromtimestamp(EXPIRY, UTC))
        cursor = advance(store, adapter, case.case_token, NOW)
        cursor = advance(store, adapter, case.case_token, cursor.next_at)
        assert transport.captured
        assert cursor.outcome.status == "uncertain"
        assert not cursor.done
        bound = case.authorization_expires_at + ps.CAPTURE_RECONCILIATION_GRACE
        if lookup in {"401", "404"}:
            transport.status = int(lookup)
        cursor = advance(store, adapter, case.case_token, bound)
        assert len(transport.calls) == 3
        assert transport.calls[-1][:2] == ("get", f"/v1/payment_intents/{REF}")
        assert cursor.done
        assert cursor.outcome.status == (
            "requires_capture" if lookup == "requires_capture" else "uncertain"
        )
        saved = store.get_case(case.case_token)
        assert saved is not None
        assert saved.payment_state is ps.PaymentState.WRITTEN_OFF
        assert [
            e.effect for e in store.effects(case.case_token) if e.revision == saved.revision
        ] == [ps.PaymentEffect.LOG_WRITE_OFF]
        assert not any(
            e.effect is ps.PaymentEffect.SEND_PAYMENT_LINK for e in store.effects(case.case_token)
        )
        assert store.transition_history(case.case_token)[-1].event_name == "ReconciliationExhausted"
        # make_case supplies durable synthetic acceptance rather than running Lob dispatch.
        for row in store.pending_effects(case.case_token):
            if row.machine == "letter":
                store.claim_effect(row.effect_id, bound)
                store.complete_effect(row.effect_id, "routed", bound)
    with SqliteTransactionStore(path) as store:
        assert store.payment_cursor(case.case_token) == cursor
        # No existing late-evidence ingestion path reopens a settled cursor or charges again.
        for _ in range(3):
            dispatch_once(store, FakeLobClient(), adapter, lambda: bound, lambda: uuid4().hex)
        assert store.payment_cursor(case.case_token) == cursor
        logs = [
            e for e in store.effects(case.case_token) if e.effect is ps.PaymentEffect.LOG_WRITE_OFF
        ]
        assert len(logs) == 1
        assert logs[0].completed_at == bound
        assert len(transport.calls) == 3


@pytest.mark.parametrize("status", ["succeeded", "canceled"])
def test_authoritative_final_retrieve_wins_at_exhaustion_bound(tmp_path: Path, status: str) -> None:
    transport = LostCaptureTransport(
        fixture(), TimeoutError("lost capture response"), fixture(status)
    )
    adapter = client(transport)
    with SqliteTransactionStore(tmp_path / "review.sqlite") as store:
        case = make_case(store, datetime.fromtimestamp(EXPIRY, UTC))
        cursor = advance(store, adapter, case.case_token, NOW)
        cursor = advance(store, adapter, case.case_token, cursor.next_at)
        cursor = advance(
            store,
            adapter,
            case.case_token,
            case.authorization_expires_at + ps.CAPTURE_RECONCILIATION_GRACE,
        )
        assert cursor.done
        assert cursor.outcome.status == status
        assert len(transport.calls) == 3
        saved = store.get_case(case.case_token)
        assert saved is not None
        assert saved.payment_state is (
            ps.PaymentState.CAPTURED if status == "succeeded" else ps.PaymentState.WRITTEN_OFF
        )
        if status == "succeeded":
            assert cursor.outcome.charge_ref == CHARGE
            assert not any(
                e.effect is ps.PaymentEffect.SEND_PAYMENT_LINK
                for e in store.effects(case.case_token)
            )
        else:
            assert [
                e.effect for e in store.pending_effects(case.case_token) if e.machine == "payment"
            ] == [ps.PaymentEffect.SEND_PAYMENT_LINK, ps.PaymentEffect.LOG_WRITE_OFF]


def test_real_retrieve_precedes_exhaustion_and_late_uncertainty_gets_full_grace(
    tmp_path: Path,
) -> None:
    transport = Transport(*(TimeoutError("offline") for _ in range(4)))
    adapter = client(transport)
    with SqliteTransactionStore(tmp_path / "review.sqlite") as store:
        case = make_case(store, datetime.fromtimestamp(EXPIRY, UTC))
        began = case.authorization_expires_at + timedelta(days=10)
        cursor = advance(store, adapter, case.case_token, began)
        assert len(transport.calls) == 1
        assert not cursor.done
        old_bound = case.authorization_expires_at + ps.CAPTURE_RECONCILIATION_GRACE
        cursor = advance(store, adapter, case.case_token, old_bound)
        assert len(transport.calls) == 2
        assert not cursor.done, "Late uncertainty must receive the complete bounded grace"
        bound = began + ps.CAPTURE_RECONCILIATION_GRACE
        cursor = advance(store, adapter, case.case_token, bound - timedelta(seconds=1))
        assert not cursor.done
        assert cursor.next_at == bound
        assert len(transport.calls) == 3
        cursor = advance(store, adapter, case.case_token, bound)
        assert len(transport.calls) == 4
        assert all(c[:2] == ("get", f"/v1/payment_intents/{REF}") for c in transport.calls)
        assert cursor.done
        assert store.payment_cursor(case.case_token) == cursor


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_potentially_sent_create_at_expiry_keeps_hold_until_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expiry = test_case_service.NOW + timedelta(minutes=31)
    monkeypatch.setattr(
        test_case_service, "fixture", lambda: fixture(expiry=int(expiry.timestamp()))
    )
    lob = FakeLobClient([LobResponse(None, error="lost response")])
    api, real, intake_id = await approved(tmp_path, monkeypatch, lob)
    clock = Clock()
    active = fixture(expiry=int(expiry.timestamp()))
    transport = Transport(active, fixture("canceled", expiry=int(expiry.timestamp())))
    adapter = client(transport)
    try:
        token = real._intake(intake_id).case_token
        assert token is not None
        dispatch_once(real._store, lob, adapter, clock, lambda: uuid4().hex)
        assert len(lob.calls) == 1
        prepared = real._store.get(token, 1)
        assert prepared is not None
        lob.letters[prepared.request.submission_token] = "ltr_lost_original"
        clock.now = expiry + timedelta(seconds=1)
        for _ in range(3):
            pending = real._store.submission_cursor(token, 1)
            assert pending is not None
            if not isinstance(pending.outcome, Uncertain):
                break
            clock.now = max(clock.now, pending.next_at)
            dispatch_once(real._store, lob, adapter, clock, lambda: uuid4().hex)
            saved = real._store.get_case(token)
            assert saved is not None
            assert saved.letter_state is not cs.CaseState.STOPPED_NO_CHARGE
            assert saved.payment_state is not ps.PaymentState.RELEASED
            # Force the existing bounded same-key path into metadata lookup.
            clock.now = max(clock.now, pending.first_sent_at + timedelta(hours=24))
        assert lob.lookups == [prepared.request.submission_token]
        assert len(lob.calls) == 1
        assert real.status(intake_id).state is cs.CaseState.IN_TRANSIT
        assert not any(call[1].endswith("/cancel") for call in transport.calls)
    finally:
        await api.aclose()
        real._store.close()


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
@pytest.mark.parametrize("path", ["frozen", "guard"])
@pytest.mark.parametrize("minutes_left", [29, 31])
async def test_expiry_margin_and_both_paths_show_truthful_fixed_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, minutes_left: int
) -> None:
    # The guard sees time cross the margin after the initial FROZEN precheck.
    initial_minutes = 31 if path == "guard" else minutes_left
    expiry = test_case_service.NOW + timedelta(minutes=initial_minutes)
    monkeypatch.setattr(
        test_case_service, "fixture", lambda: fixture(expiry=int(expiry.timestamp()))
    )
    lob = FakeLobClient(
        [
            LobResponse(
                200, "ltr_allowed", tracking_number="940000001", evidence=LetterEvidence.PRODUCTION
            )
        ]
    )
    api, real, intake_id = await approved(tmp_path, monkeypatch, lob)
    clock = Clock()
    responses = [fixture(expiry=int(expiry.timestamp()))] if initial_minutes > 30 else []
    adapter = client(Transport(*responses, fixture("canceled", expiry=int(expiry.timestamp()))))
    try:
        token = real._intake(intake_id).case_token
        assert token is not None
        if path == "guard":
            original_claim = real._store.claim_submission

            def cross_margin(*args: object, **kwargs: object) -> object:
                result = original_claim(*args, **kwargs)  # type: ignore[arg-type]
                clock.now = expiry - timedelta(minutes=minutes_left)
                return result

            monkeypatch.setattr(real._store, "claim_submission", cross_margin)
        dispatch_once(real._store, lob, adapter, clock, lambda: uuid4().hex)
        stop = real.status(intake_id).stop
        if minutes_left == 31:
            assert len(lob.calls) == 1
            assert stop is None
        else:
            assert not lob.calls
            assert stop is not None
            assert stop.reason_key == "stop.transaction_failure.authorization_expired"
            assert stop.message == (
                "Your payment hold expired before mailing; you were not charged. "
                "Nothing was charged. Your work is saved. "
                "You can review your saved details and try again."
            )
            events = real._store.transition_history(token)
            final_stop = next(e for e in reversed(events) if e.machine == "letter")
            assert final_stop.event_name in {"AuthorizationExpired", "Stopped"}
    finally:
        await api.aclose()
        real._store.close()
