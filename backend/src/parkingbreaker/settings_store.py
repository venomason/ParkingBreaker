"""Learned-settings history and pending proposals, append-only, on the transaction store.

A setting's current value is its proposed start until the first recorded change, then the
new value of its latest change. Every write goes through settings_registry.decide: a step
toward safety is recorded at once with approver "automatic"; a step toward risk or spend
becomes a pending proposal that appears in the owner review queue and changes nothing
until the owner approves it before its explicit expiry. Legacy proposals without expiry
are audit records only and cannot be approved. Rows are never updated or deleted.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.review_queue import can_resolve
from parkingbreaker.settings_registry import SETTINGS, Current, Decision, Verdict, decide

_DDL = (
    """CREATE TABLE pb_setting_changes (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        old_value INTEGER NOT NULL,
        new_value INTEGER NOT NULL,
        proposer TEXT NOT NULL,
        approver TEXT NOT NULL,
        evidence TEXT NOT NULL,
        changed_at TEXT NOT NULL
    )""",
    """CREATE TABLE pb_setting_proposals (
        proposal_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        old_value INTEGER NOT NULL,
        new_value INTEGER NOT NULL,
        proposer TEXT NOT NULL,
        evidence TEXT NOT NULL,
        proposed_at TEXT NOT NULL
    )""",
    """CREATE TABLE pb_setting_proposal_outcomes (
        proposal_id TEXT PRIMARY KEY REFERENCES pb_setting_proposals(proposal_id),
        outcome TEXT NOT NULL CHECK (outcome IN ('APPROVED', 'REJECTED', 'STALE')),
        decided_by TEXT NOT NULL,
        decided_at TEXT NOT NULL
    )""",
)
_TABLES = ("pb_setting_changes", "pb_setting_proposals", "pb_setting_proposal_outcomes")
_EXPIRY_DDL = """CREATE TABLE pb_setting_proposal_expiries (
    proposal_id TEXT PRIMARY KEY REFERENCES pb_setting_proposals(proposal_id),
    expires_at TEXT NOT NULL
)"""
AUTOMATIC = "automatic"


@dataclass(frozen=True)
class SettingProposal:
    proposal_id: str
    name: str
    old_value: int
    new_value: int
    proposer: str
    evidence: str
    proposed_at: datetime
    expires_at: datetime | None = None


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


def initialize_settings(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='settings'"
        ).fetchone()
        tables = {
            row[0]: " ".join(row[1].upper().split())
            for row in database.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
            if row[0] in _TABLES
        }
        expected = {
            name: " ".join(sql.upper().split()) for name, sql in zip(_TABLES, _DDL, strict=True)
        }
        if version is None:
            if tables:
                raise StorageCorruption("unversioned settings schema")
            for ddl in _DDL:
                database.execute(ddl)
            database.execute("INSERT INTO pb_store_versions VALUES ('settings', 1)")
        elif version != (1,) or tables != expected:
            raise StorageCorruption("malformed settings schema")
        initialize_expiry_sidecar(
            database, "settings_expiry", "pb_setting_proposal_expiries", _EXPIRY_DDL
        )
        for table in _TABLES:
            for action in ("UPDATE", "DELETE"):
                database.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                    f"BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'settings history is append-only'); END"
                )
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def initialize_expiry_sidecar(
    database: sqlite3.Connection, component: str, table: str, ddl: str
) -> None:
    """Validate a separately versioned additive table inside the caller's transaction."""
    version = database.execute(
        "SELECT version FROM pb_store_versions WHERE component=?", (component,)
    ).fetchone()
    row = database.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if version is None:
        if row is not None:
            raise StorageCorruption("unversioned expiry schema")
        database.execute(ddl)
        database.execute("INSERT INTO pb_store_versions VALUES (?, 1)", (component,))
    elif (
        version != (1,)
        or row is None
        or " ".join(row[0].upper().split()) != " ".join(ddl.upper().split())
    ):
        raise StorageCorruption("malformed expiry schema")
    for action in ("UPDATE", "DELETE"):
        database.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
            f"BEFORE {action} ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'expiry history is append-only'); END"
        )


def _current(database: sqlite3.Connection, name: str) -> Current:
    row = database.execute(
        "SELECT new_value, changed_at FROM pb_setting_changes WHERE name=? "
        "ORDER BY seq DESC LIMIT 1",
        (name,),
    ).fetchone()
    if row is None:
        return Current(SETTINGS[name].start, None)
    return Current(row[0], datetime.fromisoformat(row[1]))


def _open(database: sqlite3.Connection, name: str | None = None) -> tuple[SettingProposal, ...]:
    rows = database.execute(
        "SELECT p.proposal_id, p.name, p.old_value, p.new_value, p.proposer, p.evidence, "
        "p.proposed_at, e.expires_at FROM pb_setting_proposals p "
        "LEFT JOIN pb_setting_proposal_expiries e ON e.proposal_id=p.proposal_id "
        "LEFT JOIN pb_setting_proposal_outcomes o ON o.proposal_id=p.proposal_id "
        "WHERE o.proposal_id IS NULL AND (? IS NULL OR p.name=?) ORDER BY p.proposed_at",
        (name, name),
    ).fetchall()
    return tuple(
        SettingProposal(
            r[0],
            r[1],
            r[2],
            r[3],
            r[4],
            r[5],
            datetime.fromisoformat(r[6]),
            datetime.fromisoformat(r[7]) if r[7] else None,
        )
        for r in rows
    )


def _check(name: str, actor: str, evidence: str, now: datetime) -> None:
    if name not in SETTINGS:
        raise ValueError("unknown setting")
    if not actor.strip() or not evidence.strip():
        raise ValueError("a change needs a named proposer and its evidence")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("change time must be timezone-aware")


class SettingsStoreMixin:
    def current_setting(self: _Connected, name: str) -> Current:
        if name not in SETTINGS:
            raise ValueError("unknown setting")
        return _current(self._require_open(), name)

    def open_setting_proposals(self: _Connected) -> tuple[SettingProposal, ...]:
        return _open(self._require_open())

    def propose_setting(
        self: _Connected,
        name: str,
        value: int,
        *,
        proposer: str,
        evidence: str,
        now: datetime,
        expires_at: datetime | None = None,
    ) -> Decision:
        """Apply a safe step, queue a risky one for the owner, or refuse; returns why."""
        _check(name, proposer, evidence, now)
        database = self._require_open()
        database.execute("BEGIN IMMEDIATE")
        try:
            current = _current(database, name)
            decision = decide(SETTINGS[name], current, value, now)
            if decision.verdict is Verdict.NEEDS_OWNER and (
                expires_at is None
                or expires_at.tzinfo is None
                or expires_at.utcoffset() is None
                or expires_at <= now
            ):
                decision = Decision(
                    Verdict.REFUSED,
                    "a risky proposal needs an explicit future timezone-aware expiry",
                )
            if decision.verdict is Verdict.NEEDS_OWNER and any(
                p.expires_at is not None and now < p.expires_at for p in _open(database, name)
            ):
                decision = Decision(Verdict.REFUSED, "a proposal for this setting is pending")
            if decision.verdict is Verdict.APPLY:
                database.execute(
                    "INSERT INTO pb_setting_changes "
                    "(name, old_value, new_value, proposer, approver, evidence, changed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        name,
                        current.value,
                        value,
                        proposer,
                        AUTOMATIC,
                        evidence,
                        now.astimezone(UTC).isoformat(),
                    ),
                )
            elif decision.verdict is Verdict.NEEDS_OWNER:
                proposal_id = uuid.uuid4().hex
                database.execute(
                    "INSERT INTO pb_setting_proposals VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        proposal_id,
                        name,
                        current.value,
                        value,
                        proposer,
                        evidence,
                        now.astimezone(UTC).isoformat(),
                    ),
                )
                assert expires_at is not None
                database.execute(
                    "INSERT INTO pb_setting_proposal_expiries VALUES (?, ?)",
                    (proposal_id, expires_at.astimezone(UTC).isoformat()),
                )
            database.execute("COMMIT")
            return decision
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def decide_setting_proposal(
        self: _Connected, proposal_id: str, *, approve: bool, actor: str, now: datetime
    ) -> str:
        """The owner's answer: APPROVED, REJECTED, or STALE if the setting moved meanwhile."""
        if not can_resolve(actor):
            raise PermissionError("only the owner approves a setting change")
        if type(approve) is not bool:
            raise ValueError("approval must be a bool")
        database = self._require_open()
        database.execute("BEGIN IMMEDIATE")
        try:
            pending = {p.proposal_id: p for p in _open(database)}
            proposal = pending.get(proposal_id)
            if proposal is None:
                raise ValueError("no such pending proposal")
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("change time must be timezone-aware")
            if now < proposal.proposed_at:
                raise ValueError("decision time precedes the proposal")
            outcome = "REJECTED"
            if approve and (
                proposal.expires_at is None
                or now >= proposal.expires_at
                or proposal.name not in SETTINGS
            ):
                outcome = "STALE"  # the setting no longer exists; it can only be closed
            elif approve:
                current = _current(database, proposal.name)
                still = decide(SETTINGS[proposal.name], current, proposal.new_value, now)
                outcome = (
                    "APPROVED"
                    if current.value == proposal.old_value and still.verdict is Verdict.NEEDS_OWNER
                    else "STALE"
                )
                if outcome == "APPROVED":
                    database.execute(
                        "INSERT INTO pb_setting_changes "
                        "(name, old_value, new_value, proposer, approver, evidence, changed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            proposal.name,
                            proposal.old_value,
                            proposal.new_value,
                            proposal.proposer,
                            actor,
                            proposal.evidence,
                            now.astimezone(UTC).isoformat(),
                        ),
                    )
            database.execute(
                "INSERT INTO pb_setting_proposal_outcomes VALUES (?, ?, ?, ?)",
                (proposal_id, outcome, actor, now.astimezone(UTC).isoformat()),
            )
            database.execute("COMMIT")
            return outcome
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
