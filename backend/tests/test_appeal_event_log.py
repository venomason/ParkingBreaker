from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parkingbreaker.case_state import StopFamily
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.oplog.appeal_events import (
    AppealEventKind,
    AppealOutcome,
    appeal_events,
    appeal_telemetry,
)
from parkingbreaker.transaction_records import TransitionRecord
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_case_service import fake_resolution as fake_resolution
from test_case_service import stripe
from test_effect_dispatch import WORKER, approved, dispatch, payment_fixture
from test_effect_dispatch import anyio_backend as anyio_backend

T0 = datetime(2026, 10, 15, 21, 0, tzinfo=UTC)


def _row(
    revision: int, machine: str, source: str, target: str, event_json: str = "{}"
) -> TransitionRecord:
    return TransitionRecord(
        "case",
        revision,
        machine,
        source,
        target,
        "Event",
        event_json,
        T0 + timedelta(hours=revision),
    )


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_mailed_and_charged_appeal_has_one_log_after_replays_and_restart(
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
    payments, _transport = stripe(payment_fixture(), payment_fixture("succeeded"))
    object.__setattr__(real._deps, "payments", payments)
    try:
        step = await client.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "dispatch"}
        )
        assert step.status_code == 200
        await dispatch(client)
        await dispatch(client)
    finally:
        await client.aclose()
    token = real._intake(case).case_token
    assert token is not None
    record = real._store._payment_case(token)
    events = appeal_events(record.authorized_at, real._store.transition_history(token))
    assert [event.kind for event in events] == [
        AppealEventKind.CARD_AUTHORIZED,
        AppealEventKind.RESEARCH_VERIFIED,
        AppealEventKind.DRAFT_READY,
        AppealEventKind.PACKET_FROZEN,
        AppealEventKind.MAIL_SUBMITTED,
        AppealEventKind.MAILED,
        AppealEventKind.CHARGE_REQUESTED,
        AppealEventKind.CHARGED,
    ]
    assert len(lob.calls) == 1
    reopened = SqliteTransactionStore(real._store.path)
    try:
        again = appeal_events(record.authorized_at, reopened.transition_history(token))
    finally:
        reopened.close()
    assert again == events
    telemetry = appeal_telemetry(events)
    assert telemetry.outcome is AppealOutcome.CHARGED
    assert (telemetry.mail_attempts, telemetry.returned, telemetry.stop_family) == (1, False, None)
    assert (telemetry.hours_to_mailed, telemetry.hours_to_charged) == (0, 0)
    published = repr(asdict(telemetry))
    for secret in (token, case, "ltr_original", "pi_", "ch_", "940000001", "2999", "2026"):
        assert secret not in published


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_case_history_rejects_update_and_delete_after_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case = await approved(tmp_path, monkeypatch, FakeLobClient())
    await client.aclose()
    token = real._intake(case).case_token
    assert token is not None
    before = real._store.transition_history(token)
    assert before
    reopened = SqliteTransactionStore(real._store.path)
    try:
        database = reopened._require_open()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            database.execute(
                "UPDATE pb_case_history SET target='DELIVERED' WHERE case_token=?", (token,)
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            database.execute("DELETE FROM pb_case_history WHERE case_token=?", (token,))
        assert reopened.transition_history(token) == before
    finally:
        reopened.close()


def test_stopped_appeal_releases_the_hold_without_charge() -> None:
    events = appeal_events(
        T0,
        [
            _row(2, "letter", "FROZEN", "SUBMITTING"),
            _row(3, "letter", "SUBMITTING", "STOPPED_NO_CHARGE", '{"reason":"LOB_REJECTED"}'),
            _row(3, "payment", "AUTHORIZED", "RELEASED"),
        ],
    )
    assert [event.kind for event in events][-2:] == [
        AppealEventKind.STOPPED,
        AppealEventKind.HOLD_RELEASED,
    ]
    telemetry = appeal_telemetry(events)
    assert telemetry.outcome is AppealOutcome.RELEASED_NO_CHARGE
    assert telemetry.stop_family is StopFamily.TRANSACTION_FAILURE
    assert (telemetry.hours_to_mailed, telemetry.hours_to_charged) == (None, None)


def test_unmapped_or_unknown_history_is_rejected_not_dropped() -> None:
    with pytest.raises(ValueError, match="NOT_A_STATE"):
        appeal_events(T0, [_row(2, "letter", "INTAKE", "NOT_A_STATE")])
    with pytest.raises(ValueError, match="no milestone"):
        appeal_events(T0, [_row(2, "letter", "RESEARCHING", "INTAKE")])
    with pytest.raises(ValueError, match="machine"):
        appeal_events(T0, [_row(2, "ledger", "A", "B")])
    with pytest.raises(ValueError, match="card hold"):
        appeal_telemetry(())
