"""Early Lob webhook evidence: an indexed, bounded backlog and a record of final outcomes.

A verified, reduced envelope that arrives before its local submission identity waits in
``pb_webhook_backlog``, keyed by the letter id and submission token it carries. Every
lookup here is answered from an index; nothing scans the backlog or walks the cases. An
event that can never bind leaves the backlog for the bounded ``pb_webhook_unmatched``
table, with a reason code and the time. No provider calls, no clocks, no environment.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import NamedTuple, Protocol

from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error

# Rows the unmatched table keeps; the oldest beyond this are dropped in the same commit.
WEBHOOK_UNMATCHED_LIMIT = 5000
UNMATCHED_EXPIRED = "UNMATCHED_EXPIRED"
CONTRADICTS_CASE_STATE = "CONTRADICTS_CASE_STATE"
CONFLICTING_LETTER = "CONFLICTING_LETTER"
# Lob's own failed/deleted notice for a letter this system canceled: it agrees with the case.
CANCEL_CONFIRMED = "CANCEL_CONFIRMED"
UNMATCHED_REASONS = frozenset(
    {UNMATCHED_EXPIRED, CONTRADICTS_CASE_STATE, CONFLICTING_LETTER, CANCEL_CONFIRMED}
)

_COMPONENT = "webhook_backlog"
_VERSION = 3

_V1_DDL = """CREATE TABLE pb_webhook_backlog (
    provider TEXT NOT NULL CHECK (provider = 'lob'),
    event_id TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    received_at TEXT NOT NULL,
    PRIMARY KEY(provider, event_id)
)"""

# ALTER TABLE ADD COLUMN produces exactly this text, so a migrated table and a fresh one
# are identical.
_V2_DDL = """CREATE TABLE pb_webhook_backlog (
    provider TEXT NOT NULL CHECK (provider = 'lob'),
    event_id TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    received_at TEXT NOT NULL, letter_id TEXT, submission_token TEXT,
    PRIMARY KEY(provider, event_id)
)"""

_BACKLOG_DDL = _V2_DDL.replace(
    "submission_token TEXT,",
    "submission_token TEXT, replay_order INTEGER NOT NULL DEFAULT 0,",
)

_UNMATCHED_DDL = """CREATE TABLE pb_webhook_unmatched (
    provider TEXT NOT NULL CHECK (provider = 'lob'),
    event_id TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    reason TEXT NOT NULL CHECK (reason <> ''),
    received_at TEXT NOT NULL,
    moved_at TEXT NOT NULL,
    PRIMARY KEY(provider, event_id)
)"""

_TOMBSTONES_DDL = """CREATE TABLE pb_webhook_final_events (
    provider TEXT NOT NULL CHECK (provider = 'lob'),
    event_id TEXT NOT NULL,
    PRIMARY KEY(provider, event_id)
)"""

_INDEXES = (
    (
        "pb_webhook_backlog_letter",
        "CREATE INDEX pb_webhook_backlog_letter ON pb_webhook_backlog(letter_id)",
    ),
    (
        "pb_webhook_backlog_token",
        "CREATE INDEX pb_webhook_backlog_token ON pb_webhook_backlog(submission_token)",
    ),
    (
        "pb_webhook_backlog_received",
        "CREATE INDEX pb_webhook_backlog_received ON pb_webhook_backlog(received_at)",
    ),
    (
        "pb_webhook_backlog_replay",
        "CREATE INDEX pb_webhook_backlog_replay ON pb_webhook_backlog(replay_order)",
    ),
    # The letter id of a cursor lives in its outcome JSON; an expression index answers
    # "which attempt holds this letter" without reading any other cursor.
    (
        "pb_submission_cursors_letter",
        "CREATE INDEX pb_submission_cursors_letter "
        "ON pb_submission_cursors(json_extract(outcome_json, '$.letter_id'))",
    ),
)

_BACKFILL = """UPDATE pb_webhook_backlog SET
    letter_id = CASE WHEN json_type(payload, '$.body.id') = 'text'
                     THEN json_extract(payload, '$.body.id') END,
    submission_token = CASE WHEN json_type(payload, '$.body.metadata.submission_token') = 'text'
                            THEN json_extract(payload, '$.body.metadata.submission_token') END"""

_ROW = "SELECT provider, event_id, payload, received_at FROM pb_webhook_backlog"

# Waiting rows move to the tail without changing immutable receipt/retention time.
WEBHOOK_BACKLOG_BATCH_SQL = _ROW + " ORDER BY replay_order, rowid LIMIT ?"
WEBHOOK_BACKLOG_ROW_SQL = _ROW + " WHERE provider=? AND event_id=?"
# Rows carrying a letter id or token that one case holds: only its own matching rows.
WEBHOOK_BACKLOG_FOR_CASE_SQL = (
    _ROW + " WHERE letter_id IN (SELECT json_extract(outcome_json, '$.letter_id') "
    "FROM pb_submission_cursors WHERE case_token=?)"
    " OR submission_token IN (SELECT submission_token FROM pb_mail_attempts WHERE case_token=?)"
    " ORDER BY replay_order, rowid LIMIT ?"
)
WEBHOOK_BACKLOG_TOKEN_LETTERS_SQL = (
    "SELECT DISTINCT letter_id FROM pb_webhook_backlog WHERE submission_token=?"
)
WEBHOOK_ATTEMPTS_BY_LETTER_SQL = (
    "SELECT case_token, attempt FROM pb_submission_cursors "
    "WHERE json_extract(outcome_json, '$.letter_id') = ? ORDER BY case_token, attempt"
)
WEBHOOK_ATTEMPT_BY_TOKEN_SQL = (
    "SELECT case_token, attempt FROM pb_mail_attempts WHERE submission_token=?"
)
WEBHOOK_CASE_BY_AUTHORIZATION_SQL = "SELECT case_token FROM pb_cases WHERE authorization_ref=?"


class BacklogRow(NamedTuple):
    provider: str
    event_id: str
    payload: str
    received_at: datetime


class WebhookEnvelopeConflict(Exception):
    """The first verified envelope won a concurrent delivery of this event identity."""

    def __init__(self, row: BacklogRow) -> None:
        super().__init__("webhook envelope changed before commit")
        self.row = row


class UnmatchedWebhook(NamedTuple):
    provider: str
    event_id: str
    payload: str
    reason: str
    received_at: datetime
    moved_at: datetime


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


def _normal(sql: object) -> str:
    return " ".join(str(sql).upper().split())


def _stamp(moment: datetime) -> str:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("webhook times must be timezone-aware")
    return moment.astimezone(UTC).isoformat()


def _moment(value: object) -> datetime:
    try:
        if not isinstance(value, str):
            raise ValueError("not text")
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            raise ValueError("naive time")
    except ValueError as exc:
        raise StorageCorruption("invalid webhook backlog time") from exc
    return moment


def _backlog_row(row: tuple[object, ...]) -> BacklogRow:
    provider, event_id, payload, received = row
    if not (isinstance(provider, str) and isinstance(event_id, str) and isinstance(payload, str)):
        raise StorageCorruption("invalid webhook backlog row")
    return BacklogRow(provider, event_id, payload, _moment(received))


def backlog_keys(payload: str) -> tuple[str | None, str | None]:
    """(letter id, submission token) of a reduced envelope; anything else is no key."""
    data = json.loads(payload)
    body = data.get("body") if isinstance(data, dict) else None
    if not isinstance(body, dict):
        return None, None
    letter_id = body.get("id")
    metadata = body.get("metadata")
    token = metadata.get("submission_token") if isinstance(metadata, dict) else None
    return (
        letter_id if isinstance(letter_id, str) else None,
        token if isinstance(token, str) else None,
    )


def settled_webhook(db: sqlite3.Connection, provider: str, event_id: str) -> bool:
    """True once the event is bound or has moved to the unmatched record: its final outcome."""
    return (
        db.execute(
            "SELECT 1 FROM pb_webhooks WHERE provider=? AND event_id=?", (provider, event_id)
        ).fetchone()
        is not None
        or db.execute(
            "SELECT 1 FROM pb_webhook_final_events WHERE provider=? AND event_id=?",
            (provider, event_id),
        ).fetchone()
        is not None
    )


def park_webhook(
    db: sqlite3.Connection, provider: str, event_id: str, payload: str, received_at: datetime
) -> bool:
    """Store a verified reduced envelope with its keys; the first copy wins. Open transaction."""
    letter_id, token = backlog_keys(payload)
    return (
        db.execute(
            "INSERT OR IGNORE INTO pb_webhook_backlog "
            "(provider, event_id, payload, received_at, letter_id, submission_token, replay_order) "
            "VALUES (?, ?, ?, ?, ?, ?, "
            "(SELECT COALESCE(MAX(replay_order), 0) + 1 FROM pb_webhook_backlog))",
            (provider, event_id, payload, _stamp(received_at), letter_id, token),
        ).rowcount
        == 1
    )


def pin_webhook_envelope(
    db: sqlite3.Connection, provider: str, event_id: str, payload: str, received_at: datetime
) -> bool:
    """Pin/check the first envelope inside its effects transaction, without a normal read.

    The conditional write protects a cached replay row too. Only a competing first
    envelope requires a read and a bounded retry through the common decision path.
    """
    if park_webhook(db, provider, event_id, payload, received_at):
        return True
    if (
        db.execute(
            "UPDATE pb_webhook_backlog SET payload=payload "
            "WHERE provider=? AND event_id=? AND payload=?",
            (provider, event_id, payload),
        ).rowcount
        != 1
    ):
        row = db.execute(WEBHOOK_BACKLOG_ROW_SQL, (provider, event_id)).fetchone()
        if row is None:
            raise StorageCorruption("missing pinned webhook envelope")
        raise WebhookEnvelopeConflict(_backlog_row(row))
    return False


def initialize_webhook_backlog(database: sqlite3.Connection) -> None:
    """Expand-only v3: durable fair replay order and final-event identities."""
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component=?", (_COMPONENT,)
        ).fetchone()
        table = database.execute(
            "SELECT sql FROM sqlite_master WHERE name='pb_webhook_backlog'"
        ).fetchone()
        if version is None:
            if table is not None:
                raise StorageCorruption("unversioned webhook backlog schema")
            database.execute(_BACKLOG_DDL)
            _create_missing(database, tables=True)
            _create_tombstones(database)
            database.execute("INSERT INTO pb_store_versions VALUES (?, ?)", (_COMPONENT, _VERSION))
        elif version == (1,) and table is not None and _normal(table[0]) == _normal(_V1_DDL):
            database.execute("ALTER TABLE pb_webhook_backlog ADD COLUMN letter_id TEXT")
            database.execute("ALTER TABLE pb_webhook_backlog ADD COLUMN submission_token TEXT")
            database.execute(_BACKFILL)
            _add_replay_order(database)
            _create_missing(database, tables=True)
            _create_tombstones(database)
            _backfill_tombstones(database)
            database.execute(
                "UPDATE pb_store_versions SET version=? WHERE component=?", (_VERSION, _COMPONENT)
            )
        elif version == (2,):
            _create_missing(database, tables=False, version=2)
            _check_schema(database, version=2)
            _add_replay_order(database)
            _create_missing(database, tables=False)
            _create_tombstones(database)
            _backfill_tombstones(database)
            database.execute(
                "UPDATE pb_store_versions SET version=? WHERE component=?", (_VERSION, _COMPONENT)
            )
        elif version != (_VERSION,):
            raise StorageCorruption("unsupported webhook backlog schema")
        else:
            # An index is derived data. The submission component can rebuild its cursor
            # table (and so lose the letter index); rebuilding an index never loses data.
            _create_missing(database, tables=False)
        _check_schema(database)
        database.execute("COMMIT")
    except BaseException:
        rollback_preserving_error(database)
        raise


def _create_missing(database: sqlite3.Connection, *, tables: bool, version: int = _VERSION) -> None:
    """Add what is absent (the unmatched table only when asked); the check verifies the rest."""
    wanted = [("pb_webhook_unmatched", _UNMATCHED_DDL)] if tables else []
    for name, ddl in (*wanted, *_indexes(version)):
        if database.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is None:
            database.execute(ddl)


def _indexes(version: int) -> tuple[tuple[str, str], ...]:
    return tuple(
        item for item in _INDEXES if version != 2 or item[0] != "pb_webhook_backlog_replay"
    )


def _add_replay_order(database: sqlite3.Connection) -> None:
    database.execute(
        "ALTER TABLE pb_webhook_backlog ADD COLUMN replay_order INTEGER NOT NULL DEFAULT 0"
    )
    database.execute(
        "WITH ordered AS (SELECT rowid AS position, "
        "ROW_NUMBER() OVER (ORDER BY received_at, rowid) AS ordinal FROM pb_webhook_backlog) "
        "UPDATE pb_webhook_backlog SET replay_order="
        "(SELECT ordinal FROM ordered WHERE position=pb_webhook_backlog.rowid)"
    )


def _create_tombstones(database: sqlite3.Connection) -> None:
    if (
        database.execute(
            "SELECT 1 FROM sqlite_master WHERE name='pb_webhook_final_events'"
        ).fetchone()
        is None
    ):
        database.execute(_TOMBSTONES_DDL)


def _backfill_tombstones(database: sqlite3.Connection) -> None:
    database.execute(
        "INSERT OR IGNORE INTO pb_webhook_final_events (provider, event_id) "
        "SELECT provider, event_id FROM pb_webhook_unmatched"
    )


def _check_schema(database: sqlite3.Connection, *, version: int = _VERSION) -> None:
    expected: list[tuple[str, str]] = [
        ("pb_webhook_backlog", _V2_DDL if version == 2 else _BACKLOG_DDL),
        ("pb_webhook_unmatched", _UNMATCHED_DDL),
        *_indexes(version),
    ]
    if version >= 3:
        expected.append(("pb_webhook_final_events", _TOMBSTONES_DDL))
    for name, ddl in expected:
        found = database.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if found is None or _normal(found[0]) != _normal(ddl):
            raise StorageCorruption("unsupported webhook backlog schema")


class WebhookBacklogStoreMixin:
    """Reads and the final-outcome move; the binding write stays in ``record_webhook``."""

    def pending_webhooks(self: _Connected) -> tuple[tuple[str, str, str], ...]:
        """Every parked envelope. Unbounded: for diagnostics and tests, never the sweep."""
        return tuple(
            (row[0], row[1], row[2])
            for row in self._require_open().execute(
                "SELECT provider, event_id, payload FROM pb_webhook_backlog ORDER BY rowid"
            )
        )

    def backlog_batch(self: _Connected, limit: int) -> tuple[BacklogRow, ...]:
        """At most ``limit`` parked envelopes in persistent fair replay order."""
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        return tuple(
            _backlog_row(row)
            for row in self._require_open().execute(WEBHOOK_BACKLOG_BATCH_SQL, (limit,))
        )

    def defer_backlog(self: _Connected, row: BacklogRow) -> None:
        """A still-waiting row joins the tail; stale replays cannot change another envelope."""
        self._require_open().execute(
            "UPDATE pb_webhook_backlog SET replay_order="
            "(SELECT COALESCE(MAX(replay_order), 0) + 1 FROM pb_webhook_backlog) "
            "WHERE provider=? AND event_id=? AND payload=?",
            (row.provider, row.event_id, row.payload),
        )

    def backlog_row(self: _Connected, provider: str, event_id: str) -> BacklogRow | None:
        row = self._require_open().execute(WEBHOOK_BACKLOG_ROW_SQL, (provider, event_id)).fetchone()
        return None if row is None else _backlog_row(row)

    def backlog_for_case(self: _Connected, case_id: str, limit: int) -> tuple[BacklogRow, ...]:
        """Parked envelopes whose letter id or token this case holds; reads only those rows."""
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        database = self._require_open()
        if database.execute("SELECT 1 FROM pb_webhook_backlog LIMIT 1").fetchone() is None:
            return ()
        return tuple(
            _backlog_row(row)
            for row in database.execute(WEBHOOK_BACKLOG_FOR_CASE_SQL, (case_id, case_id, limit))
        )

    def backlog_letters_for_token(self: _Connected, token: str) -> frozenset[str]:
        """Letter ids of the parked envelopes carrying this submission token."""
        return frozenset(
            row[0]
            for row in self._require_open().execute(WEBHOOK_BACKLOG_TOKEN_LETTERS_SQL, (token,))
            if isinstance(row[0], str)
        )

    def attempts_for_letter(self: _Connected, letter_id: str) -> tuple[tuple[str, int], ...]:
        """(case, attempt) pairs whose cursor holds this provider letter id."""
        return tuple(
            (str(row[0]), int(row[1]))
            for row in self._require_open().execute(WEBHOOK_ATTEMPTS_BY_LETTER_SQL, (letter_id,))
        )

    def attempt_for_submission_token(self: _Connected, token: str) -> tuple[str, int] | None:
        row = self._require_open().execute(WEBHOOK_ATTEMPT_BY_TOKEN_SQL, (token,)).fetchone()
        return None if row is None else (str(row[0]), int(row[1]))

    def case_for_authorization(self: _Connected, authorization_ref: str) -> str | None:
        row = (
            self._require_open()
            .execute(WEBHOOK_CASE_BY_AUTHORIZATION_SQL, (authorization_ref,))
            .fetchone()
        )
        return None if row is None else str(row[0])

    def unmatched_webhooks(
        self: _Connected, limit: int = WEBHOOK_UNMATCHED_LIMIT
    ) -> tuple[UnmatchedWebhook, ...]:
        """The newest ``limit`` moved envelopes, oldest of them first."""
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        rows = self._require_open().execute(
            "SELECT provider, event_id, payload, reason, received_at, moved_at FROM "
            "(SELECT rowid AS position, * FROM pb_webhook_unmatched "
            "ORDER BY position DESC LIMIT ?) ORDER BY position",
            (limit,),
        )
        return tuple(
            UnmatchedWebhook(
                str(row[0]), str(row[1]), str(row[2]), str(row[3]), _moment(row[4]), _moment(row[5])
            )
            for row in rows
        )

    def move_unmatched(
        self: _Connected,
        provider: str,
        event_id: str,
        payload: str,
        reason: str,
        *,
        received_at: datetime,
        moved_at: datetime,
        retain: int = WEBHOOK_UNMATCHED_LIMIT,
    ) -> bool:
        """Give an event that can never bind its final outcome, atomically.

        Leaves the backlog and joins the unmatched record in one commit. False when the
        event already has a final outcome (bound, or moved earlier): nothing is recorded.
        The record keeps the newest ``retain`` rows, so it cannot grow without bound.
        """
        if reason not in UNMATCHED_REASONS:
            raise ValueError("unknown unmatched reason")
        if type(retain) is not int or retain < 1:
            raise ValueError("retain must be a positive integer")
        db = self._require_open()
        db.execute("BEGIN IMMEDIATE")
        try:
            settled = settled_webhook(db, provider, event_id)
            if not settled:
                pin_webhook_envelope(db, provider, event_id, payload, received_at)
            db.execute(
                "DELETE FROM pb_webhook_backlog WHERE provider=? AND event_id=?",
                (provider, event_id),
            )
            if not settled:
                db.execute(
                    "INSERT INTO pb_webhook_final_events (provider, event_id) VALUES (?, ?)",
                    (provider, event_id),
                )
                db.execute(
                    "INSERT INTO pb_webhook_unmatched VALUES (?, ?, ?, ?, ?, ?)",
                    (provider, event_id, payload, reason, _stamp(received_at), _stamp(moved_at)),
                )
                db.execute(
                    "DELETE FROM pb_webhook_unmatched WHERE rowid <= "
                    "(SELECT rowid FROM pb_webhook_unmatched ORDER BY rowid DESC LIMIT 1 OFFSET ?)",
                    (retain,),
                )
            db.execute("COMMIT")
            return not settled
        except BaseException:
            rollback_preserving_error(db)
            raise
