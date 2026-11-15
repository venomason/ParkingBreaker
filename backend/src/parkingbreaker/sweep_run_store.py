"""Durable record of every scheduled sweep, for health monitoring (T24).

A run row is written before the sweep does any work and finished in cleanup. A run that
never finishes (the process died or hung) stays unfinished and is reported as interrupted;
a failed or partial run never counts as the last successful sweep. Rows hold counts and a
fixed outcome only: no case ids, customer data or error text.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error

_DDL = """CREATE TABLE pb_sweep_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT CHECK (outcome IN ('ok', 'partial', 'failed')),
    processed INTEGER CHECK (processed >= 0),
    failed INTEGER CHECK (failed >= 0),
    CHECK ((finished_at IS NULL) = (outcome IS NULL))
)"""


class SweepOutcome(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class SweepRunRecorder(Protocol):
    def start_sweep_run(self, at: datetime) -> int: ...

    def finish_sweep_run(
        self, run_id: int, at: datetime, outcome: SweepOutcome, processed: int, failed: int
    ) -> None: ...


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


def initialize_sweep_runs(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='sweep_runs'"
        ).fetchone()
        row = database.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='pb_sweep_runs'"
        ).fetchone()
        if version is None:
            if row is not None:
                raise StorageCorruption("unversioned sweep run schema")
            database.execute(_DDL)
            database.execute("INSERT INTO pb_store_versions VALUES ('sweep_runs', 1)")
        elif (
            version != (1,)
            or row is None
            or " ".join(row[0].upper().split()) != " ".join(_DDL.upper().split())
        ):
            raise StorageCorruption("malformed sweep run schema")
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_sweep_runs_no_delete BEFORE DELETE ON pb_sweep_runs "
            "BEGIN SELECT RAISE(ABORT, 'sweep runs are append-only'); END"
        )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_sweep_runs_finish_once "
            "BEFORE UPDATE ON pb_sweep_runs "
            "WHEN OLD.finished_at IS NOT NULL OR NEW.started_at IS NOT OLD.started_at "
            "BEGIN SELECT RAISE(ABORT, 'sweep runs finish once'); END"
        )
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


class SweepRunStoreMixin:
    def start_sweep_run(self: _Connected, at: datetime) -> int:
        database = self._require_open()
        if database.in_transaction:
            raise StorageCorruption("sweep runs need their own transaction")
        database.execute("BEGIN IMMEDIATE")
        try:
            cursor = database.execute(
                "INSERT INTO pb_sweep_runs (started_at) VALUES (?)", (at.isoformat(),)
            )
            database.execute("COMMIT")
            assert cursor.lastrowid is not None
            return int(cursor.lastrowid)
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def finish_sweep_run(
        self: _Connected,
        run_id: int,
        at: datetime,
        outcome: SweepOutcome,
        processed: int,
        failed: int,
    ) -> None:
        database = self._require_open()
        if database.in_transaction:
            raise StorageCorruption("sweep runs need their own transaction")
        database.execute("BEGIN IMMEDIATE")
        try:
            database.execute(
                "UPDATE pb_sweep_runs SET finished_at=?, outcome=?, processed=?, failed=? "
                "WHERE run_id=? AND finished_at IS NULL",
                (at.isoformat(), outcome.value, processed, failed, run_id),
            )
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
