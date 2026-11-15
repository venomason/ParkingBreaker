"""Owner resolutions of review items, append-only, on the transaction store's connection.

Open items are derived from recorded facts on every read (review_queue); this table only
says which item ids the owner has resolved. A resolution is written once and never changed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from parkingbreaker.disputes import DisputeFact
from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.oplog.appeal_events import appeal_events
from parkingbreaker.review_queue import (
    Resolution,
    ReviewAuthority,
    ReviewItem,
    ReviewKind,
    can_resolve,
    review_items,
)
from parkingbreaker.transaction_records import CaseRecord, TransitionRecord

_DDL = """CREATE TABLE pb_review_resolutions (
    item_id TEXT PRIMARY KEY,
    resolution TEXT NOT NULL,
    resolved_by TEXT NOT NULL,
    resolved_at TEXT NOT NULL
)"""

_EXPIRY_DDL = """CREATE TABLE pb_review_resolution_expiries (
    item_id TEXT PRIMARY KEY REFERENCES pb_review_resolutions(item_id),
    expires_at TEXT NOT NULL
)"""


@dataclass(frozen=True)
class ReviewDecision:
    """Immutable informational acknowledgement, never an action authorization."""

    item_id: str
    resolution: Resolution
    actor: str
    resolved_at: datetime
    expires_at: datetime | None

    @property
    def authority(self) -> ReviewAuthority:
        """An acknowledgement, including a legacy one, never authorizes an action."""
        return ReviewAuthority.INFORMATIONAL


_CANDIDATES = (
    "SELECT case_token FROM pb_webhooks WHERE provider='stripe' AND payload LIKE '{\"kind\":%' "
    "UNION SELECT case_token FROM pb_case_history WHERE machine='payment' "
    "AND target='WRITTEN_OFF' ORDER BY 1"
)


class _Store(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...
    def get_case(self, case_id: str) -> CaseRecord | None: ...
    def transition_history(self, case_id: str) -> tuple[TransitionRecord, ...]: ...
    def dispute_facts(self, case_id: str) -> tuple[DisputeFact, ...]: ...


def initialize_review(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='review'"
        ).fetchone()
        row = database.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='pb_review_resolutions'"
        ).fetchone()
        if version is None:
            if row is not None:
                raise StorageCorruption("unversioned review schema")
            database.execute(_DDL)
            database.execute("INSERT INTO pb_store_versions VALUES ('review', 1)")
        elif (
            version != (1,)
            or row is None
            or " ".join(row[0].upper().split()) != " ".join(_DDL.upper().split())
        ):
            raise StorageCorruption("malformed review schema")
        from parkingbreaker.settings_store import initialize_expiry_sidecar

        initialize_expiry_sidecar(
            database, "review_expiry", "pb_review_resolution_expiries", _EXPIRY_DDL
        )
        for action in ("UPDATE", "DELETE"):
            database.execute(
                f"CREATE TRIGGER IF NOT EXISTS pb_review_resolutions_no_{action.lower()} "
                f"BEFORE {action} ON pb_review_resolutions "
                "BEGIN SELECT RAISE(ABORT, 'review resolutions are append-only'); END"
            )
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


class ReviewStoreMixin:
    def review_queue(self: _Store) -> tuple[ReviewItem, ...]:
        """Every review item, resolved or not, derived from recorded facts."""
        items: list[ReviewItem] = []
        for (token,) in self._require_open().execute(_CANDIDATES).fetchall():
            case = self.get_case(token)
            if case is None:
                raise StorageCorruption("review fact without a case")
            events = appeal_events(case.authorized_at, self.transition_history(token))
            items.extend(review_items(token, events, self.dispute_facts(token)))
        # Pending setting proposals wait here; the owner answers them with
        # decide_setting_proposal, and an answered proposal leaves the queue.
        items.extend(
            ReviewItem(
                f"setting:{proposal_id}",
                ReviewKind.SETTING_CHANGE,
                "",
                expires_at=datetime.fromisoformat(expiry) if expiry else None,
            )
            for (proposal_id, expiry) in self._require_open().execute(
                "SELECT p.proposal_id, e.expires_at FROM pb_setting_proposals p "
                "LEFT JOIN pb_setting_proposal_expiries e ON e.proposal_id=p.proposal_id "
                "LEFT JOIN pb_setting_proposal_outcomes o ON o.proposal_id=p.proposal_id "
                "WHERE o.proposal_id IS NULL ORDER BY p.proposed_at, p.proposal_id"
            )
        )
        return tuple(items)

    def open_review_items(self: _Store) -> tuple[ReviewItem, ...]:
        resolved = {
            row[0]
            for row in self._require_open().execute("SELECT item_id FROM pb_review_resolutions")
        }
        return tuple(
            item for item in ReviewStoreMixin.review_queue(self) if item.item_id not in resolved
        )

    def review_decisions(self: _Store) -> tuple[ReviewDecision, ...]:
        """Read recorded informational acknowledgements, including legacy null expiry."""
        return tuple(
            ReviewDecision(
                r[0],
                Resolution(r[1]),
                r[2],
                datetime.fromisoformat(r[3]),
                datetime.fromisoformat(r[4]) if r[4] else None,
            )
            for r in self._require_open().execute(
                "SELECT r.item_id, r.resolution, r.resolved_by, r.resolved_at, e.expires_at "
                "FROM pb_review_resolutions r LEFT JOIN pb_review_resolution_expiries e "
                "ON e.item_id=r.item_id ORDER BY r.item_id"
            )
        )

    def resolve_review_item(
        self: _Store,
        item_id: str,
        resolution: Resolution,
        actor: str,
        at: datetime,
        *,
        expires_at: datetime | None = None,
    ) -> bool:
        """Record the owner's resolution once; False if it was already resolved."""
        if not can_resolve(actor):
            raise PermissionError("only the owner resolves review items")
        if type(resolution) is not Resolution:
            raise ValueError("resolution must be a Resolution")
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("resolution time must be timezone-aware")
        if expires_at is not None and (
            expires_at.tzinfo is None or expires_at.utcoffset() is None or expires_at <= at
        ):
            raise ValueError("expiry must be timezone-aware and after resolution")
        if item_id.startswith("setting:"):
            raise ValueError("answer a setting change with decide_setting_proposal")
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            if item_id not in {item.item_id for item in ReviewStoreMixin.review_queue(self)}:
                raise ValueError("unknown review item")
            cursor = database.execute(
                "INSERT OR IGNORE INTO pb_review_resolutions VALUES (?, ?, ?, ?)",
                (item_id, str(resolution), actor, at.isoformat()),
            )
            if cursor.rowcount == 1 and expires_at is not None:
                database.execute(
                    "INSERT INTO pb_review_resolution_expiries VALUES (?, ?)",
                    (item_id, expires_at.isoformat()),
                )
            database.execute("COMMIT")
            return cursor.rowcount == 1
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
