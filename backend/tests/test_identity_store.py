"""One-use identity links and session persistence on the transaction store."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from parkingbreaker.identity_store import IdentityAccount, IdentityConflict, IdentitySession
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.transaction_store import SqliteTransactionStore

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def digest(number: int) -> str:
    return f"{number:064x}"


def issue(
    store: SqliteTransactionStore,
    *,
    customer_id: str = "customer-1",
    email: str = "Alice@EXAMPLE.COM",
    token: int = 1,
    issued_at: datetime = NOW,
    expires_at: datetime = NOW + timedelta(minutes=15),
) -> IdentityAccount:
    return store.issue_identity_link(
        customer_id=customer_id,
        email=email,
        token_digest=digest(token),
        issued_at=issued_at,
        expires_at=expires_at,
    )


def consume(
    store: SqliteTransactionStore,
    *,
    token: int = 1,
    session: int = 100,
    now: datetime = NOW,
    expires_at: datetime = NOW + timedelta(hours=1),
) -> IdentitySession | None:
    return store.consume_identity_link(
        digest(token),
        session_digest=digest(session),
        csrf_digest=digest(session + 1_000),
        now=now,
        session_expires_at=expires_at,
    )


def test_same_email_keeps_distinct_appeal_scopes_without_alias_rewriting(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "identity.sqlite") as store:
        first = issue(store)
        repeated = issue(
            store, customer_id="different-request-id", email="Alice@example.com", token=2
        )
        distinct_local_case = issue(
            store, customer_id="customer-2", email="alice@example.com", token=3
        )

        assert first.customer_id == "customer-1"
        assert repeated.customer_id == "different-request-id"
        assert first.email == repeated.email == "Alice@example.com"
        assert first.created_at == repeated.created_at == NOW
        assert distinct_local_case.customer_id == "customer-2"
        assert distinct_local_case.email == "alice@example.com"
        assert first.verified_at is None


def test_conflicting_customer_id_or_link_digest_changes_nothing(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "identity.sqlite") as store:
        issue(store)
        with pytest.raises(IdentityConflict, match=r"^identity conflict$"):
            issue(store, customer_id="customer-1", email="other@example.com", token=2)
        with pytest.raises(IdentityConflict, match=r"^identity conflict$"):
            issue(store, customer_id="customer-2", email="other@example.com", token=1)
        assert store.identity_session(digest(100), now=NOW) is None
        accounts = store._require_open().execute("SELECT count(*) FROM pb_identity_accounts")
        links = store._require_open().execute("SELECT count(*) FROM pb_identity_links")
        assert accounts.fetchone()[0] == 1
        assert links.fetchone()[0] == 1


def test_reissued_link_cannot_predate_existing_account(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "identity.sqlite") as store:
        issue(store)
        with pytest.raises(IdentityConflict, match="invalid identity input"):
            issue(
                store,
                token=2,
                issued_at=NOW - timedelta(seconds=1),
                expires_at=NOW + timedelta(minutes=15),
            )
        links = store._require_open().execute("SELECT count(*) FROM pb_identity_links")
        assert links.fetchone()[0] == 1


@pytest.mark.parametrize(
    "email",
    [
        "",
        "   ",
        "no-at",
        "a@@example.com",
        "@example.com",
        "a@localhost",
        "a..b@example.com",
        "a\nb@example.com",
        "a@example.com\r\nBcc:elsewhere@example.com",
        "a@bad-.com",
        "x" * 250 + "@a.com",
    ],
)
def test_rejects_malformed_email_without_writing(tmp_path: Path, email: str) -> None:
    with SqliteTransactionStore(tmp_path / "identity.sqlite") as store:
        with pytest.raises(IdentityConflict):
            issue(store, email=email)
        count = store._require_open().execute("SELECT count(*) FROM pb_identity_accounts")
        assert count.fetchone()[0] == 0


def test_invalid_digests_ids_and_time_order_are_refused(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "identity.sqlite") as store:
        for token in ("A" * 64, "0" * 63, "g" * 64):
            with pytest.raises(IdentityConflict):
                store.issue_identity_link(
                    customer_id="customer-1",
                    email="a@example.com",
                    token_digest=token,
                    issued_at=NOW,
                    expires_at=NOW + timedelta(minutes=1),
                )
        with pytest.raises(IdentityConflict):
            issue(store, customer_id="  ")
        with pytest.raises(IdentityConflict):
            issue(store, issued_at=NOW, expires_at=NOW)
        with pytest.raises(IdentityConflict):
            issue(store, issued_at=datetime(2026, 10, 17), expires_at=NOW + timedelta(minutes=1))
        count = store._require_open().execute("SELECT count(*) FROM pb_identity_accounts")
        assert count.fetchone()[0] == 0


def test_future_link_exact_expiry_replay_and_session_collision_rollback(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "identity.sqlite") as store:
        issue(store)
        assert consume(store, now=NOW - timedelta(seconds=1)) is None
        assert consume(store, now=NOW + timedelta(minutes=15)) is None
        session = consume(store)
        assert session is not None
        assert session.customer_id == "customer-1"
        assert store.identity_session(digest(100), now=NOW) == session
        assert consume(store, session=102) is None

        issue(store, token=2, email="second@example.com", customer_id="customer-2")
        with pytest.raises(IdentityConflict):
            consume(store, token=2, session=100)
        # The failed session insert rolled back verification and link consumption.
        assert (
            store._require_open()
            .execute("SELECT consumed_at FROM pb_identity_links WHERE token_digest=?", (digest(2),))
            .fetchone()[0]
            is None
        )
        assert (
            store._require_open()
            .execute("SELECT verified_at FROM pb_identity_accounts WHERE customer_id='customer-2'")
            .fetchone()[0]
            is None
        )
        recovered = consume(store, token=2, session=200)
        assert recovered is not None
        assert recovered.customer_id == "customer-2"


def test_session_expiry_revoke_idempotence_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "identity.sqlite"
    store = SqliteTransactionStore(path)
    issue(store)
    session = consume(store, expires_at=NOW + timedelta(minutes=5))
    assert session is not None
    store.close()

    with SqliteTransactionStore(path) as reopened:
        assert reopened.identity_session(digest(100), now=NOW - timedelta(seconds=1)) is None
        assert reopened.identity_session(digest(100), now=NOW + timedelta(minutes=4)) == session
        assert reopened.identity_session(digest(100), now=NOW + timedelta(minutes=5)) is None
        reopened.revoke_identity_session(digest(100), now=NOW + timedelta(minutes=1))
        reopened.revoke_identity_session(digest(100), now=NOW + timedelta(minutes=2))
        assert reopened.identity_session(digest(100), now=NOW + timedelta(minutes=2)) is None
        row = (
            reopened._require_open()
            .execute(
                "SELECT revoked_at FROM pb_identity_sessions WHERE session_digest=?", (digest(100),)
            )
            .fetchone()
        )
        assert row[0] == (NOW + timedelta(minutes=1)).isoformat(timespec="microseconds")
        reopened.revoke_identity_session(digest(999), now=NOW)


def test_rejects_session_expiry_not_in_future(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "identity.sqlite") as store:
        issue(store)
        with pytest.raises(IdentityConflict):
            consume(store, expires_at=NOW)
        row = store._require_open().execute("SELECT consumed_at FROM pb_identity_links")
        assert row.fetchone()[0] is None


def test_consume_rejects_persisted_link_predating_account_and_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "identity.sqlite"
    with SqliteTransactionStore(path) as store:
        issue(store)
    database = sqlite3.connect(path)
    database.execute(
        "UPDATE pb_identity_links SET issued_at=? WHERE token_digest=?",
        ((NOW - timedelta(seconds=1)).isoformat(timespec="microseconds"), digest(1)),
    )
    database.commit()
    database.close()

    with (
        SqliteTransactionStore(path) as store,
        pytest.raises(StorageCorruption, match="identity link predates its account"),
    ):
        consume(store)

    with SqliteTransactionStore(path) as store:
        assert (
            store._require_open()
            .execute("SELECT consumed_at FROM pb_identity_links WHERE token_digest=?", (digest(1),))
            .fetchone()[0]
            is None
        )
        assert (
            store._require_open()
            .execute("SELECT verified_at FROM pb_identity_accounts WHERE customer_id='customer-1'")
            .fetchone()[0]
            is None
        )
        count = store._require_open().execute("SELECT count(*) FROM pb_identity_sessions")
        assert count.fetchone()[0] == 0


def test_session_requires_verification_at_or_before_creation(tmp_path: Path) -> None:
    path = tmp_path / "identity.sqlite"
    with SqliteTransactionStore(path) as store:
        issue(store)
        assert consume(store) is not None
    database = sqlite3.connect(path)
    database.execute(
        "UPDATE pb_identity_accounts SET verified_at=?",
        ((NOW + timedelta(minutes=1)).isoformat(timespec="microseconds"),),
    )
    database.commit()
    database.close()

    with SqliteTransactionStore(path) as store:
        assert store.identity_session(digest(100), now=NOW + timedelta(minutes=2)) is None


def test_schema_version_and_unversioned_identity_tables_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "identity.sqlite"
    SqliteTransactionStore(path).close()
    database = sqlite3.connect(path)
    database.execute("UPDATE pb_store_versions SET version=99 WHERE component='identity'")
    database.commit()
    database.close()
    with pytest.raises(StorageCorruption, match="unsupported identity schema"):
        SqliteTransactionStore(path)

    path2 = tmp_path / "unversioned.sqlite"
    SqliteTransactionStore(path2).close()
    database = sqlite3.connect(path2)
    database.execute("DELETE FROM pb_store_versions WHERE component='identity'")
    database.execute("CREATE TABLE pb_identity_extra (value TEXT)")
    database.commit()
    database.close()
    with pytest.raises(StorageCorruption, match="unversioned identity schema"):
        SqliteTransactionStore(path2)


def test_malformed_identity_schema_and_rows_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "identity.sqlite"
    with SqliteTransactionStore(path) as store:
        issue(store)
    database = sqlite3.connect(path)
    database.execute("ALTER TABLE pb_identity_sessions ADD COLUMN unexpected TEXT")
    database.commit()
    database.close()
    with pytest.raises(StorageCorruption, match="malformed identity schema"):
        SqliteTransactionStore(path)

    path2 = tmp_path / "bad-row.sqlite"
    with SqliteTransactionStore(path2) as store:
        issue(store)
        consume(store)
    database = sqlite3.connect(path2)
    database.execute("UPDATE pb_identity_sessions SET csrf_digest=?", ("A" * 64,))
    database.commit()
    database.close()
    with (
        SqliteTransactionStore(path2) as store,
        pytest.raises(StorageCorruption, match="malformed identity session"),
    ):
        store.identity_session(digest(100), now=NOW)

    path3 = tmp_path / "bad-account.sqlite"
    with SqliteTransactionStore(path3) as store:
        issue(store)
        consume(store)
    database = sqlite3.connect(path3)
    database.execute("UPDATE pb_identity_accounts SET email='not-an-email'")
    database.commit()
    database.close()
    with (
        SqliteTransactionStore(path3) as store,
        pytest.raises(StorageCorruption, match="malformed identity account"),
    ):
        store.identity_session(digest(100), now=NOW)


def test_two_connections_can_consume_only_once(tmp_path: Path) -> None:
    path = tmp_path / "identity.sqlite"
    with SqliteTransactionStore(path) as store:
        issue(store)
    barrier = Barrier(2)

    def attempt(session_number: int) -> IdentitySession | None:
        with SqliteTransactionStore(path) as store:
            barrier.wait(timeout=5)
            return consume(store, session=session_number)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, (300, 400)))
    successes = [outcome for outcome in outcomes if outcome is not None]
    assert len(successes) == 1
    with SqliteTransactionStore(path) as store:
        for session_number in (300, 400):
            value = store.identity_session(digest(session_number), now=NOW)
            assert (value is not None) == (successes[0].session_digest == digest(session_number))


def test_v1_migration_preserves_sessions_links_and_foreign_keys(tmp_path: Path) -> None:
    from parkingbreaker.identity_store import _IDENTITY_DDL

    path = tmp_path / "migration.sqlite"
    with SqliteTransactionStore(path) as store:
        issue(store)
        session = consume(store)
        assert session is not None
        before = store._require_open().execute("SELECT * FROM pb_identity_sessions").fetchall()
    database = sqlite3.connect(path)
    database.execute("PRAGMA foreign_keys=OFF")
    database.execute(
        _IDENTITY_DDL[0]
        .replace("pb_identity_accounts", "legacy_accounts", 1)
        .replace("email TEXT NOT NULL,", "email TEXT NOT NULL UNIQUE,")
    )
    database.execute("INSERT INTO legacy_accounts SELECT * FROM pb_identity_accounts")
    database.execute("DROP TABLE pb_identity_accounts")
    database.execute("ALTER TABLE legacy_accounts RENAME TO pb_identity_accounts")
    database.execute("UPDATE pb_store_versions SET version=1 WHERE component='identity'")
    database.commit()
    database.close()
    with SqliteTransactionStore(path) as migrated:
        assert migrated.identity_session(digest(100), now=NOW) == session
        assert (
            migrated._require_open().execute("SELECT * FROM pb_identity_sessions").fetchall()
            == before
        )
        assert migrated._require_open().execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert migrated._require_open().execute("PRAGMA foreign_key_check").fetchall() == []
        issue(migrated, customer_id="appeal-2", token=2)
    with SqliteTransactionStore(path) as reopened:
        assert reopened.identity_session(digest(100), now=NOW) == session


def test_legacy_multi_workspace_link_is_not_consumed(tmp_path: Path) -> None:
    from parkingbreaker.workspace import Workspace

    with SqliteTransactionStore(tmp_path / "legacy.sqlite") as store:
        issue(store)
        for workspace in ("one", "two"):
            store.save_workspace(workspace, Workspace(), NOW)
            store.set_workspace_owner(workspace, "customer-1", NOW)
        assert consume(store) is None
        assert store._require_open().execute(
            "SELECT consumed_at FROM pb_identity_links"
        ).fetchone() == (None,)


def test_restart_lineage_is_not_legacy_ambiguity(tmp_path: Path) -> None:
    """A restart chain keeps one unlineaged root, so the session survives."""
    from parkingbreaker.workspace import Workspace

    with SqliteTransactionStore(tmp_path / "lineage.sqlite") as store:
        issue(store)
        assert consume(store) is not None
        for workspace in ("root", "child"):
            store.save_workspace(workspace, Workspace(), NOW)
            store.set_workspace_owner(workspace, "customer-1", NOW)
        store.record_workspace_lineage("child", "root", now=NOW)
        assert store.identity_session(digest(100), now=NOW) is not None

        # One unlinked extra workspace is legacy ambiguity again: refuse the scope.
        store.save_workspace("legacy", Workspace(), NOW)
        store.set_workspace_owner("legacy", "customer-1", NOW)
        assert store.identity_session(digest(100), now=NOW) is None
