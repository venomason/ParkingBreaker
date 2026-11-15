from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parkingbreaker.disputes import DisputeStatus, dispute_fact
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.oplog.appeal_events import AppealEvent, AppealEventKind
from parkingbreaker.review_queue import Resolution, ReviewKind, review_items
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution
from test_case_service import stripe
from test_disputes_ledger import CREATED, _dispute, _event, _warning
from test_effect_dispatch import approved, dispatch, payment_fixture

AT = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_disputes_appear_once_and_only_the_owner_resolves_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(200, "ltr_original", evidence=LetterEvidence.PRODUCTION)])
    api, real, _case = await approved(tmp_path, monkeypatch, lob)
    payments, _transport = stripe(payment_fixture(), payment_fixture("succeeded"))
    object.__setattr__(real._deps, "payments", payments)
    object.__setattr__(real._deps, "stripe_webhook_secret", "secret")
    try:
        await dispatch(api)
        await dispatch(api)
        assert real._store.open_review_items() == ()
        for event_id, kind, obj in (
            ("evt_d1", "charge.dispute.created", _dispute()),
            ("evt_w1", "radar.early_fraud_warning.created", _warning()),
            ("evt_d2", "charge.dispute.closed", _dispute("lost")),
        ):
            payload, headers = _event(event_id, kind, obj)
            assert real.webhook("stripe", payload, headers) is True
            assert real.webhook("stripe", payload, headers) is False
    finally:
        await api.aclose()
    store = real._store
    items = store.open_review_items()
    assert [(item.item_id, item.kind, item.dispute_status) for item in items] == [
        ("dispute:dp_Dispute123", ReviewKind.DISPUTE, DisputeStatus.LOST),
        ("fraud_warning:issfr_Warning123", ReviewKind.FRAUD_WARNING, None),
    ]
    with pytest.raises(PermissionError, match="owner"):
        store.resolve_review_item(items[0].item_id, Resolution.ACKNOWLEDGED, "worker", AT)
    with pytest.raises(ValueError, match="unknown review item"):
        store.resolve_review_item("dispute:dp_Other", Resolution.ACKNOWLEDGED, "owner", AT)
    assert store.resolve_review_item(items[0].item_id, Resolution.ACKNOWLEDGED, "owner", AT)
    assert not store.resolve_review_item(items[0].item_id, Resolution.DISMISSED, "owner", AT)
    (decision,) = store.review_decisions()
    assert (
        decision.item_id,
        decision.resolution,
        decision.actor,
        decision.resolved_at,
        decision.expires_at,
    ) == (items[0].item_id, Resolution.ACKNOWLEDGED, "owner", AT, None)
    assert items[0].authority == "INFORMATIONAL"
    assert store.current_setting("ad_daily_cap_cents").value == 0
    for expiry in (AT, AT.replace(tzinfo=None)):
        with pytest.raises(ValueError, match="expiry"):
            store.resolve_review_item(
                items[1].item_id, Resolution.ACKNOWLEDGED, "owner", AT, expires_at=expiry
            )

    reopened = SqliteTransactionStore(store.path)
    try:
        assert [item.item_id for item in reopened.open_review_items()] == [items[1].item_id]
        assert len(reopened.review_queue()) == 2
        assert reopened.review_decisions() == (decision,)
        assert reopened.resolve_review_item(
            items[1].item_id,
            Resolution.ACKNOWLEDGED,
            "owner",
            AT,
            expires_at=AT + timedelta(hours=1),
        )
        assert reopened.review_decisions()[1].expires_at == AT + timedelta(hours=1)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            reopened._require_open().execute("DELETE FROM pb_review_resolution_expiries")
        database = reopened._require_open()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            database.execute("UPDATE pb_review_resolutions SET resolution='DISMISSED'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            database.execute("DELETE FROM pb_review_resolutions")
    finally:
        reopened.close()


def test_written_off_charges_and_replays_map_to_stable_items() -> None:
    events = (
        AppealEvent(AppealEventKind.CARD_AUTHORIZED, AT),
        AppealEvent(AppealEventKind.CHARGE_REQUESTED, AT),
        AppealEvent(AppealEventKind.WRITTEN_OFF, AT),
    )
    facts = (
        dispute_fact("charge.dispute.updated", _dispute("warning_closed"), CREATED + 1),
        dispute_fact("charge.dispute.created", _dispute("warning_needs_response"), CREATED),
    )
    items = review_items("case_a", events, facts)
    assert [(item.item_id, item.dispute_status) for item in items] == [
        ("dispute:dp_Dispute123", DisputeStatus.WARNING_CLOSED),
        ("written_off:case_a", None),
    ]
    assert review_items("case_a", events, facts + facts) == items
    assert all(item.deadline_margin_hours is None for item in items)
    assert review_items("case_a", events[:2], ()) == ()
