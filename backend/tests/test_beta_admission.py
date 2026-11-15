"""Free-beta admission uses real durable storage and never activates a provider."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from parkingbreaker.agencies import IssuerType, ResolvedAgency
from parkingbreaker.beta_store import BetaConflict
from parkingbreaker.payment_records import mutation_key
from parkingbreaker.research_records import agency_json
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)
START = datetime(2026, 11, 1, tzinfo=UTC)
END = datetime(2026, 12, 1, tzinfo=UTC)


def campaign(store: SqliteTransactionStore) -> None:
    store.create_beta_campaign("beta-1", START, END, "identity-fixture-v1", "owner-fixture", NOW)


def candidate(store: SqliteTransactionStore, number: int, jurisdiction: int | None = None) -> str:
    """Trusted qualification fixture, not proof of a real jurisdiction's procedure."""
    token = f"case_{number}"
    issuer = f"CA:issuer-{number}.gov"
    agency = ResolvedAgency(
        issuer,
        f"Fixture issuer {number}",
        "CA",
        (f"issuer-{number}.gov",),
        IssuerType.MUNICIPAL,
        "Parking review",
        "CA",
    )
    store.create_owned_workspace(f"owner-{number}", f"ws_{number}", Workspace(), NOW)
    store.record_intake(
        token,
        f"ws_{number}",
        json.dumps({"fixture": number}),
        json.dumps(
            {"kind": "offer", "issuer": {"issuer_id": issuer}, "tiers": [{"tier": "CERTIFIED"}]}
        ),
        NOW,
        agency_json=agency_json(agency),
    )
    digest = hashlib.sha256(f"qualification-{number}".encode()).hexdigest()
    store.bind_beta_jurisdiction(
        "beta-1",
        issuer,
        f"jurisdiction-{number if jurisdiction is None else jurisdiction}",
        digest,
        NOW,
    )
    store.qualify_beta_candidate("beta-1", token, digest, NOW + timedelta(days=1), NOW)
    return token


def test_randomized_jurisdiction_groups_close_at_five_and_replay_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        campaign(store)
        for number in range(1, 10):
            candidate(store, number, 1 if number in (1, 2) else number)
        calls: list[int] = []

        def last(size: int) -> int:
            calls.append(size)
            return size - 1

        batch = store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW, randbelow=last)
        assert len(batch) == len({item.jurisdiction_key for item in batch}) == 5
        assert "case_9" in {item.intake_id for item in batch}
        assert "case_1" not in {item.intake_id for item in batch}
        assert calls[::2] == [8, 7, 6, 5, 4]
        with pytest.raises(BetaConflict, match="review_required"):
            store.select_beta_batch("beta-1", "batch-2", "approval-2", NOW)
        with pytest.raises(BetaConflict, match="request_reused"):
            store.select_beta_batch("beta-1", "batch-1", "different-approval", NOW)
    with SqliteTransactionStore(path) as store:
        replay = store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW)
        assert replay == batch
        assert all(store.beta_entitlement(item.intake_id) == item for item in batch)


def test_later_batches_require_explicit_review_and_never_reuse_a_first_month_jurisdiction(
    tmp_path: Path,
) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        campaign(store)
        for number in range(1, 9):
            candidate(store, number)
        first = store.select_beta_batch(
            "beta-1", "batch-1", "approval-1", NOW, randbelow=lambda _: 0
        )
        store.review_beta_batch("batch-1", "review-1", NOW)
        store.review_beta_batch("batch-1", "review-1", NOW)
        with pytest.raises(BetaConflict, match="review_reused"):
            store.review_beta_batch("batch-1", "different-review", NOW)
        candidate(store, 9, int(first[0].jurisdiction_key.rsplit("-", 1)[1]))
        second = store.select_beta_batch("beta-1", "batch-2", "approval-2", NOW)
        assert len(second) == 3
        assert {x.jurisdiction_key for x in first}.isdisjoint(x.jurisdiction_key for x in second)
        assert store.beta_entitlement("case_9") is None
        with pytest.raises(BetaConflict, match="review_required"):
            store.select_beta_batch("beta-1", "batch-3", "approval-3", NOW)
        store.review_beta_batch("batch-2", "review-2", NOW)
        with pytest.raises(BetaConflict, match="no_eligible_candidates"):
            store.select_beta_batch("beta-1", "batch-3", "approval-3", NOW)


def test_concurrent_selection_has_one_durable_five_appeal_batch(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        campaign(store)
        for number in range(1, 13):
            candidate(store, number)
    ready = Barrier(2)

    def select(number: int) -> tuple[str, ...] | str:
        with SqliteTransactionStore(path) as store:
            ready.wait(timeout=10)
            try:
                return tuple(
                    x.intake_id
                    for x in store.select_beta_batch(
                        "beta-1",
                        f"batch-{number}",
                        f"approval-{number}",
                        NOW,
                    )
                )
            except BetaConflict as exc:
                return str(exc)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(select, (1, 2)))
    assert sum(isinstance(x, tuple) and len(x) == 5 for x in results) == 1
    assert results.count("beta_batch_review_required") == 1
    with SqliteTransactionStore(path) as store:
        assert store._require_open().execute(
            "SELECT COUNT(*) FROM pb_beta_entitlements"
        ).fetchone() == (5,)


def test_expired_changed_or_payment_started_candidates_cannot_be_admitted(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        campaign(store)
        for number in range(1, 5):
            candidate(store, number)
        db = store._require_open()
        db.execute("UPDATE pb_intake_records SET intake_json='{}' WHERE intake_id='case_1'")
        db.execute(
            "UPDATE pb_beta_candidates SET eligible_until=? WHERE intake_id='case_2'",
            ((NOW - timedelta(seconds=1)).isoformat(),),
        )
        record = store.get_intake("case_3")
        assert record is not None
        store.update_intake("case_3", record.revision, selected_tier="CERTIFIED", case_token=None)
        request = {
            "amount_cents": 2999,
            "currency": "usd",
            "metadata": {
                "case": "case_3",
                "tier": "CERTIFIED",
                "service_amount_cents": "2999",
            },
            "payment_method_id": "pm_fixture",
            "tier": "CERTIFIED",
        }
        store.begin_authorization_attempt(
            "case_3",
            mutation_key("authorization", "case_3", "CERTIFIED:2999", "pm_fixture"),
            "CERTIFIED",
            2999,
            "pm_fixture",
            json.dumps(request, sort_keys=True, separators=(",", ":")),
            NOW,
        )
        admitted = store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW)
        assert [x.intake_id for x in admitted] == ["case_4"]
        with pytest.raises(BetaConflict, match="payment_attempt_exists"):
            store.qualify_beta_candidate("beta-1", "case_3", "a" * 64, NOW + timedelta(days=1), NOW)


def test_failed_selection_rolls_back_and_database_refuses_a_sixth_slot(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        campaign(store)
        for number in range(1, 7):
            candidate(store, number)
        with pytest.raises(ValueError, match="random selection"):
            store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW, randbelow=lambda n: n)
        db = store._require_open()
        assert db.execute("SELECT COUNT(*) FROM pb_beta_batches").fetchone() == (0,)
        store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW, randbelow=lambda _: 0)
        row = list(db.execute("SELECT * FROM pb_beta_entitlements LIMIT 1").fetchone())
        row[0], row[1], row[4] = uuid4().hex, "case_6", "jurisdiction-6"
        with pytest.raises(sqlite3.IntegrityError, match="batch is full"):
            db.execute("INSERT INTO pb_beta_entitlements VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
        assert db.execute("SELECT COUNT(*) FROM pb_beta_entitlements").fetchone() == (5,)


def test_explicit_campaign_window_and_canonical_identity_are_not_silently_rewritten(
    tmp_path: Path,
) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        campaign(store)
        candidate(store, 1)
        campaign(store)
        with pytest.raises(BetaConflict, match="identity_changed"):
            store.bind_beta_jurisdiction("beta-1", "CA:issuer-1.gov", "another", "a" * 64, NOW)
        for at in (START - timedelta(seconds=1), END):
            with pytest.raises(BetaConflict, match="window_closed"):
                store.select_beta_batch("beta-1", "batch-1", "approval-1", at)
        assert store._require_open().execute("SELECT COUNT(*) FROM pb_beta_batches").fetchone() == (
            0,
        )
