"""Append-only private runtime intake kill-switch on the existing transaction store.

One latest row is the operator override: ``paused`` stops every *new* intake (paid and
free) while accepted cases keep their holds, callbacks, recovery and completion paths.
No row means intake is open, so a store that never used the switch behaves exactly as
before.

Every change records its revision, the exact state, command ID, named actor, reason and
UTC timestamp; rows are never updated or deleted. Reusing a command ID with a different
payload refuses; retrying the same payload returns the saved result. A malformed schema or
an unreadable stored state raises ``StorageCorruption`` (fail closed) rather than quietly
accepting new intake.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol, cast

from parkingbreaker.mail_attempt_store import (
    StorageCorruption,
    check_versions_schema,
    rollback_preserving_error,
)

COMPONENT = "runtime_intake_pause"
TABLE = "pb_runtime_intake_pause"
MAX_ACTOR_LENGTH = 120
MAX_REASON_LENGTH = 500

_DDL = """CREATE TABLE pb_runtime_intake_pause (
    revision INTEGER PRIMARY KEY CHECK (revision >= 1),
    paused INTEGER NOT NULL CHECK (paused IN (0, 1)),
    command_id TEXT NOT NULL UNIQUE,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)"""

_COMMAND_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_PRINTABLE = re.compile(r"[^\x00-\x1f\x7f]+")
_SELECT = (
    "SELECT revision, paused, command_id, actor, reason, recorded_at "
    f"FROM {TABLE} ORDER BY revision DESC LIMIT 1"
)
_SELECT_BY_COMMAND = (
    "SELECT revision, paused, command_id, actor, reason, recorded_at "
    f"FROM {TABLE} WHERE command_id=?"
)
LINEAGE_TABLE = "pb_workspace_lineage"
_LINEAGE_DDL = """CREATE TABLE pb_workspace_lineage (
    child_workspace TEXT PRIMARY KEY REFERENCES pb_intake_owners(workspace_token),
    parent_workspace TEXT NOT NULL REFERENCES pb_intake_owners(workspace_token),
    reason TEXT NOT NULL CHECK (reason = 'restart'),
    recorded_at TEXT NOT NULL,
    CHECK (child_workspace <> parent_workspace)
)"""


class IntakePauseConflict(ValueError):
    """A stale or reused operator action, or a payload outside the strict bounds."""


@dataclass(frozen=True)
class IntakePauseChange:
    revision: int
    paused: bool
    command_id: str
    actor: str
    reason: str
    recorded_at: datetime
    replayed: bool = False


@dataclass(frozen=True)
class IntakePauseStatus:
    """The stored override, honestly absent while intake is open by default."""

    paused: bool | None
    revision: int | None
    command_id: str | None
    actor: str | None
    reason: str | None
    recorded_at: datetime | None


def _text(value: object, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or _PRINTABLE.fullmatch(value) is None
    ):
        raise IntakePauseConflict(f"invalid {field}")
    return value


def _command_id(value: object) -> str:
    if not isinstance(value, str) or _COMMAND_ID.fullmatch(value) is None:
        raise IntakePauseConflict("invalid command_id")
    return value


def _utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise IntakePauseConflict("invalid recorded time")
    return value.astimezone(UTC)


def _change_from_row(row: tuple[object, ...]) -> IntakePauseChange:
    revision, paused, command_id, actor, reason, recorded_text = row
    try:
        if type(revision) is not int or revision < 1:
            raise ValueError("invalid revision")
        if type(paused) is not int or paused not in (0, 1):
            raise ValueError("invalid paused flag")
        if (
            not isinstance(command_id, str)
            or _COMMAND_ID.fullmatch(command_id) is None
            or not isinstance(actor, str)
            or not actor
            or len(actor) > MAX_ACTOR_LENGTH
            or actor != actor.strip()
            or not isinstance(reason, str)
            or not reason
            or len(reason) > MAX_REASON_LENGTH
            or reason != reason.strip()
            or not isinstance(recorded_text, str)
        ):
            raise ValueError("invalid operator fields")
        recorded_at = datetime.fromisoformat(recorded_text)
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise ValueError("timestamp is not timezone-aware")
    except (TypeError, ValueError, OverflowError) as exc:
        raise StorageCorruption("invalid stored runtime intake pause") from exc
    return IntakePauseChange(revision, bool(paused), command_id, actor, reason, recorded_at)


def initialize_runtime_intake_pause(database: sqlite3.Connection) -> None:
    """Create or validate the versioned append-only table inside a caller transaction."""
    database.execute("BEGIN IMMEDIATE")
    try:
        check_versions_schema(database)
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component=?", (COMPONENT,)
        ).fetchone()
        table = database.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
        if version is None:
            if table is not None:
                raise StorageCorruption("unversioned runtime intake pause schema")
            database.execute(_DDL)
            database.execute(_LINEAGE_DDL)
            database.execute("INSERT INTO pb_store_versions VALUES (?, 1)", (COMPONENT,))
        elif (
            type(version[0]) is not int
            or version[0] != 1
            or table is None
            or " ".join(str(table[0]).upper().split()) != " ".join(_DDL.upper().split())
        ):
            raise StorageCorruption("malformed runtime intake pause schema")
        lineage = database.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (LINEAGE_TABLE,)
        ).fetchone()
        if lineage is None or " ".join(str(lineage[0]).upper().split()) != " ".join(
            _LINEAGE_DDL.upper().split()
        ):
            raise StorageCorruption("malformed workspace lineage schema")
        for action in ("UPDATE", "DELETE"):
            database.execute(
                f"CREATE TRIGGER IF NOT EXISTS {LINEAGE_TABLE}_no_{action.lower()} "
                f"BEFORE {action} ON {LINEAGE_TABLE} "
                "BEGIN SELECT RAISE(ABORT, 'workspace lineage is append-only'); END"
            )
        for action in ("UPDATE", "DELETE"):
            database.execute(
                f"CREATE TRIGGER IF NOT EXISTS {TABLE}_no_{action.lower()} "
                f"BEFORE {action} ON {TABLE} "
                "BEGIN SELECT RAISE(ABORT, 'runtime intake pause history is append-only'); END"
            )
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...

    def _intake_pause_row(self, database: sqlite3.Connection) -> tuple[object, ...] | None: ...


class IntakePauseStoreMixin:
    """The runtime intake kill-switch: reads plus one append-only operator write."""

    def _intake_pause_row(
        self: _Connected, database: sqlite3.Connection
    ) -> tuple[object, ...] | None:
        row = database.execute(_SELECT).fetchone()
        return None if row is None else cast(tuple[object, ...], row)

    def intake_paused(self: _Connected) -> bool:
        """Whether new intake is stopped; an unused switch is open, never invented."""
        row = self._intake_pause_row(self._require_open())
        return False if row is None else _change_from_row(row).paused

    def intake_pause_status(self: _Connected) -> IntakePauseStatus:
        row = self._intake_pause_row(self._require_open())
        if row is None:
            return IntakePauseStatus(None, None, None, None, None, None)
        change = _change_from_row(row)
        return IntakePauseStatus(
            change.paused,
            change.revision,
            change.command_id,
            change.actor,
            change.reason,
            change.recorded_at,
        )

    def record_workspace_lineage(
        self: _Connected, child: str, parent: str, *, now: datetime
    ) -> None:
        """Record why a workspace exists: a restart of the parent's finished transaction."""
        if not child or not parent or child == parent:
            raise IntakePauseConflict("invalid workspace lineage")
        database = self._require_open()
        database.execute(
            f"INSERT INTO {LINEAGE_TABLE} (child_workspace, parent_workspace, reason, "
            "recorded_at) VALUES (?, ?, 'restart', ?)",
            (child, parent, _utc(now).isoformat()),
        )

    def workspace_parent(self: _Connected, child: str) -> str | None:
        row = (
            self._require_open()
            .execute(
                f"SELECT parent_workspace FROM {LINEAGE_TABLE} WHERE child_workspace=?", (child,)
            )
            .fetchone()
        )
        return None if row is None else str(row[0])

    def set_intake_pause(
        self: _Connected,
        paused: bool,
        expected_revision: int,
        command_id: str,
        actor: str,
        reason: str,
        *,
        now: datetime,
    ) -> IntakePauseChange:
        """Append one operator change under an exact expected revision.

        The same command ID with the same original payload returns its saved result. The
        same ID with any changed payload refuses. A new command whose expected revision is
        not the current revision refuses without writing.
        """
        if not isinstance(paused, bool):
            raise IntakePauseConflict("invalid paused flag")
        if type(expected_revision) is not int or expected_revision < 0:
            raise IntakePauseConflict("invalid expected_revision")
        command = _command_id(command_id)
        named_actor = _text(actor, "actor", MAX_ACTOR_LENGTH)
        explanation = _text(reason, "reason", MAX_REASON_LENGTH)
        recorded_at = _utc(now)
        database = self._require_open()
        database.execute("BEGIN IMMEDIATE")
        try:
            existing = database.execute(_SELECT_BY_COMMAND, (command,)).fetchone()
            if existing is not None:
                saved = _change_from_row(existing)
                if (
                    saved.paused is paused
                    and saved.actor == named_actor
                    and saved.reason == explanation
                    and expected_revision == saved.revision - 1
                ):
                    database.execute("COMMIT")
                    return replace(saved, replayed=True)
                raise IntakePauseConflict("command_id_payload_changed")
            current = self._intake_pause_row(database)
            current_revision = 0 if current is None else _change_from_row(current).revision
            if expected_revision != current_revision:
                raise IntakePauseConflict("stale_expected_revision")
            revision = current_revision + 1
            database.execute(
                f"INSERT INTO {TABLE} "
                "(revision, paused, command_id, actor, reason, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    revision,
                    1 if paused else 0,
                    command,
                    named_actor,
                    explanation,
                    recorded_at.isoformat(),
                ),
            )
            saved_row = database.execute(
                "SELECT revision, paused, command_id, actor, reason, recorded_at "
                f"FROM {TABLE} WHERE revision=?",
                (revision,),
            ).fetchone()
            if saved_row is None:
                raise StorageCorruption("missing saved runtime intake pause")
            change = _change_from_row(saved_row)
            database.execute("COMMIT")
            return change
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
