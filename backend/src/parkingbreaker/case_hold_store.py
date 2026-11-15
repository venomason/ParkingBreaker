"""Per-case incident hold: an overlay on a case, never a case state (T74).

A hold stops new paid research, new drafting and new physical mail initiation for one
case while an incident is looked at. It never creates, cancels or changes an effect,
never touches the frozen packet, and never lapses into permission to send:
``review_due_at`` only escalates. Expiry stops, provider truth, reconciliation, capture
after mailing and hold release keep running. New mail is gated at the durable
create-intent claim: a held case starts no create, while an existing uncertain attempt is
reconciled by lookup only and may settle from provider truth. A hold never recalls or
alters a letter already created, and a matching valid resume gates a later create again.

Each hold carries a generation, bumped by every hold and resume, so a stale resume cannot
clear a newer hold. Every command has a caller-chosen id; repeating it returns its first
outcome, and reusing it for a different request is refused. A hold that is busy is logged
separately and its id stays open, so the same command can be repeated until it takes effect.
The command log is append-only.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error

_HOLDS_DDL = """CREATE TABLE pb_case_holds (
    case_token TEXT PRIMARY KEY,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    held INTEGER NOT NULL CHECK (held IN (0, 1)),
    reason_code TEXT,
    held_at TEXT,
    review_due_at TEXT,
    CHECK ((held = 1) = (reason_code IS NOT NULL AND held_at IS NOT NULL
        AND review_due_at IS NOT NULL))
)"""

_LOG_DDL = """CREATE TABLE pb_case_hold_commands (
    command_id TEXT PRIMARY KEY,
    case_token TEXT NOT NULL,
    command TEXT NOT NULL CHECK (command IN ('hold', 'resume', 'retry')),
    reason_code TEXT,
    expected_generation INTEGER,
    outcome TEXT NOT NULL CHECK (outcome IN ('applied', 'stale', 'rejected')),
    generation INTEGER NOT NULL,
    at TEXT NOT NULL
)"""

# A hold that could not become effective yet; its command id stays open for a retry.
_BUSY_DDL = """CREATE TABLE pb_case_hold_busy (
    command_id TEXT NOT NULL,
    case_token TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    at TEXT NOT NULL
)"""


class HoldReason(StrEnum):
    """Fixed reason codes only; never free text or customer data."""

    INCIDENT = "incident"
    PROVIDER_OUTAGE = "provider_outage"
    CODE_FIX = "code_fix"
    DATA_CHECK = "data_check"


class HoldCommand(StrEnum):
    HOLD = "hold"
    RESUME = "resume"
    RETRY = "retry"


class CommandIdReused(ValueError):
    """A command id was repeated for a different case, command, reason or generation."""


class CommandOutcome(StrEnum):
    APPLIED = "applied"
    BUSY = "busy"  # an operation in flight prevents the hold from being effective yet
    STALE = "stale"  # the expected generation is not the current one
    REJECTED = "rejected"  # not allowed in this case's state


@dataclass(frozen=True)
class CaseHold:
    case_token: str
    generation: int
    held: bool
    reason_code: HoldReason | None
    held_at: datetime | None
    review_due_at: datetime | None


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


def initialize_case_holds(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='case_holds'"
        ).fetchone()
        tables: dict[str, str] = dict(
            database.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name IN ('pb_case_holds', 'pb_case_hold_commands', 'pb_case_hold_busy')"
            ).fetchall()
        )
        if version is None:
            if tables:
                raise StorageCorruption("unversioned case hold schema")
            database.execute(_HOLDS_DDL)
            database.execute(_LOG_DDL)
            database.execute(_BUSY_DDL)
            database.execute("INSERT INTO pb_store_versions VALUES ('case_holds', 1)")
        elif version != (1,) or {
            name: " ".join(sql.upper().split()) for name, sql in tables.items()
        } != {
            "pb_case_holds": " ".join(_HOLDS_DDL.upper().split()),
            "pb_case_hold_commands": " ".join(_LOG_DDL.upper().split()),
            "pb_case_hold_busy": " ".join(_BUSY_DDL.upper().split()),
        }:
            raise StorageCorruption("malformed case hold schema")
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_case_hold_commands_fixed "
            "BEFORE UPDATE ON pb_case_hold_commands "
            "BEGIN SELECT RAISE(ABORT, 'hold commands are append-only'); END"
        )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_case_hold_commands_no_delete "
            "BEFORE DELETE ON pb_case_hold_commands "
            "BEGIN SELECT RAISE(ABORT, 'hold commands are append-only'); END"
        )
        for action in ("UPDATE", "DELETE"):
            database.execute(
                f"CREATE TRIGGER IF NOT EXISTS pb_case_hold_busy_no_{action.lower()} "
                f"BEFORE {action} ON pb_case_hold_busy "
                "BEGIN SELECT RAISE(ABORT, 'busy holds are append-only'); END"
            )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_case_holds_no_delete BEFORE DELETE ON pb_case_holds "
            "BEGIN SELECT RAISE(ABORT, 'case holds are kept'); END"
        )
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def _hold(row: tuple[str, int, int, str | None, str | None, str | None]) -> CaseHold:
    token, generation, held, reason, held_at, due = row
    return CaseHold(
        token,
        int(generation),
        bool(held),
        None if reason is None else HoldReason(reason),
        None if held_at is None else datetime.fromisoformat(held_at),
        None if due is None else datetime.fromisoformat(due),
    )


def _check_command_id(command_id: str) -> None:
    if not command_id.strip() or len(command_id) > 128:
        raise ValueError("invalid command id")


def _first_outcome(
    database: sqlite3.Connection,
    command_id: str,
    case_token: str,
    command: HoldCommand,
    reason: HoldReason | None,
    expected_generation: int | None,
) -> CommandOutcome | None:
    """A repeated command's logged outcome; None if undecided; another request is refused."""
    reason_code = None if reason is None else reason.value
    seen = database.execute(
        "SELECT outcome, case_token, command, reason_code, expected_generation "
        "FROM pb_case_hold_commands WHERE command_id=?",
        (command_id,),
    ).fetchone()
    if seen is not None:
        if tuple(seen[1:]) != (case_token, command.value, reason_code, expected_generation):
            raise CommandIdReused(command_id)
        return CommandOutcome(seen[0])
    busy = database.execute(
        "SELECT case_token, reason_code FROM pb_case_hold_busy WHERE command_id=? LIMIT 1",
        (command_id,),
    ).fetchone()
    if busy is not None and (
        command is not HoldCommand.HOLD or tuple(busy) != (case_token, reason_code)
    ):
        raise CommandIdReused(command_id)
    return None


class CaseHoldStoreMixin:
    def case_hold(self: _Connected, case_token: str) -> CaseHold | None:
        row = (
            self._require_open()
            .execute(
                "SELECT case_token, generation, held, reason_code, held_at, review_due_at "
                "FROM pb_case_holds WHERE case_token=?",
                (case_token,),
            )
            .fetchone()
        )
        return None if row is None else _hold(row)

    def held_cases(self: _Connected) -> tuple[CaseHold, ...]:
        rows = (
            self._require_open()
            .execute(
                "SELECT case_token, generation, held, reason_code, held_at, review_due_at "
                "FROM pb_case_holds WHERE held = 1 ORDER BY case_token"
            )
            .fetchall()
        )
        return tuple(_hold(row) for row in rows)

    def record_hold_command(
        self: _Connected,
        command_id: str,
        case_token: str,
        command: HoldCommand,
        at: datetime,
        *,
        expected_generation: int | None,
        reason: HoldReason | None = None,
        review_due_at: datetime | None = None,
        allowed: bool = True,
    ) -> tuple[CommandOutcome, CaseHold | None]:
        """Apply one command atomically with its log row; a repeated id returns its outcome.

        ``hold`` needs a reason and a review deadline; ``resume`` clears an active hold;
        ``retry`` changes nothing and leaves an active hold in place.
        ``expected_generation`` None means "whatever is current" (hold and retry only).
        ``allowed`` False records the command as rejected (for example a terminal case).
        """
        database = self._require_open()
        if database.in_transaction:
            raise StorageCorruption("hold commands need their own transaction")
        _check_command_id(command_id)
        database.execute("BEGIN IMMEDIATE")
        try:
            seen = _first_outcome(
                database, command_id, case_token, command, reason, expected_generation
            )
            row = database.execute(
                "SELECT case_token, generation, held, reason_code, held_at, review_due_at "
                "FROM pb_case_holds WHERE case_token=?",
                (case_token,),
            ).fetchone()
            current = None if row is None else _hold(row)
            if seen is not None:
                database.execute("COMMIT")
                return seen, current
            generation = 0 if current is None else current.generation
            held = current is not None and current.held
            if not allowed:
                outcome = CommandOutcome.REJECTED
            elif (expected_generation is not None and expected_generation != generation) or (
                command is HoldCommand.RESUME and not held
            ):
                outcome = CommandOutcome.STALE
            else:
                outcome = CommandOutcome.APPLIED
            if outcome is CommandOutcome.APPLIED and command is HoldCommand.HOLD:
                if reason is None or review_due_at is None:
                    raise ValueError("a hold needs a reason and a review deadline")
                generation += 1
                database.execute(
                    "INSERT INTO pb_case_holds VALUES (?, ?, 1, ?, ?, ?) "
                    "ON CONFLICT (case_token) DO UPDATE SET generation=excluded.generation, "
                    "held=1, reason_code=excluded.reason_code, held_at=excluded.held_at, "
                    "review_due_at=excluded.review_due_at",
                    (
                        case_token,
                        generation,
                        reason.value,
                        at.isoformat(),
                        review_due_at.isoformat(),
                    ),
                )
            elif outcome is CommandOutcome.APPLIED and command is HoldCommand.RESUME:
                generation += 1
                database.execute(
                    "UPDATE pb_case_holds SET generation=?, held=0, reason_code=NULL, "
                    "held_at=NULL, review_due_at=NULL WHERE case_token=?",
                    (generation, case_token),
                )
            database.execute(
                "INSERT INTO pb_case_hold_commands VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    command_id,
                    case_token,
                    command.value,
                    None if reason is None else reason.value,
                    expected_generation,
                    outcome.value,
                    generation,
                    at.isoformat(),
                ),
            )
            row = database.execute(
                "SELECT case_token, generation, held, reason_code, held_at, review_due_at "
                "FROM pb_case_holds WHERE case_token=?",
                (case_token,),
            ).fetchone()
            database.execute("COMMIT")
            return outcome, None if row is None else _hold(row)
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def record_busy_hold(
        self: _Connected, command_id: str, case_token: str, reason: HoldReason, at: datetime
    ) -> CommandOutcome | None:
        """Log a hold that could not become effective yet; the same id may be repeated.

        Checked in the same transaction as the log row: a command already decided returns
        its first outcome and logs nothing, and an id bound to another request is refused.
        """
        database = self._require_open()
        if database.in_transaction:
            raise StorageCorruption("hold commands need their own transaction")
        _check_command_id(command_id)
        database.execute("BEGIN IMMEDIATE")
        try:
            seen = _first_outcome(database, command_id, case_token, HoldCommand.HOLD, reason, None)
            if seen is None:
                database.execute(
                    "INSERT INTO pb_case_hold_busy VALUES (?, ?, ?, ?)",
                    (command_id, case_token, reason.value, at.isoformat()),
                )
            database.execute("COMMIT")
            return seen
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
