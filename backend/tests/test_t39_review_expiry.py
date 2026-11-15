from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parkingbreaker.settings_registry import Verdict
from parkingbreaker.transaction_store import SqliteTransactionStore

AT = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.mark.parametrize("expiry", [None, AT, AT.replace(tzinfo=None)])
def test_risky_proposal_requires_future_aware_expiry(
    tmp_path: Path, expiry: datetime | None
) -> None:
    with SqliteTransactionStore(tmp_path / "s.db") as store:
        result = store.propose_setting(
            "ad_daily_cap_cents",
            500,
            proposer="worker",
            evidence="facts",
            now=AT,
            expires_at=expiry,
        )
        assert result.verdict is Verdict.REFUSED
        assert store.open_setting_proposals() == ()


@pytest.mark.parametrize(("seconds", "outcome"), [(59, "APPROVED"), (60, "STALE"), (61, "STALE")])
def test_expiry_boundary(tmp_path: Path, seconds: int, outcome: str) -> None:
    with SqliteTransactionStore(tmp_path / "s.db") as store:
        store.propose_setting(
            "ad_daily_cap_cents",
            500,
            proposer="worker",
            evidence="facts",
            now=AT,
            expires_at=AT + timedelta(seconds=60),
        )
        (proposal,) = store.open_setting_proposals()
        assert (
            store.decide_setting_proposal(
                proposal.proposal_id,
                approve=True,
                actor="owner",
                now=AT + timedelta(seconds=seconds),
            )
            == outcome
        )
        assert store.current_setting("ad_daily_cap_cents").value == (
            500 if outcome == "APPROVED" else 0
        )


def test_legacy_and_expired_proposals_do_not_block_replacement(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    with SqliteTransactionStore(path) as store:
        database = store._require_open()
        database.execute(
            "INSERT INTO pb_setting_proposals VALUES "
            "('legacy', 'ad_daily_cap_cents', 0, 500, 'worker', 'facts', ?)",
            (AT.isoformat(),),
        )
        assert (
            store.propose_setting(
                "ad_daily_cap_cents",
                500,
                proposer="worker",
                evidence="facts",
                now=AT,
                expires_at=AT + timedelta(minutes=1),
            ).verdict
            is Verdict.NEEDS_OWNER
        )
        assert (
            store.decide_setting_proposal("legacy", approve=True, actor="owner", now=AT) == "STALE"
        )
        assert (
            store.propose_setting(
                "ad_daily_cap_cents",
                500,
                proposer="worker",
                evidence="replacement",
                now=AT + timedelta(minutes=1),
                expires_at=AT + timedelta(minutes=2),
            ).verdict
            is Verdict.NEEDS_OWNER
        )
    with SqliteTransactionStore(path) as store:
        assert len(store.open_setting_proposals()) == 2
        assert len(store.open_review_items()) == 2
        assert all(item.expires_at is not None for item in store.open_review_items())
        assert store.current_setting("ad_daily_cap_cents").value == 0
        import sqlite3

        for action in (
            "UPDATE pb_setting_proposal_expiries SET expires_at='x'",
            "DELETE FROM pb_setting_proposal_expiries",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                store._require_open().execute(action)


def test_proposal_sidecar_failure_rolls_back(tmp_path: Path) -> None:
    import sqlite3

    with SqliteTransactionStore(tmp_path / "s.db") as store:
        database = store._require_open()
        database.execute(
            "CREATE TRIGGER fail_expiry BEFORE INSERT ON pb_setting_proposal_expiries "
            "BEGIN SELECT RAISE(ABORT, 'probe'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="probe"):
            store.propose_setting(
                "ad_daily_cap_cents",
                500,
                proposer="worker",
                evidence="facts",
                now=AT,
                expires_at=AT + timedelta(minutes=1),
            )
        assert store.open_setting_proposals() == ()
        assert not database.in_transaction


def test_legacy_schema_reopen_preserves_rows_without_inventing_expiry(tmp_path: Path) -> None:
    import sqlite3

    from parkingbreaker.review_queue import ReviewAuthority

    path = tmp_path / "legacy.db"
    with SqliteTransactionStore(path) as store:
        database = store._require_open()
        database.execute(
            "INSERT INTO pb_setting_proposals VALUES "
            "('legacy', 'ad_daily_cap_cents', 0, 500, 'old-worker', 'old-facts', ?)",
            (AT.isoformat(),),
        )
        database.execute(
            "INSERT INTO pb_review_resolutions VALUES (?, ?, ?, ?)",
            ("fraud_warning:issfr_Legacy", "ACKNOWLEDGED", "owner", AT.isoformat()),
        )
        original = {
            table: database.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("pb_setting_proposals", "pb_review_resolutions")
        }
    # Reproduce the actual v1 shape: historical rows, no additive expiry components.
    with sqlite3.connect(path) as database:
        for table in ("pb_setting_proposal_expiries", "pb_review_resolution_expiries"):
            database.execute(f"DROP TABLE {table}")
        database.execute(
            "DELETE FROM pb_store_versions WHERE component IN ('settings_expiry', 'review_expiry')"
        )
    with SqliteTransactionStore(path) as store:
        database = store._require_open()
        for table, rows in original.items():
            assert database.execute(f"SELECT * FROM {table}").fetchall() == rows
        (proposal,) = store.open_setting_proposals()
        (decision,) = store.review_decisions()
        assert proposal.expires_at is None
        assert decision.expires_at is None
        assert decision.authority is ReviewAuthority.INFORMATIONAL
        assert decision.actor == "owner"
        assert decision.resolved_at == AT
        assert database.execute("SELECT * FROM pb_setting_proposal_expiries").fetchall() == []
        assert database.execute("SELECT * FROM pb_review_resolution_expiries").fetchall() == []
        assert (
            store.decide_setting_proposal("legacy", approve=True, actor="owner", now=AT) == "STALE"
        )
        assert store.current_setting("ad_daily_cap_cents").value == 0


def test_informational_resolution_expiry_is_atomic_and_never_authorizes(tmp_path: Path) -> None:
    import sqlite3

    from parkingbreaker.disputes import dispute_fact
    from parkingbreaker.review_queue import Resolution, ReviewAuthority
    from parkingbreaker.workspace import Workspace
    from test_disputes_ledger import CREATED, _warning
    from test_stripe_payment import REF

    with SqliteTransactionStore(tmp_path / "info.db") as store:
        store.save_workspace("info-workspace", Workspace(), AT)
        case = store.start_case("info-workspace", REF, AT, AT + timedelta(days=7))
        fact = dispute_fact("radar.early_fraud_warning.created", _warning(), CREATED)
        assert store.record_webhook(
            "stripe",
            "evt_Info",
            case.case_token,
            fact.to_json(),
            occurred_at=AT,
            new_effect_id=lambda: "synthetic-effect",
        )
        (item,) = store.open_review_items()
        assert item.authority is ReviewAuthority.INFORMATIONAL
        before = store.get_case(case.case_token)
        database = store._require_open()
        database.execute(
            "CREATE TRIGGER fail_review_expiry BEFORE INSERT ON pb_review_resolution_expiries "
            "BEGIN SELECT RAISE(ABORT, 'expiry-write-failed'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="expiry-write-failed"):
            store.resolve_review_item(
                item.item_id,
                Resolution.ACKNOWLEDGED,
                "owner",
                AT,
                expires_at=AT + timedelta(minutes=1),
            )
        assert store.review_decisions() == ()
        assert not database.in_transaction
        database.execute("DROP TRIGGER fail_review_expiry")
        assert store.resolve_review_item(
            item.item_id,
            Resolution.ACKNOWLEDGED,
            "owner",
            AT,
            expires_at=AT + timedelta(minutes=1),
        )
        (decision,) = store.review_decisions()
        assert decision.item_id == item.item_id
        assert decision.expires_at == AT + timedelta(minutes=1)
        assert decision.authority is ReviewAuthority.INFORMATIONAL
        assert store.get_case(case.case_token) == before
        assert store.current_setting("ad_daily_cap_cents").value == 0
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            database.execute("DELETE FROM pb_review_resolution_expiries")
