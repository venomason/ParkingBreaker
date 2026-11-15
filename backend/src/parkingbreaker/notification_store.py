"""Atomic persistence for request-scoped sign-in-link notification envelopes."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from parkingbreaker.customer_access import token_digest as validate_nonce
from parkingbreaker.identity_store import (
    IdentityConflict,
    _account,
    _canonical_email,
    _customer_id,
    _digest,
    _insert_identity_link_rows,
    _link,
    _stamp,
    _stored_time,
    _utc,
)
from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error

_NOTIFICATION_DDL = """CREATE TABLE pb_notification_access_links (
    request_id TEXT PRIMARY KEY,
    effect_id TEXT NOT NULL UNIQUE REFERENCES pb_pending_effects(effect_id),
    customer_id TEXT NOT NULL REFERENCES pb_identity_accounts(customer_id),
    nonce TEXT NOT NULL UNIQUE,
    token_digest TEXT NOT NULL UNIQUE REFERENCES pb_identity_links(token_digest),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    CHECK (expires_at > issued_at)
)"""
_MAX_LINK_LIFETIME = timedelta(minutes=15)


class NotificationRateLimited(ValueError):
    """Fresh email requests exceeded the durable recipient budget."""


class NotificationConflict(ValueError):
    """A refused notification write without request or customer information."""


@dataclass(frozen=True, repr=False)
class AccessLinkNotification:
    request_id: str
    effect_id: str
    customer_id: str
    email: str
    nonce: str
    token_digest: str
    issued_at: datetime
    expires_at: datetime

    def __repr__(self) -> str:
        return "AccessLinkNotification(<redacted>)"


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


def initialize_notification_access_links(database: sqlite3.Connection) -> None:
    """Create or strictly validate the notification envelope schema version 1."""
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='notifications'"
        ).fetchone()
        tables = {
            row[0]: row[1]
            for row in database.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
            if row[0].startswith("pb_notification_")
        }
        if version is None:
            if tables:
                raise StorageCorruption("unversioned notification schema")
            database.execute(_NOTIFICATION_DDL)
            database.execute("INSERT INTO pb_store_versions VALUES ('notifications', 1)")
        elif type(version[0]) is not int or version[0] != 1:
            raise StorageCorruption("unsupported notification schema")
        else:
            expected = {"pb_notification_access_links": " ".join(_NOTIFICATION_DDL.upper().split())}
            actual = {name: " ".join(sql.upper().split()) for name, sql in tables.items()}
            if actual != expected:
                raise StorageCorruption("malformed notification schema")
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


class NotificationStoreMixin:
    """Persist notification intent and its one-use link on the transaction connection."""

    def queue_access_link(
        self: _Connected,
        request_id: str,
        effect_id: str,
        customer_id: str,
        email: str,
        nonce: str,
        token_digest: str,
        issued_at: datetime,
        expires_at: datetime,
        *,
        bound_scope: str | None = None,
    ) -> AccessLinkNotification:
        request_id = _uuid4(request_id)
        email = _canonicalize_email(email)
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            previous = _read_notification(database, request_id)
            if previous is not None:
                if previous.email != email or (
                    bound_scope is not None and previous.customer_id != bound_scope
                ):
                    raise NotificationConflict("notification conflict")
                database.execute("COMMIT")
                return previous

            effect_id = _uuid4(effect_id)
            customer_id = _identity_input(lambda: _customer_id(customer_id))
            if bound_scope is not None and bound_scope != customer_id:
                raise NotificationConflict("notification conflict")
            token_digest = _identity_input(lambda: _digest(token_digest))
            nonce = _validate_nonce(nonce)
            issued_at = _identity_input(lambda: _utc(issued_at))
            expires_at = _identity_input(lambda: _utc(expires_at))
            if expires_at <= issued_at or expires_at - issued_at > _MAX_LINK_LIFETIME:
                raise NotificationConflict("notification conflict")

            # Count only new intents. Lost-response retries return above and never
            # consume another slot; the transaction serializes concurrent callers.
            recent = database.execute(
                "SELECT count(*) FROM pb_notification_access_links n "
                "JOIN pb_identity_accounts a ON a.customer_id=n.customer_id "
                "WHERE a.email=? AND n.issued_at>?",
                (email, _stamp(issued_at - _MAX_LINK_LIFETIME)),
            ).fetchone()[0]
            if recent >= 5:
                raise NotificationRateLimited("request rate limited")

            account = _insert_identity_link_rows(
                database,
                customer_id=customer_id,
                email=email,
                token_digest=token_digest,
                issued_at=issued_at,
                expires_at=expires_at,
            )
            database.execute(
                "INSERT INTO pb_pending_effects "
                "(effect_id, machine, effect, created_at, request_token) "
                "VALUES (?, 'notification', 'SEND_SIGN_IN_LINK', ?, ?)",
                (effect_id, _stamp(issued_at), request_id),
            )
            database.execute(
                "INSERT INTO pb_notification_access_links "
                "(request_id, effect_id, customer_id, nonce, token_digest, issued_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    effect_id,
                    account.customer_id,
                    nonce,
                    token_digest,
                    _stamp(issued_at),
                    _stamp(expires_at),
                ),
            )
            result = _read_notification(database, request_id)
            if result is None:
                raise StorageCorruption("notification row was not persisted")
            database.execute("COMMIT")
            return result
        except sqlite3.IntegrityError:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise NotificationConflict("notification conflict") from None
        except IdentityConflict:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise NotificationConflict("notification conflict") from None
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def access_notification(self: _Connected, request_id: str) -> AccessLinkNotification | None:
        request_id = _uuid4(request_id)
        try:
            return _read_notification(self._require_open(), request_id)
        except StorageCorruption:
            raise
        except sqlite3.Error:
            raise StorageCorruption("notification storage could not be read") from None


def _read_notification(
    database: sqlite3.Connection, request_id: str
) -> AccessLinkNotification | None:
    row = database.execute(
        "SELECT n.request_id, n.effect_id, n.customer_id, n.nonce, n.token_digest, "
        "n.issued_at, n.expires_at, a.customer_id, a.email, a.created_at, a.verified_at, "
        "l.token_digest, l.customer_id, l.issued_at, l.expires_at, l.consumed_at, "
        "e.effect_id, e.request_token, e.machine, e.effect, e.case_token, e.revision, "
        "e.counter_json FROM pb_notification_access_links n "
        "LEFT JOIN pb_identity_accounts a ON a.customer_id=n.customer_id "
        "LEFT JOIN pb_identity_links l ON l.token_digest=n.token_digest "
        "LEFT JOIN pb_pending_effects e ON e.effect_id=n.effect_id "
        "WHERE n.request_id=?",
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        (
            stored_request,
            effect_id,
            customer_id,
            nonce,
            stored_digest,
            issued_text,
            expires_text,
            account_id,
            email,
            account_created,
            account_verified,
            link_digest,
            link_customer_id,
            link_issued_text,
            link_expires_text,
            _consumed_text,
            outbox_effect_id,
            request_token,
            machine,
            effect,
            case_token,
            revision,
            counter_json,
        ) = row
        if (
            stored_request != request_id
            or not isinstance(effect_id, str)
            or _uuid4(effect_id) != effect_id
            or not isinstance(customer_id, str)
            or not isinstance(nonce, str)
            or not isinstance(stored_digest, str)
        ):
            raise ValueError
        _identity_input(lambda: _customer_id(customer_id))
        _identity_input(lambda: _digest(stored_digest))
        if _validate_nonce(nonce) != nonce:
            # _validate_nonce returns the identical canonical nonce string.
            raise ValueError
        if not isinstance(email, str):
            raise ValueError
        canonical_email = _canonical_email(email)
        account = _account((account_id, email, account_created, account_verified))
        link = _link(
            (link_digest, link_customer_id, link_issued_text, link_expires_text, _consumed_text)
        )
        issued_at = _strict_time(issued_text)
        expires_at = _strict_time(expires_text)
        if (
            canonical_email != account.email
            or account.customer_id != customer_id
            or link[0] != stored_digest
            or link[1] != customer_id
            or link[2] != issued_at
            or link[3] != expires_at
            or issued_at < account.created_at
            or outbox_effect_id != effect_id
            or request_token != request_id
            or machine != "notification"
            or effect != "SEND_SIGN_IN_LINK"
            or case_token is not None
            or revision is not None
            or counter_json is not None
            or expires_at <= issued_at
            or expires_at - issued_at > _MAX_LINK_LIFETIME
        ):
            raise ValueError
        return AccessLinkNotification(
            request_id,
            effect_id,
            customer_id,
            account.email,
            nonce,
            stored_digest,
            issued_at,
            expires_at,
        )
    except (
        IdentityConflict,
        NotificationConflict,
        StorageCorruption,
        TypeError,
        ValueError,
        OverflowError,
        AssertionError,
    ):
        raise StorageCorruption("malformed notification record") from None


def _strict_time(value: object) -> datetime:
    parsed = _stored_time(value)
    if parsed is None:
        raise StorageCorruption("malformed notification record")
    return parsed


def _uuid4(value: str) -> str:
    if not isinstance(value, str):
        raise NotificationConflict("notification conflict")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise NotificationConflict("notification conflict") from None
    if parsed.version != 4 or str(parsed) != value:
        raise NotificationConflict("notification conflict")
    return value


def _canonicalize_email(value: str) -> str:
    try:
        return _canonical_email(value)
    except IdentityConflict:
        raise NotificationConflict("notification conflict") from None


def _identity_input[T](validate: Callable[[], T]) -> T:
    try:
        return validate()
    except IdentityConflict:
        raise NotificationConflict("notification conflict") from None


def _validate_nonce(value: str) -> str:
    try:
        validate_nonce(value)
    except (TypeError, ValueError):
        raise NotificationConflict("notification conflict") from None
    return value
