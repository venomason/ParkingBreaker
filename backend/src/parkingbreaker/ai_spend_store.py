"""Durable per-appeal AI spending caps, one per worker, on the transaction connection (T40).

Before every model call the gateway reserves that call's ceiling, never more than what
is left of the appeal's allowance for that worker, and passes it to the provider as the
call's hard ceiling. After the call the reservation is settled to the reported cost (an
overrun is recorded as reported, so it stays visible). A call whose cost is unknown (the
provider failed or the process died mid-call) stays charged at its full reservation.
Reservations are taken inside one immediate transaction, so concurrent calls for the same
appeal and worker can together never reserve more than the cap. Rows are append-only
apart from the one-time settlement of their own cost.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Protocol

from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.oplog.ai_journal import AiWorker

_DDL = """CREATE TABLE pb_ai_spend (
    subject_token TEXT NOT NULL,
    worker TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    reserved_cents INTEGER NOT NULL CHECK (reserved_cents >= 1),
    settled_cents INTEGER CHECK (settled_cents >= 0),
    reserved_at TEXT NOT NULL,
    PRIMARY KEY (subject_token, worker, seq)
)"""

# Proposed 2026-11-03, pending board review: the whole-appeal allowance for each worker.
# Research allows a few runs at its 50-cent per-run budget; intake reading and drafting
# allow two calls each. A cap hit stops that worker for this appeal and is logged.
WORKER_CAPS_CENTS: dict[AiWorker, int] = {
    AiWorker.INTAKE_READING: 50,
    AiWorker.RESEARCH: 150,
    AiWorker.DRAFTING: 50,
}


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


def initialize_ai_spend(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='ai_spend'"
        ).fetchone()
        row = database.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='pb_ai_spend'"
        ).fetchone()
        if version is None:
            if row is not None:
                raise StorageCorruption("unversioned AI spend schema")
            database.execute(_DDL)
            database.execute("INSERT INTO pb_store_versions VALUES ('ai_spend', 1)")
        elif (
            version != (1,)
            or row is None
            or " ".join(row[0].upper().split()) != " ".join(_DDL.upper().split())
        ):
            raise StorageCorruption("malformed AI spend schema")
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_ai_spend_no_delete BEFORE DELETE ON pb_ai_spend "
            "BEGIN SELECT RAISE(ABORT, 'AI spend is append-only'); END"
        )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_ai_spend_settle_once BEFORE UPDATE ON pb_ai_spend "
            "WHEN OLD.settled_cents IS NOT NULL "
            "OR NEW.reserved_cents IS NOT OLD.reserved_cents "
            "BEGIN SELECT RAISE(ABORT, 'AI spend settles once'); END"
        )
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


class AiSpendStoreMixin:
    def ai_spent_cents(self: _Connected, subject: str, worker: AiWorker) -> int:
        """Settled costs plus every unsettled reservation, counted in full."""
        (spent,) = (
            self._require_open()
            .execute(
                "SELECT COALESCE(SUM(COALESCE(settled_cents, reserved_cents)), 0) "
                "FROM pb_ai_spend WHERE subject_token=? AND worker=?",
                (subject, worker.value),
            )
            .fetchone()
        )
        return int(spent)

    def reserve_ai_spend(
        self: _Connected,
        subject: str,
        worker: AiWorker,
        cap_cents: int,
        at: datetime,
        ceiling_cents: int | None = None,
    ) -> tuple[int, int] | None:
        """Reserve one call's ceiling within what is left; (seq, cents), or None if none is."""
        if type(cap_cents) is not int or cap_cents < 0:
            raise ValueError("cap must be a non-negative integer")
        if ceiling_cents is not None and (type(ceiling_cents) is not int or ceiling_cents < 0):
            raise ValueError("ceiling must be a non-negative integer")
        database = self._require_open()
        if database.in_transaction:
            raise StorageCorruption("AI spend needs its own transaction")
        database.execute("BEGIN IMMEDIATE")
        try:
            spent = AiSpendStoreMixin.ai_spent_cents(self, subject, worker)
            left = cap_cents - spent
            if ceiling_cents is not None:
                left = min(left, ceiling_cents)
            if left <= 0:
                database.execute("COMMIT")
                return None
            (seq,) = database.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM pb_ai_spend "
                "WHERE subject_token=? AND worker=?",
                (subject, worker.value),
            ).fetchone()
            database.execute(
                "INSERT INTO pb_ai_spend VALUES (?, ?, ?, ?, NULL, ?)",
                (subject, worker.value, seq, left, at.isoformat()),
            )
            database.execute("COMMIT")
            return int(seq), left
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def settle_ai_spend(
        self: _Connected, subject: str, worker: AiWorker, seq: int, cost_cents: int
    ) -> None:
        """Record a call's reported cost once, as reported."""
        database = self._require_open()
        if database.in_transaction:
            raise StorageCorruption("AI spend needs its own transaction")
        database.execute("BEGIN IMMEDIATE")
        try:
            database.execute(
                "UPDATE pb_ai_spend SET settled_cents=? "
                "WHERE subject_token=? AND worker=? AND seq=?",
                (cost_cents, subject, worker.value, seq),
            )
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
