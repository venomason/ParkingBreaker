"""Visitor price recognition does not grant private appeal authority."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parkingbreaker.customer_access import AccessService, token_digest
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.prices import PRICE_ARMS
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_customer_auth import credential

AT = datetime(2026, 11, 1, 12, tzinfo=UTC)


def test_shared_price_preserves_distinct_private_appeal_scopes_and_reopens(tmp_path: Path) -> None:
    path = tmp_path / "prices.db"
    raw = credential(60)
    with SqliteTransactionStore(path) as store:
        price = store.create_pricing_assignment(token_digest(raw), now=AT)
        assert store.create_pricing_assignment(token_digest(raw), now=AT) == price
        access = AccessService(store, clock=lambda: AT)
        one = access.start_appeal(pricing_token=raw)
        two = access.start_appeal(pricing_token=raw)
        assert one.customer_id != two.customer_id
        assert store.pricing_for_scope(one.customer_id) == price
        assert store.pricing_for_scope(two.customer_id) == price
        assert access.authenticate(raw) is None
        authenticated = access.authenticate(one.session_token)
        assert authenticated is not None
        assert authenticated.customer_id == one.customer_id
        assert store.pricing_assignment(token_digest(one.session_token)) is None
    with SqliteTransactionStore(path) as store:
        assert store.pricing_for_scope(one.customer_id) == price
        assert store.pricing_for_scope(two.customer_id) == price
        assert store.pricing_assignment(token_digest(raw)) == price
        assert price.amount_cents == PRICE_ARMS[price.arm]
        for table in ("pb_price_visitors", "pb_price_credentials", "pb_price_scopes"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                store._require_open().execute(f"DELETE FROM {table}")


def test_binding_failure_rolls_back_scope_and_one_use_link(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "prices.db") as store:
        access = AccessService(store, clock=lambda: AT)
        for raw in (credential(61), "bad"):
            with pytest.raises(ValueError, match=r"credential|assignment"):
                access.start_appeal(pricing_token=raw)
            db = store._require_open()
            assert db.execute("SELECT count(*) FROM pb_identity_accounts").fetchone() == (0,)
            assert db.execute("SELECT count(*) FROM pb_identity_links").fetchone() == (0,)
            assert not db.in_transaction


def test_credential_collision_never_rebinds_and_legacy_scope_is_not_backfilled(
    tmp_path: Path,
) -> None:
    with SqliteTransactionStore(tmp_path / "prices.db") as store:
        access = AccessService(store, clock=lambda: AT)
        old = access.start_appeal()
        a = store.create_pricing_assignment(token_digest(credential(62)), now=AT)
        b = store.create_pricing_assignment(token_digest(credential(63)), now=AT)
        with pytest.raises(ValueError, match="already bound"):
            store.issue_pricing_credential(a.visitor_id, token_digest(credential(63)))
        assert store.pricing_assignment(token_digest(credential(63))) == b
        alias = token_digest(credential(64))
        assert store.issue_pricing_credential(a.visitor_id, alias) == a
        assert store.pricing_assignment(alias) == a
        assert store.pricing_for_scope(old.customer_id) is None
        assert store._require_open().execute("SELECT count(*) FROM pb_price_scopes").fetchone() == (
            0,
        )


def test_pricing_initialization_preserves_legacy_identity_rows(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with SqliteTransactionStore(path) as store:
        old = AccessService(store, clock=lambda: AT).start_appeal()
        rows = store._require_open().execute("SELECT * FROM pb_identity_accounts").fetchall()
    with sqlite3.connect(path) as db:
        for table in ("pb_price_scopes", "pb_price_credentials", "pb_price_visitors"):
            db.execute(f"DROP TABLE {table}")
        db.execute("DELETE FROM pb_store_versions WHERE component='pricing'")
    with SqliteTransactionStore(path) as store:
        assert (
            store._require_open().execute("SELECT * FROM pb_identity_accounts").fetchall() == rows
        )
        assert store.pricing_for_scope(old.customer_id) is None
        assert store._require_open().execute("SELECT * FROM pb_price_visitors").fetchall() == []


def test_pricing_schema_tamper_and_failed_credential_insert_refuse(tmp_path: Path) -> None:
    path = tmp_path / "fail.db"
    with SqliteTransactionStore(path) as store:
        db = store._require_open()
        db.execute(
            "CREATE TRIGGER fail_price BEFORE INSERT ON pb_price_credentials "
            "BEGIN SELECT RAISE(ABORT, 'synthetic-failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="synthetic-failure"):
            store.create_pricing_assignment(token_digest(credential(65)), now=AT)
        assert db.execute("SELECT * FROM pb_price_visitors").fetchall() == []
        assert not db.in_transaction
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE pb_price_visitors ADD COLUMN tampered TEXT")
    with pytest.raises(StorageCorruption, match="pricing schema"):
        SqliteTransactionStore(path)


def test_visitor_assignment_survives_private_session_expiry(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "prices.db") as store:
        clock = [AT]
        access = AccessService(store, clock=lambda: clock[0])
        price, raw = access.browser_pricing(None)
        assert raw is not None
        scope = access.start_appeal(pricing_token=raw)
        clock[0] += timedelta(days=31)
        assert access.authenticate(scope.session_token) is None
        recognized, replacement = access.browser_pricing(raw)
        assert recognized == price
        assert replacement is None
        next_scope = access.start_appeal(pricing_token=raw)
        assert next_scope.customer_id != scope.customer_id
        assert store.pricing_for_scope(next_scope.customer_id) == price


@pytest.mark.parametrize("digest", ["", "X" * 64, "0" * 63])
def test_invalid_digest_never_creates_assignment(tmp_path: Path, digest: str) -> None:
    with SqliteTransactionStore(tmp_path / "invalid.db") as store:
        with pytest.raises(ValueError, match="credential"):
            store.create_pricing_assignment(digest, now=AT)
        assert store._require_open().execute("SELECT * FROM pb_price_visitors").fetchall() == []


def test_new_scope_binding_failure_is_atomic_with_existing_assignment(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "atomic.db") as store:
        raw = credential(66)
        price = store.create_pricing_assignment(token_digest(raw), now=AT)
        db = store._require_open()
        db.execute(
            "CREATE TRIGGER fail_price_scope BEFORE INSERT ON pb_price_scopes "
            "BEGIN SELECT RAISE(ABORT, 'binding-failed'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="binding-failed"):
            AccessService(store, clock=lambda: AT).start_appeal(pricing_token=raw)
        assert db.execute("SELECT * FROM pb_identity_accounts").fetchall() == []
        assert db.execute("SELECT * FROM pb_identity_links").fetchall() == []
        assert db.execute("SELECT * FROM pb_price_scopes").fetchall() == []
        assert store.pricing_assignment(token_digest(raw)) == price
        assert not db.in_transaction
