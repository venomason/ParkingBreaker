"""Current policy transition: preserve history, refuse new unverified authority.

Historical records are injected directly as synthetic pre-upgrade data. No test-only
runtime switch enables the retired lane; all assertions exercise current production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.case_state import CaseState
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse, MetadataLookup, Uncertain
from parkingbreaker.payment_state import PaymentState
from test_case_service import Clock, approve_with_preview, drafting_case, fake_resolution, stripe
from test_effect_dispatch import approved, payment_fixture
from test_t30_mail_gate import save_customer_instruction

assert fake_resolution
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fake_resolution")]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize("boundary", ["preview", "approval", "frozen_replay"])
async def test_historical_consent_does_not_authorize_a_new_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        real.step(case, s.WorkerStep(step_id="draft"))
        draft = real.draft(case)
        adoption = real.adopt_text(
            case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256)
        )
        body = approve_with_preview(real, case, adoption.adopted_sha256)
        if boundary == "frozen_replay":
            real.approve(case, body)
        record = real._intake(case)
        save_customer_instruction(real._store, case, record.owner)
        state = real._store.get_case(record.case_token or "")
        packet = real._store.get(record.case_token or "", 1)
        history = real._store.destination_decision_history(case, record.owner)
        consent = real._store.address_consent_events(case, record.owner)
        # Read-only status and rejected actions do not migrate or erase anything.
        real.status(case)

        def attempt() -> None:
            if boundary == "preview":
                real.packet_preview(
                    case, s.PacketPreviewRequest(adopted_sha256=body.adopted_sha256)
                )
            else:
                real.approve(case, body)

        with pytest.raises(ServiceConflict, match=r"^destination_verification_required$"):
            attempt()
        assert real._store.get_case(record.case_token or "") == state
        assert real._store.get(record.case_token or "", 1) == packet
        assert real._store.destination_decision_history(case, record.owner) == history
        assert real._store.address_consent_events(case, record.owner) == consent
    finally:
        await client.aclose()
        real._store.close()


async def test_historical_unsent_frozen_packet_refuses_create_and_releases_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(200, "must_not_create", evidence=LetterEvidence.PRODUCTION)])
    client, real, case = await approved(tmp_path, monkeypatch, lob)
    try:
        record = real._intake(case)
        save_customer_instruction(real._store, case, record.owner)
        frozen = real._store.get(record.case_token or "", 1)
        history = real._store.destination_decision_history(case, record.owner)
        payments, transport = stripe(payment_fixture(), payment_fixture("canceled"))
        object.__setattr__(real._deps, "payments", payments)
        real.dispatch(case)
        real.dispatch(case)
        assert not lob.calls
        assert real.status(case).state is CaseState.STOPPED_NO_CHARGE
        assert real.status(case).payment_state is PaymentState.RELEASED
        assert real._store.get(record.case_token or "", 1) == frozen
        assert real._store.destination_decision_history(case, record.owner) == history
        assert not any(call[1].endswith("/capture") for call in transport.calls)
        assert any(call[1].endswith("/cancel") for call in transport.calls)
    finally:
        await client.aclose()
        real._store.close()


async def test_historical_prior_send_intent_reconciles_without_new_create_or_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(None)])
    client, real, case = await approved(tmp_path, monkeypatch, lob)
    try:
        clock = Clock()
        object.__setattr__(real._deps, "clock", clock)
        real.dispatch(case)
        record = real._intake(case)
        token = record.case_token or ""
        previous = real._store.submission_cursor(token, 1)
        assert previous is not None
        assert isinstance(previous.outcome, Uncertain)
        save_customer_instruction(real._store, case, record.owner)
        frozen = real._store.get(token, 1)

        def unavailable(token: str) -> MetadataLookup:
            lob.lookups.append(token)
            return MetadataLookup(authoritative=False)

        monkeypatch.setattr(lob, "find_by_metadata", unavailable)
        clock.now = previous.next_at
        real.dispatch(case)
        cursor = real._store.submission_cursor(token, 1)
        assert cursor is not None
        assert isinstance(cursor.outcome, Uncertain)
        assert len(lob.calls) == 1  # original intent only, no new unverified create
        assert lob.lookups
        assert real._store.get(token, 1) == frozen
        assert real.status(case).payment_state is PaymentState.AUTHORIZED
    finally:
        await client.aclose()
        real._store.close()


async def test_changed_instruction_between_validation_and_lock_prevents_drafting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        original = real._destination_address
        record = real._intake(case)

        def change(*args: object, **kwargs: object) -> object:
            address = original(*args, **kwargs)  # type: ignore[arg-type]
            save_customer_instruction(real._store, case, record.owner)
            return address

        monkeypatch.setattr(real, "_destination_address", change)
        with pytest.raises(ServiceConflict, match=r"^destination_decision_stale$"):
            real.step(case, s.WorkerStep(step_id="draft"))
        assert real._store.get_draft(case) is None
    finally:
        await client.aclose()
        real._store.close()
