"""Opaque visitor price assignments, separate from private appeal access.

Assignments persist for the experiment. A browser credential recognizes an assignment;
its possession grants no session, case access, or authority to choose a price. Existing
intake offers and case amounts remain authoritative and are never rewritten here.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.prices import PRICE_ARMS, PRICE_EXPERIMENT_ID, assigned_price, price_arm

_DDL = (
    """CREATE TABLE pb_price_visitors (
        visitor_id TEXT PRIMARY KEY,
        experiment_id TEXT NOT NULL,
        arm TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE pb_price_credentials (
        token_digest TEXT PRIMARY KEY,
        visitor_id TEXT NOT NULL REFERENCES pb_price_visitors(visitor_id)
    )""",
    """CREATE TABLE pb_price_scopes (
        scope_id TEXT PRIMARY KEY REFERENCES pb_identity_accounts(customer_id),
        visitor_id TEXT NOT NULL REFERENCES pb_price_visitors(visitor_id)
    )""",
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SELECT = "SELECT v.visitor_id, v.experiment_id, v.arm, v.amount_cents, v.created_at "


@dataclass(frozen=True)
class PricingAssignment:
    visitor_id: str
    experiment_id: str
    arm: str
    amount_cents: int
    created_at: datetime


def _digest(value: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("invalid pricing credential")
    return value


def _assignment(row: tuple[object, ...] | None) -> PricingAssignment | None:
    if row is None:
        return None
    visitor, experiment, arm, cents, stamp = row
    try:
        if (
            not isinstance(visitor, str)
            or not visitor
            or not isinstance(experiment, str)
            or not isinstance(arm, str)
            or type(cents) is not int
            or PRICE_ARMS.get(arm) != cents
            or not isinstance(stamp, str)
        ):
            raise ValueError("invalid assignment")
        created = datetime.fromisoformat(stamp)
        if created.tzinfo is None or created.utcoffset() is None:
            raise ValueError("invalid assignment time")
        return PricingAssignment(visitor, experiment, arm, cents, created)
    except (ValueError, TypeError):
        raise StorageCorruption("malformed pricing assignment") from None


def initialize_pricing(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='pricing'"
        ).fetchone()
        expected = {sql.split()[2]: " ".join(sql.upper().split()) for sql in _DDL}
        actual = {
            name: " ".join(sql.upper().split())
            for name, sql in database.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table'"
            )
            if name in expected
        }
        if version is None:
            if actual:
                raise StorageCorruption("unversioned pricing schema")
            for sql in _DDL:
                database.execute(sql)
            database.execute("INSERT INTO pb_store_versions VALUES ('pricing', 1)")
        elif version != (1,) or actual != expected:
            raise StorageCorruption("malformed pricing schema")
        for table in expected:
            for action in ("UPDATE", "DELETE"):
                database.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                    f"BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'pricing assignments are append-only'); END"
                )
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def bind_scope_pricing(database: sqlite3.Connection, scope_id: str, digest: str) -> None:
    """Called inside the same transaction that creates a new private appeal scope."""
    if not database.in_transaction:
        raise ValueError("pricing binding requires a transaction")
    assignment = _assignment(
        database.execute(
            _SELECT + "FROM pb_price_credentials c JOIN pb_price_visitors v USING(visitor_id) "
            "WHERE c.token_digest=?",
            (_digest(digest),),
        ).fetchone()
    )
    if assignment is None or assignment.experiment_id != PRICE_EXPERIMENT_ID:
        raise ValueError("pricing assignment unavailable")
    database.execute("INSERT INTO pb_price_scopes VALUES (?, ?)", (scope_id, assignment.visitor_id))


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


class PricingStoreMixin:
    def pricing_assignment(self: _Connected, digest: str) -> PricingAssignment | None:
        return _assignment(
            self._require_open()
            .execute(
                _SELECT + "FROM pb_price_credentials c JOIN pb_price_visitors v USING(visitor_id) "
                "WHERE c.token_digest=?",
                (_digest(digest),),
            )
            .fetchone()
        )

    def pricing_for_scope(self: _Connected, scope_id: str) -> PricingAssignment | None:
        return _assignment(
            self._require_open()
            .execute(
                _SELECT + "FROM pb_price_scopes s JOIN pb_price_visitors v USING(visitor_id) "
                "WHERE s.scope_id=?",
                (scope_id,),
            )
            .fetchone()
        )

    def create_pricing_assignment(
        self: _Connected, digest: str, *, now: datetime
    ) -> PricingAssignment:
        digest = _digest(digest)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("pricing time must be timezone-aware")
        database = self._require_open()
        database.execute("BEGIN IMMEDIATE")
        try:
            existing = PricingStoreMixin.pricing_assignment(self, digest)
            if existing is not None:
                if existing.experiment_id != PRICE_EXPERIMENT_ID:
                    raise ValueError("pricing credential belongs to another experiment")
                database.execute("COMMIT")
                return existing
            visitor = uuid4().hex
            cents = assigned_price(visitor)
            assignment = PricingAssignment(
                visitor, PRICE_EXPERIMENT_ID, price_arm(cents), cents, now.astimezone(UTC)
            )
            database.execute(
                "INSERT INTO pb_price_visitors VALUES (?, ?, ?, ?, ?)",
                (
                    visitor,
                    assignment.experiment_id,
                    assignment.arm,
                    cents,
                    assignment.created_at.isoformat(),
                ),
            )
            database.execute("INSERT INTO pb_price_credentials VALUES (?, ?)", (digest, visitor))
            database.execute("COMMIT")
            return assignment
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def issue_pricing_credential(
        self: _Connected, visitor_id: str, digest: str
    ) -> PricingAssignment:
        """Reissue recognition after authenticated recovery; does not authorize appeal access."""
        digest = _digest(digest)
        database = self._require_open()
        database.execute("BEGIN IMMEDIATE")
        try:
            assignment = _assignment(
                database.execute(
                    _SELECT + "FROM pb_price_visitors v WHERE visitor_id=?",
                    (visitor_id,),
                ).fetchone()
            )
            if assignment is None or assignment.experiment_id != PRICE_EXPERIMENT_ID:
                raise ValueError("pricing assignment unavailable")
            existing = PricingStoreMixin.pricing_assignment(self, digest)
            if existing is None:
                database.execute(
                    "INSERT INTO pb_price_credentials VALUES (?, ?)", (digest, visitor_id)
                )
            elif existing.visitor_id != visitor_id:
                raise ValueError("pricing credential already bound")
            database.execute("COMMIT")
            return assignment
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
