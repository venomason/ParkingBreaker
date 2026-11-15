"""T18 coverage: a failing ROLLBACK must not mask the original error in the access stores."""

from __future__ import annotations

import base64
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from parkingbreaker.identity_store import IdentityConflict
from parkingbreaker.notification_store import NotificationConflict
from parkingbreaker.transaction_store import SqliteTransactionStore

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
DIGEST = "a" * 64


class _FailingRollback:
    def __init__(self, inner: sqlite3.Connection) -> None:
        self.inner = inner

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if sql.strip().upper() == "ROLLBACK":
            raise sqlite3.OperationalError("injected rollback failure")
        return self.inner.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _inject(store: SqliteTransactionStore) -> sqlite3.Connection:
    inner = store._require_open()
    store._database = _FailingRollback(inner)  # type: ignore[assignment]
    return inner


def _restore(store: SqliteTransactionStore, inner: sqlite3.Connection) -> None:
    store._database = inner
    if inner.in_transaction:
        inner.execute("ROLLBACK")


def test_identity_rollback_failure_does_not_mask_original_error(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        store.issue_appeal_start(
            scope_id="scope-1",
            link_digest=DIGEST,
            now=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
        inner = _inject(store)
        try:
            with pytest.raises(sqlite3.IntegrityError) as raised:
                store.issue_appeal_start(
                    scope_id="scope-1",
                    link_digest="b" * 64,
                    now=NOW,
                    expires_at=NOW + timedelta(hours=1),
                )
            assert any("ROLLBACK also failed" in n for n in raised.value.__notes__)
        finally:
            _restore(store, inner)


def test_identity_integrity_rollback_failure_still_maps_to_conflict(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        store.issue_identity_link(
            customer_id="customer-1",
            email="a@example.com",
            token_digest=DIGEST,
            issued_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
        inner = _inject(store)
        try:
            with pytest.raises(IdentityConflict):
                store.issue_identity_link(
                    customer_id="customer-2",
                    email="b@example.com",
                    token_digest=DIGEST,
                    issued_at=NOW,
                    expires_at=NOW + timedelta(hours=1),
                )
        finally:
            _restore(store, inner)


def test_notification_rollback_failure_does_not_mask_original_error(tmp_path: Path) -> None:
    request_id = str(uuid.uuid4())
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        args: dict[str, Any] = {
            "request_id": request_id,
            "effect_id": str(uuid.uuid4()),
            "customer_id": "customer-1",
            "nonce": base64.urlsafe_b64encode(b"\x03" * 32).decode("ascii").rstrip("="),
            "token_digest": DIGEST,
            "issued_at": NOW,
            "expires_at": NOW + timedelta(minutes=15),
        }
        store.queue_access_link(email="a@example.com", **args)
        inner = _inject(store)
        try:
            with pytest.raises(NotificationConflict) as raised:
                store.queue_access_link(email="other@example.com", **args)
            assert any("ROLLBACK also failed" in n for n in raised.value.__notes__)
        finally:
            _restore(store, inner)


def test_access_email_rollback_failure_does_not_mask_original_error(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        store._submission_locked = True
        inner = _inject(store)
        try:
            with pytest.raises(NotificationConflict, match="notification not found") as raised:
                store.claim_access_email(
                    str(uuid.uuid4()),
                    operation_id=str(uuid.uuid4()),
                    message_sha256=DIGEST,
                    now=NOW,
                )
            assert any("ROLLBACK also failed" in n for n in raised.value.__notes__)
        finally:
            _restore(store, inner)
