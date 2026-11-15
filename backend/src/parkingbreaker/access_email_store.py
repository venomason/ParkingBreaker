"""Email-call intents beneath the existing notification outbox, on its SQLite connection.

An unfinished call is truthful uncertainty. Recovery appends a call using the same
outbox-derived provider key and immutable message digest; it never invents a result.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from parkingbreaker.email_client import EmailOutcome, EmailReason
from parkingbreaker.identity_store import _digest, _stamp, _utc
from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.notification_store import (
    AccessLinkNotification,
    NotificationConflict,
    _read_notification,
    _strict_time,
    _uuid4,
)

_DDL = """CREATE TABLE pb_access_email_calls (
    operation_id TEXT PRIMARY KEY,
    effect_id TEXT NOT NULL REFERENCES pb_pending_effects(effect_id),
    attempt INTEGER NOT NULL CHECK (attempt BETWEEN 1 AND 8),
    message_sha256 TEXT NOT NULL,
    started_at TEXT NOT NULL,
    next_at TEXT NOT NULL,
    outcome_json TEXT,
    completed_at TEXT,
    UNIQUE (effect_id, attempt),
    CHECK ((outcome_json IS NULL) = (completed_at IS NULL))
)"""
MAX_ATTEMPTS = 8


@dataclass(frozen=True, repr=False)
class AccessEmailCall:
    operation_id: str
    notification: AccessLinkNotification
    attempt: int
    message_sha256: str
    started_at: datetime
    next_at: datetime

    def __repr__(self) -> str:
        return "AccessEmailCall(<redacted>)"


class _Connected(Protocol):
    _submission_locked: bool

    def _require_open(self) -> sqlite3.Connection: ...


def initialize_access_email(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='access_email'"
        ).fetchone()
        tables = {
            row[0]: row[1]
            for row in database.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
            if row[0].startswith("pb_access_email_")
        }
        if version is None:
            if tables:
                raise StorageCorruption("unversioned access email schema")
            database.execute(_DDL)
            database.execute("INSERT INTO pb_store_versions VALUES ('access_email', 1)")
        elif version != (1,) or tables != {"pb_access_email_calls": _DDL}:
            # Whitespace is normalized because SQLite may preserve formatting differently.
            if version != (1,) or {
                name: " ".join(sql.upper().split()) for name, sql in tables.items()
            } != {"pb_access_email_calls": " ".join(_DDL.upper().split())}:
                raise StorageCorruption("malformed access email schema")
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


class AccessEmailStoreMixin:
    def due_access_emails(self: _Connected, now: datetime, *, limit: int = 10) -> tuple[str, ...]:
        now = _utc(now)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid access email batch size")
        rows = (
            self._require_open()
            .execute(
                "SELECT e.request_token FROM pb_pending_effects e "
                "LEFT JOIN pb_access_email_calls c ON c.effect_id=e.effect_id "
                "AND c.attempt=(SELECT MAX(attempt) FROM pb_access_email_calls "
                "WHERE effect_id=e.effect_id) "
                "WHERE e.machine='notification' AND e.effect='SEND_SIGN_IN_LINK' "
                "AND e.completed_at IS NULL AND (c.next_at IS NULL OR c.next_at<=?) "
                "ORDER BY COALESCE(c.next_at,e.created_at), e.effect_id LIMIT ?",
                (_stamp(now), limit),
            )
            .fetchall()
        )
        return tuple(_uuid4(row[0]) for row in rows)

    def claim_access_email(
        self: _Connected,
        request_id: str,
        *,
        operation_id: str,
        message_sha256: str,
        now: datetime,
        configuration_valid: bool = True,
    ) -> AccessEmailCall | None:
        if not self._submission_locked:
            raise NotificationConflict("notification ownership required")
        request_id, operation_id = _uuid4(request_id), _uuid4(operation_id)
        message_sha256, now = _digest(message_sha256), _utc(now)
        database = self._require_open()
        database.execute("BEGIN IMMEDIATE")
        try:
            note = _read_notification(database, request_id)
            if note is None:
                raise NotificationConflict("notification not found")
            outbox = database.execute(
                "SELECT claimed_at, outcome, completed_at FROM pb_pending_effects "
                "WHERE effect_id=?",
                (note.effect_id,),
            ).fetchone()
            if now < note.issued_at:
                raise NotificationConflict("notification clock moved backwards")
            if outbox[2] is not None:
                database.execute("COMMIT")
                return None
            calls = _calls(database, note)
            last_completion = database.execute(
                "SELECT completed_at FROM pb_access_email_calls WHERE effect_id=? "
                "ORDER BY attempt DESC LIMIT 1",
                (note.effect_id,),
            ).fetchone()
            if calls and now < max(
                calls[-1][0].started_at,
                (
                    calls[-1][0].started_at
                    if last_completion[0] is None
                    else _strict_time(last_completion[0])
                ),
            ):
                raise NotificationConflict("notification clock moved backwards")
            if calls and now < calls[-1][0].next_at:
                database.execute("COMMIT")
                return None
            consumed = database.execute(
                "SELECT consumed_at FROM pb_identity_links WHERE token_digest=?",
                (note.token_digest,),
            ).fetchone()[0]
            stop = (
                "expired"
                if now >= note.expires_at
                else "consumed"
                if consumed is not None
                else "configuration_failed"
                if not configuration_valid
                or (calls and calls[-1][0].message_sha256 != message_sha256)
                else "exhausted"
                if len(calls) >= MAX_ATTEMPTS
                else None
            )
            if stop is not None:
                _complete(database, note, stop, now)
                database.execute("COMMIT")
                return None
            attempt = len(calls) + 1
            next_at = now + timedelta(seconds=min(30 * 2 ** (attempt - 1), 240))
            call = AccessEmailCall(operation_id, note, attempt, message_sha256, now, next_at)
            database.execute(
                "INSERT INTO pb_access_email_calls VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
                (
                    operation_id,
                    note.effect_id,
                    attempt,
                    message_sha256,
                    _stamp(now),
                    _stamp(next_at),
                ),
            )
            database.execute(
                "UPDATE pb_pending_effects SET claimed_at=COALESCE(claimed_at, ?) "
                "WHERE effect_id=?",
                (_stamp(now), note.effect_id),
            )
            database.execute("COMMIT")
            return call
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def finish_access_email(
        self: _Connected, call: AccessEmailCall, outcome: EmailOutcome, *, now: datetime
    ) -> None:
        if not self._submission_locked:
            raise NotificationConflict("notification ownership required")
        if not isinstance(outcome, EmailOutcome):
            raise ValueError("invalid email outcome")
        now = _utc(now)
        database = self._require_open()
        database.execute("BEGIN IMMEDIATE")
        try:
            note = _read_notification(database, call.notification.request_id)
            if note != call.notification:
                raise StorageCorruption("notification identity changed")
            calls = _calls(database, note)
            outbox = database.execute(
                "SELECT completed_at FROM pb_pending_effects WHERE effect_id=?",
                (note.effect_id,),
            ).fetchone()
            if (
                not calls
                or calls[-1][0] != call
                or calls[-1][1] is not None
                or outbox[0] is not None
            ):
                raise NotificationConflict("notification call is stale")
            if now < call.started_at:
                raise NotificationConflict("notification clock moved backwards")
            database.execute(
                "UPDATE pb_access_email_calls SET outcome_json=?, completed_at=? "
                "WHERE operation_id=?",
                (_outcome_json(outcome), _stamp(now), call.operation_id),
            )
            if outcome.status != "uncertain":
                _complete(database, note, outcome.status, now)
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise


def _complete(
    database: sqlite3.Connection, note: AccessLinkNotification, outcome: str, now: datetime
) -> None:
    database.execute(
        "UPDATE pb_pending_effects SET claimed_at=COALESCE(claimed_at, ?), "
        "outcome=?, completed_at=? "
        "WHERE effect_id=? AND completed_at IS NULL",
        (_stamp(now), outcome, _stamp(now), note.effect_id),
    )


def _outcome_json(outcome: EmailOutcome) -> str:
    return json.dumps(
        {
            "status": outcome.status,
            "provider_id": outcome.provider_id,
            "reason": None if outcome.reason is None else outcome.reason.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _calls(
    database: sqlite3.Connection, note: AccessLinkNotification
) -> list[tuple[AccessEmailCall, EmailOutcome | None]]:
    rows = database.execute(
        "SELECT operation_id, attempt, message_sha256, started_at, next_at, "
        "outcome_json, completed_at "
        "FROM pb_access_email_calls WHERE effect_id=? ORDER BY attempt",
        (note.effect_id,),
    ).fetchall()
    result: list[tuple[AccessEmailCall, EmailOutcome | None]] = []
    previous = note.issued_at
    try:
        if len(rows) > MAX_ATTEMPTS:
            raise ValueError
        for index, row in enumerate(rows, 1):
            operation, attempt, digest, started, next_at, payload, completed = row
            started, next_at = _strict_time(started), _strict_time(next_at)
            if (
                type(attempt) is not int
                or attempt != index
                or started < previous
                or next_at <= started
                or (payload is None) != (completed is None)
            ):
                raise ValueError
            call = AccessEmailCall(
                _uuid4(operation), note, attempt, _digest(digest), started, next_at
            )
            outcome = None
            if payload is not None:
                data = json.loads(payload)
                if not isinstance(data, dict) or set(data) != {"status", "provider_id", "reason"}:
                    raise ValueError
                outcome = EmailOutcome(
                    data["status"],
                    data["provider_id"],
                    None if data["reason"] is None else EmailReason(data["reason"]),
                )
                if _strict_time(completed) < started or _outcome_json(outcome) != payload:
                    raise ValueError
            if result and (
                digest != result[0][0].message_sha256
                or (result[-1][1] is not None and result[-1][1].status != "uncertain")
            ):
                raise ValueError
            result.append((call, outcome))
            previous = started if completed is None else _strict_time(completed)
        return result
    except (ValueError, TypeError, KeyError):
        raise StorageCorruption("malformed access email history") from None
