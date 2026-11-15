"""Append-only AI journal rows on the transaction store's SQLite connection.

A journal write runs in its own transaction, never inside a letter or payment commit, so a
journal failure cannot block or undo customer processing. ``try_append_ai_journal``
reports failure instead of raising. Entries are validated category records
(oplog.ai_journal); the subject is the restricted case or intake token and never leaves
this boundary.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Protocol

from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.oplog.ai_journal import AiCall, AiJournalEntry

_CALLS_DDL = """CREATE TABLE pb_ai_calls (
    subject_token TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    entry_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (subject_token, seq)
)"""
_DDL = """CREATE TABLE pb_ai_journal (
    subject_token TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    entry_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (subject_token, seq)
)"""


class JournalBusy(RuntimeError):
    """The connection is inside someone else's transaction; the journal never joins it."""


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


def _initialize_table(database: sqlite3.Connection, component: str, table: str, ddl: str) -> None:
    version = database.execute(
        "SELECT version FROM pb_store_versions WHERE component=?", (component,)
    ).fetchone()
    row = database.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if version is None:
        if row is not None:
            raise StorageCorruption(f"unversioned {table} schema")
        database.execute(ddl)
        database.execute("INSERT INTO pb_store_versions VALUES (?, 1)", (component,))
    elif (
        version != (1,)
        or row is None
        or " ".join(row[0].upper().split()) != " ".join(ddl.upper().split())
    ):
        raise StorageCorruption(f"malformed {table} schema")
    for action in ("UPDATE", "DELETE"):
        database.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
            f"BEFORE {action} ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
        )


def initialize_ai_journal(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")
    try:
        _initialize_table(database, "ai_journal", "pb_ai_journal", _DDL)
        _initialize_table(database, "ai_calls", "pb_ai_calls", _CALLS_DDL)
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def _subject(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("subject must be a non-blank token")
    return value


def _append(database: sqlite3.Connection, table: str, subject: str, payload: str, at: str) -> int:
    if database.in_transaction:
        # Never join, commit or roll back a transaction this append did not start.
        raise JournalBusy("journal writes need their own transaction")
    database.execute("BEGIN IMMEDIATE")
    try:
        (last,) = database.execute(
            f"SELECT COALESCE(MAX(seq), 0) FROM {table} WHERE subject_token=?", (subject,)
        ).fetchone()
        database.execute(
            f"INSERT INTO {table} VALUES (?, ?, ?, ?)", (subject, last + 1, payload, at)
        )
        database.execute("COMMIT")
        return int(last) + 1
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def _aware(at: datetime) -> str:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("recorded_at must be timezone-aware")
    return at.isoformat()


class AiJournalStoreMixin:
    def append_ai_journal(
        self: _Connected, subject: str, entry: AiJournalEntry, recorded_at: datetime
    ) -> int:
        """Append one entry and return its sequence number within the subject."""
        if type(entry) is not AiJournalEntry:
            raise ValueError("entry must be an AiJournalEntry")
        return _append(
            self._require_open(),
            "pb_ai_journal",
            _subject(subject),
            entry.to_json(),
            _aware(recorded_at),
        )

    def try_append_ai_journal(
        self: _Connected, subject: str, entry: AiJournalEntry, recorded_at: datetime
    ) -> bool:
        """Best-effort append for customer paths: a journal failure is reported, not raised."""
        try:
            AiJournalStoreMixin.append_ai_journal(self, subject, entry, recorded_at)
        except (sqlite3.Error, StorageCorruption, JournalBusy):
            return False
        return True

    def ai_journal(self: _Connected, subject: str) -> tuple[AiJournalEntry, ...]:
        rows = (
            self._require_open()
            .execute(
                "SELECT entry_json FROM pb_ai_journal WHERE subject_token=? ORDER BY seq",
                (_subject(subject),),
            )
            .fetchall()
        )
        try:
            return tuple(AiJournalEntry.from_json(row[0]) for row in rows)
        except (ValueError, TypeError, KeyError) as exc:
            raise StorageCorruption(f"invalid AI journal entry: {exc}") from exc

    def try_append_ai_call(
        self: _Connected, subject: str, call: AiCall, recorded_at: datetime
    ) -> bool:
        """Best-effort AI call log append; failure is reported, never raised into an appeal."""
        if type(call) is not AiCall:
            raise ValueError("call must be an AiCall")
        try:
            _append(
                self._require_open(),
                "pb_ai_calls",
                _subject(subject),
                call.to_json(),
                _aware(recorded_at),
            )
        except (sqlite3.Error, StorageCorruption, JournalBusy):
            return False
        return True

    def ai_calls(self: _Connected, subject: str) -> tuple[AiCall, ...]:
        rows = (
            self._require_open()
            .execute(
                "SELECT entry_json FROM pb_ai_calls WHERE subject_token=? ORDER BY seq",
                (_subject(subject),),
            )
            .fetchall()
        )
        try:
            return tuple(AiCall.from_json(row[0]) for row in rows)
        except (ValueError, TypeError, KeyError) as exc:
            raise StorageCorruption(f"invalid AI call entry: {exc}") from exc
