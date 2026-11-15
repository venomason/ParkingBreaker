"""Independent T20 acceptance obligations for webhook recovery and deduplication."""

from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker import case_state as cs
from parkingbreaker.api.app import create_app
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import Accepted, LobResponse, Uncertain
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_submission_coordinator import NOW, prepared, step
from test_t17_pieceB_review import distinct_event
from test_t20_backlog_bounds import reduced
from test_t20_webhook_recovery import (
    app_for,
    capture_effect_count,
    letter_body,
)


@pytest.mark.anyio
async def test_second_ambiguous_envelope_is_acknowledged_and_both_remain_durable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ambiguous-ingress.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
    real = app_for(path)
    first = letter_body()
    first["id"] = "ltr_first_ambiguous"
    first_payload, first_headers = distinct_event(
        first, "letter.in_transit", NOW, "evt_first_ambiguous"
    )
    second = letter_body()
    second["id"] = "ltr_second_ambiguous"
    second_payload, second_headers = distinct_event(
        second, "letter.in_transit", NOW, "evt_second_ambiguous"
    )
    try:
        assert real.webhook("lob", first_payload, first_headers)
        original = real._store.backlog_row("lob", "evt_first_ambiguous")
        step(real._store, FakeLobClient([LobResponse(None)]), case)
        before = real._store.get_case(case.case_token)
        async with AsyncClient(
            transport=ASGITransport(app=create_app(real)), base_url="http://test"
        ) as client:
            response = await client.post(
                "/webhooks/lob", content=second_payload, headers=second_headers
            )
        expected = {"evt_first_ambiguous", "evt_second_ambiguous"}
        # Both envelopes must survive before acknowledgement, even while neither
        # letter can safely bind. A 409 with a rolled-back new pin loses this evidence.
        assert {row[1] for row in real._store.pending_webhooks()} == expected
        assert response.status_code == 200
        assert response.json() == {"received": True, "duplicate": False}
        assert real._store.backlog_row("lob", "evt_first_ambiguous") == original
        cursor = real._store.submission_cursor(case.case_token, 1)
        assert cursor is not None
        assert isinstance(cursor.outcome, Uncertain)
        assert cursor.outcome.letter_id is None
        assert real._store.get_case(case.case_token) == before
        assert capture_effect_count(real._store, case) == 0
        assert real._store.tracking_payloads(case.case_token) == ()
        real._replay_webhooks()
        assert {row[1] for row in real._store.pending_webhooks()} == expected
        assert real._store.get_case(case.case_token) == before
        assert capture_effect_count(real._store, case) == 0
    finally:
        real._store.close()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["lob", "stripe"])
async def test_wrong_mode_secret_rejected_before_storage(tmp_path: Path, provider: str) -> None:
    real = app_for(tmp_path / "cases.sqlite")
    object.__setattr__(real._deps, f"{provider}_webhook_secret", "live-mode-secret")
    body, _ = distinct_event(letter_body(), "letter.in_transit", NOW, "evt_wrong_mode")
    stamp = str(int(NOW.timestamp()))
    digest = hmac.new(b"test-mode-secret", stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    headers = (
        {"lob-signature-timestamp": stamp, "lob-signature": digest}
        if provider == "lob"
        else {"stripe-signature": f"t={stamp},v1={digest}"}
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(real)), base_url="http://test"
        ) as client:
            response = await client.post(f"/webhooks/{provider}", content=body, headers=headers)
        assert response.status_code == 400
        assert response.json() == {"detail": "invalid_signature"}
        for table in ("pb_webhooks", "pb_webhook_backlog", "pb_webhook_unmatched"):
            assert real._store._require_open().execute(
                f"SELECT count(*) FROM {table}"
            ).fetchone() == (0,)
    finally:
        real._store.close()


def test_matched_metadata_free_return_reaches_retention_outcome(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        step(store, FakeLobClient([LobResponse(200, "ltr_early")]), case)
    real = app_for(path)
    body = letter_body()
    del body["metadata"]
    payload, headers = distinct_event(body, "letter.returned_to_sender", NOW, "evt_waiting")
    try:
        assert real.webhook("lob", payload, headers)
        object.__setattr__(real._deps, "clock", lambda: NOW + timedelta(days=31))
        for _ in range(3):
            real._replay_webhooks()
            real._replay_case_webhooks(case.case_token)
        assert real._store.pending_webhooks() == (), (
            "matched metadata-free return bypasses 30-day retention"
        )
        assert [(row.event_id, row.reason) for row in real._store.unmatched_webhooks()] == [
            ("evt_waiting", "UNMATCHED_EXPIRED")
        ]
    finally:
        real._store.close()


def _seed_starving_backlog(path: Path) -> str:
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
    real = app_for(path)
    try:
        for index in range(100):
            body = letter_body()
            del body["metadata"]
            payload, headers = distinct_event(
                body,
                "letter.returned_to_sender",
                NOW,
                f"evt_return_{index:03d}",
            )
            assert real.webhook("lob", payload, headers)
        payload, headers = distinct_event(
            letter_body(), "letter.in_transit", NOW, "evt_qualifying_scan"
        )
        assert real.webhook("lob", payload, headers)
        step(real._store, FakeLobClient([LobResponse(200, "ltr_early")]), case)
        return case.case_token
    finally:
        real._store.close()


def _assert_qualifying_scan_replayed(path: Path, case_token: str) -> None:
    with SqliteTransactionStore(path) as store:
        cursor = store.submission_cursor(case_token, 1)
        assert cursor is not None
        assert isinstance(cursor.outcome, Accepted), (
            "first 100 waiting rows starve the qualifying scan forever"
        )


def test_scan_after_hundred_waiting_returns_eventually_replays(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    case_token = _seed_starving_backlog(path)
    real = app_for(path)
    try:
        for _ in range(5):
            real._replay_webhooks()
            real._replay_case_webhooks(case_token)
    finally:
        real._store.close()
    _assert_qualifying_scan_replayed(path, case_token)


def test_scan_beyond_hundred_waiting_survives_reopened_replay_passes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cases.sqlite"
    case_token = _seed_starving_backlog(path)
    for _ in range(5):
        # app_for fixes the clock at NOW; each pass starts with a reopened store.
        real = app_for(path)
        try:
            real._replay_webhooks()
            real._replay_case_webhooks(case_token)
        finally:
            real._store.close()
    _assert_qualifying_scan_replayed(path, case_token)


def test_concurrent_second_letter_return_cannot_return_the_bound_letter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        step(store, FakeLobClient([LobResponse(None)]), case)
    a, b = app_for(path), app_for(path)
    original = b._match_lob_attempt
    body_a, body_b = letter_body(), letter_body()
    body_a["id"], body_b["id"] = "ltr_one", "ltr_two"
    payload_a, headers_a = distinct_event(body_a, "letter.in_transit", NOW, "evt_one")
    payload_b, headers_b = distinct_event(body_b, "letter.returned_to_sender", NOW, "evt_two")

    def interleaved(
        ref: str, token: str, event_type: str
    ) -> tuple[tuple[str, int] | None, str | None]:
        selected = original(ref, token, event_type)
        assert selected[0] == (case.case_token, 1)
        assert a.webhook("lob", payload_a, headers_a)
        cursor = a._store.submission_cursor(case.case_token, 1)
        assert cursor is not None
        assert isinstance(cursor.outcome, Accepted)
        assert cursor.outcome.letter_id == "ltr_one"
        return selected

    monkeypatch.setattr(b, "_match_lob_attempt", interleaved)
    try:
        assert b.webhook("lob", payload_b, headers_b)
        current = b._store.get_case(case.case_token)
        assert current is not None
        assert current.letter_state is cs.CaseState.IN_TRANSIT, (
            "return for ltr_two incorrectly returned ltr_one"
        )
        assert len(b._store.tracking_payloads(case.case_token)) == 1
    finally:
        a._store.close()
        b._store.close()


def test_first_envelope_is_pinned_across_concurrent_cursor_landing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
    a, b = app_for(path), app_for(path)
    original = b._store.backlog_row
    clean = letter_body()
    first = letter_body()
    first["deleted"] = True
    payload_a, headers_a = distinct_event(first, "letter.in_transit", NOW, "evt_race")
    payload_b, headers_b = distinct_event(clean, "letter.in_transit", NOW, "evt_race")

    def interleaved(provider: str, event_id: str) -> object:
        selected = original(provider, event_id)
        assert selected is None
        assert a.webhook("lob", payload_a, headers_a)
        step(a._store, FakeLobClient([LobResponse(200, "ltr_early")]), case)
        return selected

    monkeypatch.setattr(b._store, "backlog_row", interleaved)
    try:
        assert b.webhook("lob", payload_b, headers_b)
        cursor = b._store.submission_cursor(case.case_token, 1)
        assert cursor is not None
        assert isinstance(cursor.outcome, Uncertain), (
            "later envelope replaced the first stored deleted flag"
        )
        assert capture_effect_count(b._store, case) == 0
    finally:
        a._store.close()
        b._store.close()


def test_unmatched_eviction_keeps_final_event_deduplication(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    real = app_for(path)
    payload, headers = distinct_event(letter_body(), "letter.in_transit", NOW, "evt_evicted")
    try:
        assert real.webhook("lob", payload, headers)
        later = NOW + timedelta(days=31)
        object.__setattr__(real._deps, "clock", lambda: later)
        real._replay_webhooks()
        assert real._store.unmatched_webhooks()[0].event_id == "evt_evicted"
        # Keep the real 5000-row retention limit; no constants or source are patched.
        for index in range(5000):
            assert real._store.move_unmatched(
                "lob",
                f"evt_new_{index}",
                reduced(f"evt_new_{index}", f"ltr_new_{index}", None),
                "UNMATCHED_EXPIRED",
                received_at=NOW,
                moved_at=later,
            )
        assert len(real._store.unmatched_webhooks()) == 5000
        assert "evt_evicted" not in {row.event_id for row in real._store.unmatched_webhooks()}
        stamp = str(int(later.timestamp()))
        headers = {
            "lob-signature-timestamp": stamp,
            "lob-signature": hmac.new(
                b"secret", stamp.encode() + b"." + payload, hashlib.sha256
            ).hexdigest(),
        }
        applied = real.webhook("lob", payload, headers)
        assert not applied, (
            "evicting the reduced unmatched record also evicts final-event deduplication"
        )
        assert real._store.pending_webhooks() == ()
    finally:
        real._store.close()
