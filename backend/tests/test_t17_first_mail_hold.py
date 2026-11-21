"""Fresh paid first-mail hold preflight using only offline Stripe and Lob fixtures."""

from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

import test_case_service
from parkingbreaker.case_state import CaseState, Effect, SubmitRequested
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LetterRequest, LobResponse, MetadataLookup
from parkingbreaker.transaction_records import PendingEffect
from test_case_service import Clock
from test_case_service import fake_resolution as fake_resolution
from test_effect_dispatch import approved
from test_stripe_payment import Transport, client, fixture

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fake_resolution")]
EXPIRY = test_case_service.NOW + timedelta(days=2)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _hold(status: str = "requires_capture") -> dict[str, object]:
    return fixture(status, expiry=int(EXPIRY.timestamp()))


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("read", ["timeout", "missing_expiry", "changed_expiry", "unknown"])
async def test_unknown_initial_hold_waits_then_uses_exact_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queued: bool, read: str
) -> None:
    monkeypatch.setattr(test_case_service, "fixture", _hold)
    fresh = _hold()
    evidence: object = {
        "timeout": TimeoutError("fixture read unavailable"),
        "missing_expiry": fixture(expiry=None),
        "changed_expiry": fixture(expiry=int((EXPIRY + timedelta(days=1)).timestamp())),
        "unknown": _hold("processing"),
    }[read]

    class GuardedLob(FakeLobClient):
        def create_letter(self, request: LetterRequest, idempotency_key: str) -> LobResponse:
            # No create until the successful second read of the original object.
            assert [call[0] for call in transport.calls] == ["get", "get"]
            return super().create_letter(request, idempotency_key)

    lob = GuardedLob([LobResponse(200, "ltr_Original", evidence=LetterEvidence.RENDERED)])
    api, real, intake_id = await approved(tmp_path, monkeypatch, lob)
    clock = Clock()
    transport = Transport(evidence, fresh)
    object.__setattr__(real._deps, "payments", client(transport))
    object.__setattr__(real._deps, "clock", clock)
    try:
        token = real._intake(intake_id).case_token
        assert token is not None
        store = real._store
        item = store.get_case(token)
        assert item is not None
        if queued:
            item = store.apply_letter_event(
                token,
                SubmitRequested(),
                item.revision,
                occurred_at=clock(),
                new_effect_id=lambda: uuid4().hex,
            )
            row = next(iter(store.pending_effects(token, kinds=(Effect.SUBMIT_LETTER,))))
            store.claim_effect(row.effect_id, clock())  # restart after outbox claim; no Lob cursor
        before = store.effects(token)
        gateway = f"/v1/payment_intents/{item.authorization_ref}"

        real.dispatch(intake_id)
        assert transport.calls[0][:2] == ("get", gateway)
        assert len(transport.calls) == 1
        assert not lob.calls
        assert not lob.lookups
        assert store.effects(token) == before
        assert store.submission_cursor(token, 1) is None
        assert store.payment_cursor(token) is None
        assert store.get_case(token) == item

        real.dispatch(intake_id)
        assert len(lob.calls) == 1
        assert all(call[:2] == ("get", gateway) for call in transport.calls)
        assert store.submission_cursor(token, 1) is not None
        assert store.payment_cursor(token) is None  # rendered alone is not capture evidence
    finally:
        await api.aclose()
        real._store.close()


@pytest.mark.parametrize("queued", [False, True])
async def test_verified_original_cancellation_stops_never_sent_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queued: bool
) -> None:
    monkeypatch.setattr(test_case_service, "fixture", _hold)
    lob = FakeLobClient()
    api, real, intake_id = await approved(tmp_path, monkeypatch, lob)
    clock = Clock()
    transport = Transport(_hold("canceled"), _hold("canceled"))
    object.__setattr__(real._deps, "payments", client(transport))
    object.__setattr__(real._deps, "clock", clock)
    try:
        token = real._intake(intake_id).case_token
        assert token is not None
        store = real._store
        item = store.get_case(token)
        assert item is not None
        if queued:
            store.apply_letter_event(
                token,
                SubmitRequested(),
                item.revision,
                occurred_at=clock(),
                new_effect_id=lambda: uuid4().hex,
            )
        real.dispatch(intake_id)
        real.dispatch(intake_id)  # no new send after the local refusal
        assert real.status(intake_id).state is CaseState.STOPPED_NO_CHARGE
        assert not lob.calls
        assert all(
            call[:2] == ("get", f"/v1/payment_intents/{item.authorization_ref}")
            for call in transport.calls
        )
        assert not store.pending_effects(token, kinds=(Effect.SUBMIT_LETTER,))
        assert store.submission_cursor(token, 1) is None
    finally:
        await api.aclose()
        real._store.close()


async def test_queued_authoritative_stop_rolls_back_without_possible_send_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(test_case_service, "fixture", _hold)
    lob = FakeLobClient()
    api, real, intake_id = await approved(tmp_path, monkeypatch, lob)
    clock = Clock()
    transport = Transport(_hold("canceled"), _hold("canceled"), _hold("canceled"))
    object.__setattr__(real._deps, "payments", client(transport))
    object.__setattr__(real._deps, "clock", clock)
    try:
        token = real._intake(intake_id).case_token
        assert token is not None
        store = real._store
        item = store.get_case(token)
        assert item is not None
        queued = store.apply_letter_event(
            token,
            SubmitRequested(),
            item.revision,
            occurred_at=clock(),
            new_effect_id=lambda: uuid4().hex,
        )
        before = store.effects(token)
        original_update = store._update_effect

        def interruption(
            effect_id: str,
            timestamp: datetime,
            outcome: str | None,
            *,
            manage_transaction: bool = True,
        ) -> PendingEffect:
            if outcome == "authorization_unusable_no_send":
                raise RuntimeError("fixture aborted queued stop commit")
            return original_update(
                effect_id, timestamp, outcome, manage_transaction=manage_transaction
            )

        monkeypatch.setattr(store, "_update_effect", interruption)
        with pytest.raises(RuntimeError, match="queued stop"):
            real.dispatch(intake_id)
        assert store.get_case(token) == queued
        assert store.effects(token) == before
        assert store.submission_cursor(token, 1) is None
        assert not lob.calls

        monkeypatch.setattr(store, "_update_effect", original_update)
        real.dispatch(intake_id)
        assert real.status(intake_id).state is CaseState.STOPPED_NO_CHARGE
        assert store.submission_cursor(token, 1) is None
        assert not lob.calls
        assert all(call[0] == "get" for call in transport.calls)
    finally:
        await api.aclose()
        real._store.close()


async def test_previous_possible_send_reconciles_without_a_new_hold_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(test_case_service, "fixture", _hold)

    class InconclusiveLob(FakeLobClient):
        def find_by_metadata(self, submission_token: str) -> MetadataLookup:
            self.lookups.append(submission_token)
            return MetadataLookup(None)

    lob = InconclusiveLob([LobResponse(None, error="fixture response lost")])
    api, real, intake_id = await approved(tmp_path, monkeypatch, lob)
    clock = Clock()
    transport = Transport(_hold())
    object.__setattr__(real._deps, "payments", client(transport))
    object.__setattr__(real._deps, "clock", clock)
    try:
        token = real._intake(intake_id).case_token
        assert token is not None
        real.dispatch(intake_id)
        assert len(lob.calls) == len(transport.calls) == 1
        clock.now += timedelta(hours=24)
        real.dispatch(intake_id)
        assert len(transport.calls) == 1  # no repeat preflight; no capture evidence
        assert len(lob.calls) == 1
        assert len(lob.lookups) == 1
        cursor = real._store.submission_cursor(token, 1)
        assert cursor is not None
        assert cursor.lookup_only
    finally:
        await api.aclose()
        real._store.close()
