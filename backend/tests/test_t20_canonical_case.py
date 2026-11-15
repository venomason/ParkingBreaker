"""Canonical-envelope race: retain the first case and the existing duplicate contract.

The historical scratch probe incorrectly required a new-record result for the loser
while the winner was already parked and still had no submission cursor. The public
API maps False to received=True, duplicate=True. The safety assertions below precede
that boolean assertion and additionally prove recovery by the rightful case.
"""

import asyncio
import json
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.api.app import create_app
from parkingbreaker.lob_submission import Accepted, LobResponse
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.webhook_backlog_store import BacklogRow
from test_submission_coordinator import NOW
from test_t17_pieceB_review import distinct_event
from test_t20_webhook_recovery import (
    app_for,
    capture_effect_count,
    land_cursor,
    numbered_case,
)


def test_concurrent_first_envelope_keeps_original_case_and_duplicate_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        first_case = numbered_case(store, 1)
        second_case = numbered_case(store, 2)
    a, b = app_for(path), app_for(path)
    original = b._store.backlog_row
    body_a: dict[str, object] = {
        "id": "ltr_first",
        "metadata": {"submission_token": "tok-1"},
    }
    body_b: dict[str, object] = {
        "id": "ltr_second",
        "metadata": {"submission_token": "tok-2"},
    }
    payload_a, headers_a = distinct_event(body_a, "letter.in_transit", NOW, "evt_shared")
    payload_b, headers_b = distinct_event(body_b, "letter.in_transit", NOW, "evt_shared")
    first_row: BacklogRow | None = None
    before_a: CaseRecord | None = None
    before_b: CaseRecord | None = None
    history_a: tuple[str, ...] = ()
    history_b: tuple[str, ...] = ()

    def interleaved(provider: str, event_id: str) -> BacklogRow | None:
        nonlocal first_row, before_a, before_b, history_a, history_b
        selected = original(provider, event_id)
        assert selected is None
        assert a.webhook("lob", payload_a, headers_a)
        first_row = a._store.backlog_row(provider, event_id)
        assert first_row is not None
        assert json.loads(first_row.payload)["body"]["id"] == "ltr_first"
        land_cursor(a._store, second_case, LobResponse(200, "ltr_second"))
        before_a = a._store.get_case(first_case.case_token)
        before_b = a._store.get_case(second_case.case_token)
        history_a = tuple(
            row.event_name for row in a._store.transition_history(first_case.case_token)
        )
        history_b = tuple(
            row.event_name for row in a._store.transition_history(second_case.case_token)
        )
        return selected

    monkeypatch.setattr(b._store, "backlog_row", interleaved)
    try:
        recorded = b.webhook("lob", payload_b, headers_b)
        monkeypatch.setattr(b._store, "backlog_row", original)
        # Preserve every original cross-case safety obligation, then add preservation,
        # HTTP semantics and restart/recovery proof. A mere False return cannot pass.
        assert b._store.tracking_payloads(second_case.case_token) == (), (
            "first stored case-A event was bound to case B by concurrent redelivery"
        )
        assert capture_effect_count(b._store, second_case) == 0
        assert first_row is not None
        assert original("lob", "evt_shared") == first_row
        assert (
            b._store._require_open()
            .execute(
                "SELECT case_token FROM pb_webhooks WHERE provider=? AND event_id=?",
                ("lob", "evt_shared"),
            )
            .fetchall()
            == []
        )
        assert b._store.pending_webhooks() == (("lob", "evt_shared", first_row.payload),)
        assert b._store.submission_cursor(first_case.case_token, 1) is None
        assert b._store.get_case(first_case.case_token) == before_a
        assert b._store.get_case(second_case.case_token) == before_b
        assert (
            tuple(row.event_name for row in b._store.transition_history(first_case.case_token))
            == history_a
        )
        assert (
            tuple(row.event_name for row in b._store.transition_history(second_case.case_token))
            == history_b
        )
        assert b._store.tracking_payloads(first_case.case_token) == ()
        assert capture_effect_count(b._store, first_case) == 0
        # Winning A is already parked but cannot bind without its own cursor. No new
        # record was inserted or bound by B; the public result must identify a duplicate.
        assert recorded is False

        async def assert_http_duplicate() -> None:
            async with AsyncClient(
                transport=ASGITransport(app=create_app(b)), base_url="http://test"
            ) as client:
                response = await client.post("/webhooks/lob", content=payload_b, headers=headers_b)
            assert response.status_code == 200
            assert response.json() == {"received": True, "duplicate": True}

        asyncio.run(assert_http_duplicate())
        assert original("lob", "evt_shared") == first_row
        assert capture_effect_count(b._store, first_case) == 0
        assert capture_effect_count(b._store, second_case) == 0
    finally:
        a._store.close()
        b._store.close()

    real = app_for(path)
    try:
        assert real._store.backlog_row("lob", "evt_shared") == first_row
        land_cursor(real._store, first_case, LobResponse(200, "ltr_first"))
        real._replay_case_webhooks(first_case.case_token)
        cursor = real._store.submission_cursor(first_case.case_token, 1)
        assert cursor is not None
        assert isinstance(cursor.outcome, Accepted)
        assert cursor.outcome.letter_id == "ltr_first"
        assert capture_effect_count(real._store, first_case) == 1
        assert real._store.tracking_payloads(second_case.case_token) == ()
        assert capture_effect_count(real._store, second_case) == 0
        assert real._store.get_case(second_case.case_token) == before_b
        assert real._store._require_open().execute(
            "SELECT case_token FROM pb_webhooks WHERE provider=? AND event_id=?",
            ("lob", "evt_shared"),
        ).fetchall() == [(first_case.case_token,)]
        assert real._store.pending_webhooks() == ()
        saved = real._store.tracking_payloads(first_case.case_token)
        assert len(saved) == 1
        assert json.loads(saved[0])["letter_id"] == "ltr_first"
        assert json.loads(saved[0])["event"] == "letter.in_transit"
        assert real.webhook("lob", payload_a, headers_a) is False
        assert real.webhook("lob", payload_b, headers_b) is False
        real._replay_case_webhooks(first_case.case_token)
        assert capture_effect_count(real._store, first_case) == 1
        assert capture_effect_count(real._store, second_case) == 0
        assert len(real._store.tracking_payloads(first_case.case_token)) == 1
        assert real._store.get_case(second_case.case_token) == before_b
    finally:
        real._store.close()
