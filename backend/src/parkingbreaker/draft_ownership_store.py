"""Per-case drafting ownership and a durable record of paid drafting attempts (T40).

Only one process or thread may run a case's paid drafting call at a time. Ownership is a
Linux open-file-description (OFD) byte-range write lock on a sidecar file next to the
database, at a byte offset (slot) persisted once per case. OFD locks belong to the open
file description, so every contender opens the file itself: two threads in one process
exclude each other, closing an unrelated descriptor never drops the lock, and process
death releases it. The lock never expires while its owner is alive, so a slow model call
cannot be overtaken by a second paid call. The database file itself is never locked here:
SQLite takes its own POSIX locks on it, which OFD locks would conflict with. Paid research
takes the same per-case ownership, so an incident hold (T74) cannot land mid-call.

Each drafting run (the first draft and its automatic corrections) is recorded before its
first call, and each case and research revision allows ``DRAFT_RUN_LIMIT`` runs: a normal
run plus one recovery run after a crash. Every paid model call records its number and a
fingerprint of its own inputs (including correction feedback) before the call, and whether
it returned, failed or was refused right after. A returned call's result is saved in the
same transaction as its mark, and a recovery run replays those results in order instead of
paying again (T40 follow-up). A recorded refusal is replayed: no later
run makes another paid call. A crash mid-call leaves the outcome unknown, and its spend
reservation stays counted (T40 criterion 4). ``DRAFT_CALL_LIMIT`` caps the calls as a
backstop. This is bounded duplicate-spend recovery, not exactly-once execution. Do not
delete or replace the sidecar file.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import struct
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from parkingbreaker.appeal.correction import MAX_AUTO_REDRAFTS
from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error

CALLS_PER_RUN = 1 + MAX_AUTO_REDRAFTS
DRAFT_RUN_LIMIT = 2
DRAFT_CALL_LIMIT = DRAFT_RUN_LIMIT * CALLS_PER_RUN

_SLOTS_DDL = """CREATE TABLE pb_draft_slots (
    case_token TEXT PRIMARY KEY,
    slot INTEGER NOT NULL UNIQUE CHECK (slot >= 0)
)"""

_ATTEMPTS_DDL = """CREATE TABLE pb_draft_attempts (
    case_token TEXT NOT NULL,
    generation INTEGER NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    inputs_sha256 TEXT NOT NULL CHECK (length(inputs_sha256) = 64),
    started_at TEXT NOT NULL,
    outcome TEXT CHECK (outcome IN ('returned', 'failed', 'refused')),
    result_json TEXT CHECK (result_json IS NULL OR outcome IS 'returned'),
    PRIMARY KEY (case_token, generation, attempt)
)"""

_RUNS_DDL = """CREATE TABLE pb_draft_runs (
    case_token TEXT NOT NULL,
    generation INTEGER NOT NULL,
    run INTEGER NOT NULL CHECK (run >= 1),
    started_at TEXT NOT NULL,
    PRIMARY KEY (case_token, generation, run)
)"""

_V2_ATTEMPTS_DDL = _ATTEMPTS_DDL.replace(
    "    result_json TEXT CHECK (result_json IS NULL OR outcome IS 'returned'),\n", ""
)
_V1_ATTEMPTS_DDL = _V2_ATTEMPTS_DDL.replace(
    "'returned', 'failed', 'refused'", "'returned', 'failed'"
)

# struct flock on Linux: l_type, l_whence (short); l_start, l_len (off_t); l_pid (pid_t).
_FLOCK = "@hhqqi4x"


class DraftOutcome(StrEnum):
    RETURNED = "returned"
    FAILED = "failed"
    # The provider refused, or the local budget stopped the call before any request.
    REFUSED = "refused"


class DraftRunRefused(StrEnum):
    """Why no new drafting run may start; no paid call is made in either case."""

    REFUSED = "refused"  # an earlier call was refused; its stop was not yet applied
    EXHAUSTED = "exhausted"  # the normal run and the one recovery run are used


class DraftCallsExhausted(Exception):
    """The case's drafting-call allowance is used up; no provider request was sent."""


class _Connected(Protocol):
    path: Path

    def _require_open(self) -> sqlite3.Connection: ...


def _check_table(database: sqlite3.Connection, name: str, ddl: str) -> None:
    row = database.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    if row is None or " ".join(row[0].upper().split()) != " ".join(ddl.upper().split()):
        raise StorageCorruption("malformed draft ownership schema")


def initialize_draft_ownership(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='draft_ownership'"
        ).fetchone()
        if version is None:
            for name in ("pb_draft_slots", "pb_draft_attempts", "pb_draft_runs"):
                if database.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
                ).fetchone():
                    raise StorageCorruption("unversioned draft ownership schema")
            database.execute(_SLOTS_DDL)
            database.execute(_ATTEMPTS_DDL)
            database.execute(_RUNS_DDL)
            database.execute("INSERT INTO pb_store_versions VALUES ('draft_ownership', 3)")
            version = (3,)
        if version == (1,):
            # v1 -> v2: add runs and the refused outcome, keeping every recorded call.
            _check_table(database, "pb_draft_attempts", _V1_ATTEMPTS_DDL)
            database.execute("DROP TRIGGER IF EXISTS pb_draft_attempts_no_delete")
            database.execute("DROP TRIGGER IF EXISTS pb_draft_attempts_finish_once")
            database.execute("ALTER TABLE pb_draft_attempts RENAME TO pb_draft_attempts_v1")
            database.execute(_ATTEMPTS_DDL)
            database.execute(
                "INSERT INTO pb_draft_attempts SELECT *, NULL FROM pb_draft_attempts_v1"
            )
            database.execute("DROP TABLE pb_draft_attempts_v1")
            database.execute(_RUNS_DDL)
            database.execute(
                "UPDATE pb_store_versions SET version=3 WHERE component='draft_ownership'"
            )
        elif version == (2,):
            # v2 -> v3: keep a returned call's result with its mark; earlier rows have none.
            _check_table(database, "pb_draft_attempts", _V2_ATTEMPTS_DDL)
            database.execute("DROP TRIGGER IF EXISTS pb_draft_attempts_no_delete")
            database.execute("DROP TRIGGER IF EXISTS pb_draft_attempts_finish_once")
            database.execute("ALTER TABLE pb_draft_attempts RENAME TO pb_draft_attempts_v2")
            database.execute(_ATTEMPTS_DDL)
            database.execute(
                "INSERT INTO pb_draft_attempts SELECT *, NULL FROM pb_draft_attempts_v2"
            )
            database.execute("DROP TABLE pb_draft_attempts_v2")
            database.execute(
                "UPDATE pb_store_versions SET version=3 WHERE component='draft_ownership'"
            )
        elif version != (3,):
            raise StorageCorruption("malformed draft ownership schema")
        _check_table(database, "pb_draft_slots", _SLOTS_DDL)
        _check_table(database, "pb_draft_attempts", _ATTEMPTS_DDL)
        _check_table(database, "pb_draft_runs", _RUNS_DDL)
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_draft_slots_fixed BEFORE UPDATE ON pb_draft_slots "
            "BEGIN SELECT RAISE(ABORT, 'draft slots are fixed'); END"
        )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_draft_slots_no_delete "
            "BEFORE DELETE ON pb_draft_slots "
            "BEGIN SELECT RAISE(ABORT, 'draft slots are fixed'); END"
        )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_draft_attempts_no_delete "
            "BEFORE DELETE ON pb_draft_attempts "
            "BEGIN SELECT RAISE(ABORT, 'draft attempts are append-only'); END"
        )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_draft_attempts_finish_once "
            "BEFORE UPDATE ON pb_draft_attempts "
            "WHEN OLD.outcome IS NOT NULL OR NEW.started_at IS NOT OLD.started_at "
            "BEGIN SELECT RAISE(ABORT, 'draft attempts finish once'); END"
        )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_draft_runs_fixed BEFORE UPDATE ON pb_draft_runs "
            "BEGIN SELECT RAISE(ABORT, 'draft runs are append-only'); END"
        )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_draft_runs_no_delete BEFORE DELETE ON pb_draft_runs "
            "BEGIN SELECT RAISE(ABORT, 'draft runs are append-only'); END"
        )
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def drafting_owners(database: Path, slots: Iterable[tuple[str, int]]) -> set[str]:
    """Cases whose per-case lock is held right now, probed without taking any lock.

    For the read-only health monitor (T24/T74): a case being researched or drafted is not stuck.
    """
    sidecar = database.with_name(database.name + ".draft-owner")
    if not sidecar.exists():
        return set()
    owners: set[str] = set()
    descriptor = os.open(sidecar, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for token, slot in slots:
            probe = struct.pack(_FLOCK, fcntl.F_WRLCK, os.SEEK_SET, slot, 1, 0)
            answer = fcntl.fcntl(descriptor, fcntl.F_OFD_GETLK, probe)
            if struct.unpack(_FLOCK, answer)[0] != fcntl.F_UNLCK:
                owners.add(token)
    finally:
        os.close(descriptor)
    return owners


class DraftOwnershipStoreMixin:
    def _draft_slot(self: _Connected, case_token: str) -> int:
        database = self._require_open()
        if database.in_transaction:
            raise StorageCorruption("draft ownership needs its own transaction")
        database.execute("BEGIN IMMEDIATE")
        try:
            row = database.execute(
                "SELECT slot FROM pb_draft_slots WHERE case_token=?", (case_token,)
            ).fetchone()
            if row is None:
                (slot,) = database.execute(
                    "SELECT COALESCE(MAX(slot), -1) + 1 FROM pb_draft_slots"
                ).fetchone()
                database.execute("INSERT INTO pb_draft_slots VALUES (?, ?)", (case_token, slot))
            else:
                (slot,) = row
            database.execute("COMMIT")
            return int(slot)
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    @contextmanager
    def draft_ownership(self: _Connected, case_token: str) -> Iterator[bool]:
        """Yield True while this caller owns the case's drafting, False if another does."""
        slot = DraftOwnershipStoreMixin._draft_slot(self, case_token)
        sidecar = self.path.with_name(self.path.name + ".draft-owner")
        descriptor = os.open(sidecar, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            request = struct.pack(_FLOCK, fcntl.F_WRLCK, os.SEEK_SET, slot, 1, 0)
            try:
                fcntl.fcntl(descriptor, fcntl.F_OFD_SETLK, request)
                owned = True
            except (BlockingIOError, PermissionError):
                # EAGAIN or EACCES: another open file description holds this slot.
                owned = False
            yield owned
        finally:
            os.close(descriptor)

    def begin_draft_run(
        self: _Connected, case_token: str, generation: int, at: datetime
    ) -> int | DraftRunRefused:
        """Record a new drafting run before its first call, or say why none may start."""
        database = self._require_open()
        if database.in_transaction:
            raise StorageCorruption("draft runs need their own transaction")
        database.execute("BEGIN IMMEDIATE")
        try:
            refused = database.execute(
                "SELECT 1 FROM pb_draft_attempts WHERE case_token=? AND generation=? "
                "AND outcome='refused'",
                (case_token, generation),
            ).fetchone()
            (used,) = database.execute(
                "SELECT COUNT(*) FROM pb_draft_runs WHERE case_token=? AND generation=?",
                (case_token, generation),
            ).fetchone()
            if refused is not None or used >= DRAFT_RUN_LIMIT:
                database.execute("COMMIT")
                return DraftRunRefused.REFUSED if refused else DraftRunRefused.EXHAUSTED
            run = int(used) + 1
            database.execute(
                "INSERT INTO pb_draft_runs VALUES (?, ?, ?, ?)",
                (case_token, generation, run, at.isoformat()),
            )
            database.execute("COMMIT")
            return run
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def returned_draft(
        self: _Connected, case_token: str, generation: int, inputs_sha256: str, skip: int = 0
    ) -> str | None:
        """A saved result of an earlier returned call with exactly these inputs, if any.

        ``skip`` passes over that many earlier such calls, oldest first.
        """
        row = (
            self._require_open()
            .execute(
                "SELECT result_json FROM pb_draft_attempts WHERE case_token=? AND generation=? "
                "AND inputs_sha256=? AND outcome='returned' AND result_json IS NOT NULL "
                "ORDER BY attempt LIMIT 1 OFFSET ?",
                (case_token, generation, inputs_sha256, skip),
            )
            .fetchone()
        )
        return None if row is None else str(row[0])

    def begin_draft_attempt(
        self: _Connected, case_token: str, generation: int, inputs_sha256: str, at: datetime
    ) -> int | None:
        """Record the next paid call before it is made; None once the allowance is used up."""
        database = self._require_open()
        if database.in_transaction:
            raise StorageCorruption("draft attempts need their own transaction")
        database.execute("BEGIN IMMEDIATE")
        try:
            (used,) = database.execute(
                "SELECT COUNT(*) FROM pb_draft_attempts WHERE case_token=? AND generation=?",
                (case_token, generation),
            ).fetchone()
            if used >= DRAFT_CALL_LIMIT:
                database.execute("COMMIT")
                return None
            attempt = int(used) + 1
            database.execute(
                "INSERT INTO pb_draft_attempts VALUES (?, ?, ?, ?, ?, NULL, NULL)",
                (case_token, generation, attempt, inputs_sha256, at.isoformat()),
            )
            database.execute("COMMIT")
            return attempt
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def finish_draft_attempt(
        self: _Connected,
        case_token: str,
        generation: int,
        attempt: int,
        inputs_sha256: str,
        outcome: DraftOutcome,
        result_json: str | None = None,
    ) -> None:
        """Record once whether the call returned, only for the inputs it was started with.

        A returned call's result is written in the same statement as its mark, so a crash
        can never leave a returned mark without the result it paid for.
        """
        if result_json is not None and outcome is not DraftOutcome.RETURNED:
            raise ValueError("only a returned call has a result")
        database = self._require_open()
        if database.in_transaction:
            raise StorageCorruption("draft attempts need their own transaction")
        database.execute("BEGIN IMMEDIATE")
        try:
            database.execute(
                "UPDATE pb_draft_attempts SET outcome=?, result_json=? "
                "WHERE case_token=? AND generation=? AND attempt=? AND inputs_sha256=? "
                "AND outcome IS NULL",
                (outcome.value, result_json, case_token, generation, attempt, inputs_sha256),
            )
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
