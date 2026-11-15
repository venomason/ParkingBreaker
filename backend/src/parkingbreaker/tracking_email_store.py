"""Tracking-email outbox, on the transaction connection (T46, K14).

Each letter Lob confirms gets at most one tracking email. Confirmations are read from the
append-only case history past a stored high-water mark, so no case is ever rescanned and
the money and mail paths are unchanged. A letter confirmed without a tracking number waits
up to a week for one to arrive in the authenticated Lob evidence before a number-less
email goes out. The content is frozen at the first send attempt, which is recorded before
the provider is called; every retry reuses the same idempotency key, and no attempt is
made once the provider's 24-hour idempotency window could have lapsed (Resend keeps keys
for 24 hours: https://resend.com/docs/dashboard/emails/idempotency-keys, checked
2026-10-31). An unconfirmed send is never reported as sent.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from parkingbreaker.email_client import EmailOutcome
from parkingbreaker.identity_store import _stamp, _stored_time, _utc
from parkingbreaker.lob_submission import Accepted
from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.mail_config import MailClass

_DDL = (
    """CREATE TABLE pb_tracking_email_mark (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        history_rowid INTEGER NOT NULL CHECK (history_rowid >= 0)
    )""",
    """CREATE TABLE pb_tracking_emails (
        case_token TEXT NOT NULL REFERENCES pb_cases(case_token),
        letter_id TEXT NOT NULL,
        email TEXT NOT NULL,
        certified INTEGER NOT NULL CHECK (certified IN (0, 1)),
        tracking_number TEXT,
        confirmed_at TEXT NOT NULL,
        first_try_at TEXT,
        tries INTEGER NOT NULL CHECK (tries BETWEEN 0 AND 8),
        in_flight INTEGER NOT NULL CHECK (in_flight IN (0, 1)),
        next_at TEXT NOT NULL,
        outcome TEXT CHECK (outcome IN ('accepted', 'rejected', 'exhausted', 'no_contact')),
        provider_id TEXT,
        completed_at TEXT,
        PRIMARY KEY (case_token, letter_id),
        CHECK ((outcome IS NULL) = (completed_at IS NULL))
    )""",
)
MAX_TRIES = 8
# Resend keeps idempotency keys for 24 hours; stop an hour early.
SAFE_RETRY_WINDOW = timedelta(hours=23)
TRACKING_NUMBER_WAIT = timedelta(days=7)
_SCAN_LIMIT = 200
LOG = logging.getLogger(__name__)


@dataclass(frozen=True, repr=False)
class TrackingEmail:
    case_token: str
    letter_id: str
    email: str
    certified: bool
    tracking_number: str | None
    confirmed_at: datetime
    first_try_at: datetime | None
    tries: int
    in_flight: bool

    def __repr__(self) -> str:
        return "TrackingEmail(<redacted>)"


class _Connected(Protocol):
    _submission_locked: bool

    def _require_open(self) -> sqlite3.Connection: ...

    def submission_cursor(self, case_id: str, attempt: int) -> Any: ...

    def get(self, case_id: str, attempt: int) -> Any: ...

    def _iter_tracking_payloads(self, case_id: str) -> Any: ...


def initialize_tracking_email(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='tracking_email'"
        ).fetchone()
        tables = {
            row[0]: " ".join(row[1].upper().split())
            for row in database.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
            if row[0].startswith("pb_tracking_email")
        }
        expected = {
            name: " ".join(sql.upper().split())
            for name, sql in zip(
                ("pb_tracking_email_mark", "pb_tracking_emails"), _DDL, strict=True
            )
        }
        if version is None:
            if tables:
                raise StorageCorruption("unversioned tracking email schema")
            for ddl in _DDL:
                database.execute(ddl)
            # Start after existing history: letters confirmed before this table existed
            # were never promised an email and must not get one late.
            last = database.execute("SELECT COALESCE(MAX(rowid), 0) FROM pb_case_history")
            database.execute(
                "INSERT INTO pb_tracking_email_mark VALUES (1, ?)", (last.fetchone()[0],)
            )
            database.execute("INSERT INTO pb_store_versions VALUES ('tracking_email', 1)")
        elif version != (1,) or tables != expected:
            raise StorageCorruption("malformed tracking email schema")
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def _contact(database: sqlite3.Connection, case_token: str) -> str | None:
    row = database.execute(
        "SELECT a.email, r.intake_json FROM pb_intake_records r "
        "JOIN pb_intake_owners o ON o.workspace_token=r.workspace_token "
        "LEFT JOIN pb_identity_accounts a ON a.customer_id=o.owner "
        "WHERE r.case_token=?",
        (case_token,),
    ).fetchone()
    if row is None:
        return None
    if isinstance(row[0], str) and row[0].strip():
        return row[0]
    try:
        email = json.loads(row[1])["contact"]["email"]
    except (ValueError, TypeError, KeyError):
        return None
    return email if isinstance(email, str) and email.strip() else None


def _letter(store: _Connected, case_token: str, letter_id: str) -> tuple[bool, str | None]:
    """Whether the confirmed letter went certified, and its tracking number if known."""
    for attempt in (1, 2):
        cursor = store.submission_cursor(case_token, attempt)
        outcome = None if cursor is None else cursor.outcome
        if isinstance(outcome, Accepted) and outcome.letter_id == letter_id:
            prepared = store.get(case_token, attempt)
            if prepared is None:
                raise StorageCorruption("confirmed letter has no prepared request")
            certified = prepared.request.options.mail_class is MailClass.CERTIFIED
            return certified, outcome.tracking_number
    raise StorageCorruption("confirmed letter has no accepted submission")


def _row(values: tuple[Any, ...]) -> TrackingEmail:
    (case_token, letter_id, email, certified, tracking, confirmed, first, tries, flight) = values
    confirmed_at = _stored_time(confirmed)
    first_try_at = None if first is None else _stored_time(first)
    if (
        confirmed_at is None
        or (first is not None and first_try_at is None)
        or type(tries) is not int
        or certified not in (0, 1)
        or flight not in (0, 1)
    ):
        raise StorageCorruption("malformed tracking email row")
    return TrackingEmail(
        case_token,
        letter_id,
        email,
        bool(certified),
        tracking,
        confirmed_at,
        first_try_at,
        tries,
        bool(flight),
    )


class TrackingEmailStoreMixin:
    def queue_tracking_emails(self: _Connected, now: datetime) -> int:
        """Queue one email per newly confirmed letter; returns how many were queued."""
        if not self._submission_locked:
            raise ValueError("tracking email ownership required")
        now = _utc(now)
        database = self._require_open()
        database.execute("BEGIN IMMEDIATE")
        try:
            (mark,) = database.execute(
                "SELECT history_rowid FROM pb_tracking_email_mark WHERE id=1"
            ).fetchone()
            rows = database.execute(
                "SELECT rowid, case_token, event_name, event_json, occurred_at "
                "FROM pb_case_history WHERE rowid>? AND machine='letter' "
                "ORDER BY rowid LIMIT ?",
                (mark, _SCAN_LIMIT),
            ).fetchall()
            queued = 0
            for rowid, case_token, event_name, event_json, occurred_at in rows:
                mark = rowid
                if event_name != "SubmitConfirmed":
                    continue
                try:
                    letter_id = json.loads(event_json)["lob_letter_id"]
                    confirmed_at = _utc(datetime.fromisoformat(occurred_at))
                    if not isinstance(letter_id, str) or not letter_id:
                        raise StorageCorruption("malformed confirmation history")
                    certified, tracking_number = _letter(self, case_token, letter_id)
                except (StorageCorruption, TypeError, ValueError, KeyError):
                    # One bad row must not stop every later email; it is skipped loudly.
                    LOG.warning("tracking email skipped an unreadable confirmation")
                    continue
                email = _contact(database, case_token)
                cursor = database.execute(
                    "INSERT INTO pb_tracking_emails VALUES "
                    "(?, ?, ?, ?, ?, ?, NULL, 0, 0, ?, ?, NULL, ?) ON CONFLICT DO NOTHING",
                    (
                        case_token,
                        letter_id,
                        email or "",
                        int(certified),
                        tracking_number,
                        _stamp(confirmed_at),
                        _stamp(now),
                        None if email else "no_contact",
                        None if email else _stamp(now),
                    ),
                )
                queued += cursor.rowcount
            database.execute(
                "UPDATE pb_tracking_email_mark SET history_rowid=? WHERE id=1", (mark,)
            )
            database.execute("COMMIT")
            return queued
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def due_tracking_emails(
        self: _Connected, now: datetime, *, limit: int = 10
    ) -> tuple[TrackingEmail, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid tracking email batch size")
        rows = (
            self._require_open()
            .execute(
                "SELECT case_token, letter_id, email, certified, tracking_number, "
                "confirmed_at, first_try_at, tries, in_flight FROM pb_tracking_emails "
                "WHERE completed_at IS NULL AND next_at<=? "
                "ORDER BY next_at, case_token LIMIT ?",
                (_stamp(_utc(now)), limit),
            )
            .fetchall()
        )
        return tuple(_row(row) for row in rows)

    def late_tracking_number(self: _Connected, item: TrackingEmail) -> str | None:
        """A tracking number Lob reported for this letter after it was confirmed."""
        for text in self._iter_tracking_payloads(item.case_token):
            try:
                data = json.loads(text)
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            number = data.get("tracking_number")
            if data.get("letter_id") == item.letter_id and isinstance(number, str) and number:
                return number
        return None

    def prepare_tracking_email(
        self: _Connected, item: TrackingEmail, *, now: datetime
    ) -> TrackingEmail | None:
        """Record the next send attempt before the provider is called.

        Returns the frozen email to send, or None when it must not be sent now: it is
        waiting for a tracking number, or the safe retry window has passed (then it ends
        as exhausted, which is never reported as sent).
        """
        if not self._submission_locked:
            raise ValueError("tracking email ownership required")
        now = _utc(now)
        database = self._require_open()
        database.execute("BEGIN IMMEDIATE")
        try:
            current = database.execute(
                "SELECT case_token, letter_id, email, certified, tracking_number, "
                "confirmed_at, first_try_at, tries, in_flight FROM pb_tracking_emails "
                "WHERE case_token=? AND letter_id=? AND completed_at IS NULL",
                (item.case_token, item.letter_id),
            ).fetchone()
            if current is None or _row(current) != item:
                raise ValueError("tracking email row is stale")
            key = (item.case_token, item.letter_id)
            if item.first_try_at is not None and (
                item.tries >= MAX_TRIES or now - item.first_try_at >= SAFE_RETRY_WINDOW
            ):
                database.execute(
                    "UPDATE pb_tracking_emails SET in_flight=0, outcome='exhausted', "
                    "completed_at=? WHERE case_token=? AND letter_id=?",
                    (_stamp(now), *key),
                )
                database.execute("COMMIT")
                return None
            number = item.tracking_number
            if item.first_try_at is None and number is None:
                number = TrackingEmailStoreMixin.late_tracking_number(self, item)
                if number is None and now - item.confirmed_at < TRACKING_NUMBER_WAIT:
                    database.execute(
                        "UPDATE pb_tracking_emails SET next_at=? "
                        "WHERE case_token=? AND letter_id=?",
                        (_stamp(now + timedelta(hours=1)), *key),
                    )
                    database.execute("COMMIT")
                    return None
            first = item.first_try_at or now
            tries = item.tries + 1
            next_at = now + timedelta(seconds=min(30 * 2 ** (tries - 1), 3600))
            database.execute(
                "UPDATE pb_tracking_emails SET tracking_number=?, first_try_at=?, tries=?, "
                "in_flight=1, next_at=? WHERE case_token=? AND letter_id=?",
                (number, _stamp(first), tries, _stamp(next_at), *key),
            )
            database.execute("COMMIT")
            return TrackingEmail(
                item.case_token,
                item.letter_id,
                item.email,
                item.certified,
                number,
                item.confirmed_at,
                first,
                tries,
                True,
            )
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def finish_tracking_email(
        self: _Connected, item: TrackingEmail, outcome: EmailOutcome, *, now: datetime
    ) -> None:
        """Record the provider's answer; only accepted or rejected ends the email."""
        if not self._submission_locked:
            raise ValueError("tracking email ownership required")
        now = _utc(now)
        final = None if outcome.status == "uncertain" else outcome.status
        database = self._require_open()
        database.execute("BEGIN IMMEDIATE")
        try:
            cursor = database.execute(
                "UPDATE pb_tracking_emails SET in_flight=0, outcome=?, provider_id=?, "
                "completed_at=? WHERE case_token=? AND letter_id=? AND tries=? "
                "AND in_flight=1 AND completed_at IS NULL",
                (
                    final,
                    outcome.provider_id,
                    None if final is None else _stamp(now),
                    item.case_token,
                    item.letter_id,
                    item.tries,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("tracking email row is stale")
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
