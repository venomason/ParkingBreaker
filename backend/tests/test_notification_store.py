from __future__ import annotations

import base64
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parkingbreaker.customer_access import token_digest
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.notification_store import AccessLinkNotification, NotificationConflict
from parkingbreaker.transaction_store import SqliteTransactionStore

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def _uuid(number: int) -> str:
    return str(uuid.UUID(int=number, version=4))


def _nonce(number: int) -> str:
    return base64.urlsafe_b64encode(bytes([number]) * 32).decode("ascii").rstrip("=")


def _link_digest(number: int) -> tuple[str, str]:
    raw = _nonce(number + 80)
    return raw, token_digest(raw)


def _queue(
    store: SqliteTransactionStore,
    *,
    request: int = 1,
    effect: int = 2,
    customer: str = "customer-a",
    email: str = "Alice@EXAMPLE.COM",
    nonce: int = 3,
    digest_number: int = 4,
    issued: datetime = NOW,
    expires: datetime = NOW + timedelta(minutes=15),
) -> AccessLinkNotification:
    _, digest = _link_digest(digest_number)
    return store.queue_access_link(
        _uuid(request),
        _uuid(effect),
        customer,
        email,
        _nonce(nonce),
        digest,
        issued,
        expires,
    )


def _counts(store: SqliteTransactionStore) -> tuple[int, int, int, int]:
    database = store._require_open()
    return tuple(
        database.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "pb_identity_accounts",
            "pb_identity_links",
            "pb_pending_effects",
            "pb_notification_access_links",
        )
    )


def test_queue_is_atomic_persists_only_envelope_and_reopens(tmp_path: Path) -> None:
    path = tmp_path / "notification.sqlite"
    raw_link, digest = _link_digest(4)
    nonce = _nonce(3)
    with SqliteTransactionStore(path) as store:
        result = store.queue_access_link(
            _uuid(1),
            _uuid(2),
            "customer-a",
            "Alice@EXAMPLE.COM",
            nonce,
            digest,
            NOW,
            NOW + timedelta(minutes=15),
        )
        assert result.email == "Alice@example.com"
        assert result.issued_at == NOW
        assert result.expires_at == NOW + timedelta(minutes=15)
        assert "Alice@example.com" not in repr(result)
        assert nonce not in repr(result)
        assert digest not in repr(result)
        assert raw_link not in repr(result)
        assert _counts(store) == (1, 1, 1, 1)
        database = store._require_open()
        assert database.execute(
            "SELECT version FROM pb_store_versions WHERE component='notifications'"
        ).fetchone() == (1,)
        assert database.execute(
            "SELECT request_id, effect_id, customer_id, nonce, token_digest, issued_at, expires_at "
            "FROM pb_notification_access_links"
        ).fetchone() == (
            _uuid(1),
            _uuid(2),
            "customer-a",
            nonce,
            digest,
            NOW.isoformat(timespec="microseconds"),
            (NOW + timedelta(minutes=15)).isoformat(timespec="microseconds"),
        )
        assert database.execute(
            "SELECT machine, effect, request_token, case_token, revision, counter_json "
            "FROM pb_pending_effects"
        ).fetchone() == ("notification", "SEND_SIGN_IN_LINK", _uuid(1), None, None, None)
        persisted_text = " ".join(
            str(value)
            for table in ("pb_identity_accounts", "pb_identity_links", "pb_pending_effects")
            for row in database.execute(f"SELECT * FROM {table}").fetchall()
            for value in row
        )
        assert raw_link not in persisted_text
        assert "Alice@example.com" not in " ".join(
            str(value)
            for row in database.execute("SELECT * FROM pb_notification_access_links").fetchall()
            for value in row
        )
        assert database.execute("SELECT count(*) FROM pb_cases").fetchone() == (0,)
        assert database.execute("SELECT count(*) FROM pb_workspaces").fetchone() == (0,)
        assert database.execute("SELECT count(*) FROM pb_intake_records").fetchone() == (0,)
    with SqliteTransactionStore(path) as reopened:
        assert reopened.access_notification(_uuid(1)) == result


def test_same_request_reuses_original_envelope_and_ignores_new_entropy(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "notification.sqlite") as store:
        first = _queue(store)
        repeated = _queue(
            store,
            request=1,
            effect=22,
            customer="different-customer",
            email="Alice@example.com",
            nonce=23,
            digest_number=24,
            issued=NOW + timedelta(days=1),
            expires=NOW + timedelta(days=1, minutes=10),
        )
        assert repeated == first
        assert _counts(store) == (1, 1, 1, 1)


def test_same_request_with_different_canonical_email_conflicts_without_leak(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "notification.sqlite") as store:
        first = _queue(store)
        with pytest.raises(NotificationConflict, match=r"^notification conflict$") as error:
            _queue(store, request=1, email="other@example.com")
        assert "other@example.com" not in str(error.value)
        assert _counts(store) == (1, 1, 1, 1)
        assert store.access_notification(_uuid(1)) == first


def test_distinct_appeals_with_same_email_have_distinct_scopes(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "notification.sqlite") as store:
        first = _queue(store)
        second = _queue(
            store,
            request=11,
            effect=12,
            customer="new-candidate-customer",
            email="Alice@example.com",
            nonce=13,
            digest_number=14,
        )
        assert first.customer_id == "customer-a"
        assert second.customer_id != first.customer_id
        assert second.email == first.email
        assert _counts(store) == (2, 2, 2, 2)


@pytest.mark.parametrize(
    ("issued", "expires"),
    [
        (NOW, NOW),
        (NOW, NOW + timedelta(minutes=15, microseconds=1)),
        (datetime(2026, 10, 17, 12), NOW + timedelta(minutes=1)),
    ],
)
def test_expiry_bounds_and_aware_time_validation(
    tmp_path: Path, issued: datetime, expires: datetime
) -> None:
    with SqliteTransactionStore(tmp_path / "notification.sqlite") as store:
        with pytest.raises(NotificationConflict, match=r"^notification conflict$"):
            _queue(store, issued=issued, expires=expires)
        assert _counts(store) == (0, 0, 0, 0)


@pytest.mark.parametrize("collision", ["nonce", "digest", "effect"])
def test_unique_collisions_roll_back_account_link_outbox_and_envelope(
    tmp_path: Path, collision: str
) -> None:
    with SqliteTransactionStore(tmp_path / "notification.sqlite") as store:
        original = _queue(store)
        before = _counts(store)
        with pytest.raises(NotificationConflict, match=r"^notification conflict$"):
            _queue(
                store,
                request=31,
                effect=2 if collision == "effect" else 32,
                customer="second-customer",
                email="second@example.com",
                nonce=3 if collision == "nonce" else 33,
                digest_number=4 if collision == "digest" else 34,
            )
        assert _counts(store) == before
        assert store.access_notification(_uuid(1)) == original
        assert store.access_notification(_uuid(31)) is None


@pytest.mark.parametrize("table", ["pb_pending_effects", "pb_notification_access_links"])
def test_insert_failure_rolls_back_all_issuance_rows(tmp_path: Path, table: str) -> None:
    with SqliteTransactionStore(tmp_path / "notification.sqlite") as store:
        store._require_open().execute(
            f"CREATE TRIGGER reject_notification BEFORE INSERT ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
        with pytest.raises(NotificationConflict, match=r"^notification conflict$") as error:
            _queue(store)
        assert _counts(store) == (0, 0, 0, 0)
        assert "synthetic failure" not in str(error.value)


def test_getter_refuses_cross_bound_record_drift_without_pii(tmp_path: Path) -> None:
    path = tmp_path / "notification.sqlite"
    with SqliteTransactionStore(path) as store:
        result = _queue(store)
    with sqlite3.connect(path) as database:
        database.execute("PRAGMA foreign_keys=OFF")
        database.execute(
            "UPDATE pb_pending_effects SET request_token=? WHERE effect_id=?",
            (_uuid(99), result.effect_id),
        )
    with SqliteTransactionStore(path) as store:
        with pytest.raises(StorageCorruption, match=r"^malformed notification record$") as error:
            store.access_notification(result.request_id)
        assert "Alice@example.com" not in str(error.value)


def test_notification_schema_drift_and_unknown_version_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "notification.sqlite"
    with SqliteTransactionStore(path):
        pass
    with sqlite3.connect(path) as database:
        database.execute("ALTER TABLE pb_notification_access_links ADD COLUMN unexpected TEXT")
    with pytest.raises(StorageCorruption, match=r"^malformed notification schema$"):
        SqliteTransactionStore(path)

    unknown = tmp_path / "unknown.sqlite"
    with SqliteTransactionStore(unknown):
        pass
    with sqlite3.connect(unknown) as database:
        database.execute("UPDATE pb_store_versions SET version=9 WHERE component='notifications'")
    with pytest.raises(StorageCorruption, match=r"^unsupported notification schema$"):
        SqliteTransactionStore(unknown)


def test_explicit_scope_is_checked_inside_atomic_retry(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "scope.sqlite") as store:
        original = _queue(store)
        with pytest.raises(NotificationConflict):
            store.queue_access_link(
                original.request_id,
                _uuid(22),
                "appeal-b",
                original.email,
                _nonce(23),
                _link_digest(24)[1],
                NOW,
                NOW + timedelta(minutes=15),
                bound_scope="appeal-b",
            )
        assert store.access_notification(original.request_id) == original
        assert _counts(store) == (1, 1, 1, 1)


def test_fresh_email_requests_are_rate_limited_but_retries_keep_identity(tmp_path: Path) -> None:
    from parkingbreaker.notification_store import NotificationRateLimited

    path = tmp_path / "rate.sqlite"
    with SqliteTransactionStore(path) as store:
        first = _queue(store)
        for number in range(2, 6):
            _queue(
                store,
                request=number,
                effect=number + 20,
                customer=f"appeal-{number}",
                nonce=number + 30,
                digest_number=number + 40,
            )
        with pytest.raises(NotificationRateLimited, match="request rate limited"):
            _queue(store, request=6, effect=26, customer="appeal-6", nonce=36, digest_number=46)
        assert _queue(store) == first
        assert _counts(store) == (5, 5, 5, 5)
    with SqliteTransactionStore(path) as reopened:
        with pytest.raises(NotificationRateLimited):
            _queue(reopened, request=6, effect=26, customer="appeal-6", nonce=36, digest_number=46)
        after = NOW + timedelta(minutes=15)
        assert _queue(
            reopened,
            request=6,
            effect=26,
            customer="appeal-6",
            nonce=36,
            digest_number=46,
            issued=after,
            expires=after + timedelta(minutes=15),
        )
