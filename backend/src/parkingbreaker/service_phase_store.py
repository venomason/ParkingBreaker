"""Append-only private runtime service-phase override on the existing transaction store.

One latest row is the operator override. No row means the configured ``PB_SERVICE_PHASE``
remains in force exactly; there is no invented default. The effective phase is read from
this store on every request, and the new-paid-intent gate reads it again inside its own
``BEGIN IMMEDIATE`` transaction, so a committed change is the enforcement boundary.

Every successful change records its revision, exact phase, command ID, named actor, reason
and UTC timestamp. Rows are never updated or deleted. Reusing a command ID with a different
payload refuses; retrying the same payload returns the saved result. A malformed schema or
an unreadable stored phase raises ``StorageCorruption`` (fail closed) rather than enabling
new paid authorizations.
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
from parkingbreaker.service_phase import ServicePhase, parse_service_phase

COMPONENT = "runtime_service_phase"
TABLE = "pb_runtime_service_phase"
MAX_ACTOR_LENGTH = 120
MAX_REASON_LENGTH = 500

_DDL = """CREATE TABLE pb_runtime_service_phase (
    revision INTEGER PRIMARY KEY CHECK (revision >= 1),
    phase TEXT NOT NULL CHECK (phase IN ('paid', 'controlled_free_beta')),
    command_id TEXT NOT NULL UNIQUE,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)"""

_COMMAND_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_PRINTABLE = re.compile(r"[^\x00-\x1f\x7f]+")
_SELECT = (
    "SELECT revision, phase, command_id, actor, reason, recorded_at "
    f"FROM {TABLE} ORDER BY revision DESC LIMIT 1"
)
_SELECT_BY_COMMAND = (
    "SELECT revision, phase, command_id, actor, reason, recorded_at "
    f"FROM {TABLE} WHERE command_id=?"
)


class ServicePhaseConflict(ValueError):
    """A stale or reused operator action, or a payload outside the strict bounds."""


@dataclass(frozen=True)
class ServicePhaseChange:
    revision: int
    phase: ServicePhase
    command_id: str
    actor: str
    reason: str
    recorded_at: datetime
    replayed: bool = False


@dataclass(frozen=True)
class ServicePhaseStatus:
    """The stored override, honestly absent when the configured fallback is in force."""

    stored_override: ServicePhase | None
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
        raise ServicePhaseConflict(f"invalid {field}")
    return value


def _command_id(value: object) -> str:
    if not isinstance(value, str) or _COMMAND_ID.fullmatch(value) is None:
        raise ServicePhaseConflict("invalid command_id")
    return value


def _utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ServicePhaseConflict("invalid recorded time")
    return value.astimezone(UTC)


def _change_from_row(row: tuple[object, ...]) -> ServicePhaseChange:
    revision, phase_text, command_id, actor, reason, recorded_text = row
    try:
        if type(revision) is not int or revision < 1:
            raise ValueError("invalid revision")
        if not isinstance(phase_text, str):
            raise ValueError("invalid phase")
        phase = parse_service_phase(phase_text)
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
        raise StorageCorruption("invalid stored runtime service phase") from exc
    return ServicePhaseChange(revision, phase, command_id, actor, reason, recorded_at)


def initialize_runtime_service_phase(database: sqlite3.Connection) -> None:
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
                raise StorageCorruption("unversioned runtime service phase schema")
            database.execute(_DDL)
            database.execute("INSERT INTO pb_store_versions VALUES (?, 1)", (COMPONENT,))
        elif (
            type(version[0]) is not int
            or version[0] != 1
            or table is None
            or " ".join(str(table[0]).upper().split()) != " ".join(_DDL.upper().split())
        ):
            raise StorageCorruption("malformed runtime service phase schema")
        for action in ("UPDATE", "DELETE"):
            database.execute(
                f"CREATE TRIGGER IF NOT EXISTS {TABLE}_no_{action.lower()} "
                f"BEFORE {action} ON {TABLE} "
                "BEGIN SELECT RAISE(ABORT, 'runtime service phase history is append-only'); END"
            )
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...

    def _runtime_service_phase_row(
        self, database: sqlite3.Connection
    ) -> tuple[object, ...] | None: ...

    def _runtime_service_phase_in_transaction(
        self, database: sqlite3.Connection
    ) -> ServicePhase | None: ...


class ServicePhaseStoreMixin:
    """Runtime phase reads and the one append-only operator write, on the shared store."""

    def _runtime_service_phase_row(
        self: _Connected, database: sqlite3.Connection
    ) -> tuple[object, ...] | None:
        row = database.execute(_SELECT).fetchone()
        return None if row is None else cast(tuple[object, ...], row)

    def _runtime_service_phase_in_transaction(
        self: _Connected, database: sqlite3.Connection
    ) -> ServicePhase | None:
        """Read the committed override inside the caller's write transaction."""
        row = self._runtime_service_phase_row(database)
        return None if row is None else _change_from_row(row).phase

    def effective_service_phase(self: _Connected, fallback: ServicePhase) -> ServicePhase:
        """The stored override when present, otherwise exactly the configured fallback."""
        if not isinstance(fallback, ServicePhase):
            raise ValueError("fallback must be a service phase")
        override = self._runtime_service_phase_in_transaction(self._require_open())
        return fallback if override is None else override

    def runtime_service_phase_status(self: _Connected) -> ServicePhaseStatus:
        row = self._runtime_service_phase_row(self._require_open())
        if row is None:
            return ServicePhaseStatus(None, None, None, None, None, None)
        change = _change_from_row(row)
        return ServicePhaseStatus(
            change.phase,
            change.revision,
            change.command_id,
            change.actor,
            change.reason,
            change.recorded_at,
        )

    def set_runtime_service_phase(
        self: _Connected,
        phase: ServicePhase,
        expected_revision: int,
        command_id: str,
        actor: str,
        reason: str,
        *,
        now: datetime,
    ) -> ServicePhaseChange:
        """Append one operator change under an exact expected revision.

        The same command ID with the same original payload returns its saved result. The
        same ID with any changed payload refuses. A new command whose expected revision is
        not the current revision refuses without writing.
        """
        if not isinstance(phase, ServicePhase):
            raise ServicePhaseConflict("invalid phase")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ServicePhaseConflict("invalid expected_revision")
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
                    saved.phase is phase
                    and saved.actor == named_actor
                    and saved.reason == explanation
                    and expected_revision == saved.revision - 1
                ):
                    database.execute("COMMIT")
                    return replace(saved, replayed=True)
                raise ServicePhaseConflict("command_id_payload_changed")
            current = self._runtime_service_phase_row(database)
            current_revision = 0 if current is None else _change_from_row(current).revision
            if expected_revision != current_revision:
                raise ServicePhaseConflict("stale_expected_revision")
            revision = current_revision + 1
            database.execute(
                f"INSERT INTO {TABLE} "
                "(revision, phase, command_id, actor, reason, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    revision,
                    phase.value,
                    command,
                    named_actor,
                    explanation,
                    recorded_at.isoformat(),
                ),
            )
            saved_row = database.execute(
                "SELECT revision, phase, command_id, actor, reason, recorded_at "
                f"FROM {TABLE} WHERE revision=?",
                (revision,),
            ).fetchone()
            if saved_row is None:
                raise StorageCorruption("missing saved runtime service phase")
            change = _change_from_row(saved_row)
            database.execute("COMMIT")
            return change
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
