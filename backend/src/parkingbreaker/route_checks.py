"""Freshness of successful official-page checks, isolated per immutable route revision."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Protocol

from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.route_registry import RouteRevision, RouteScope

# Four missed quarter-hour checks exhaust the independent dispatch freshness window.
MAX_ROUTE_CHECK_AGE = timedelta(hours=1)
_DDL = """CREATE TABLE pb_municipal_route_checks (
    issuer TEXT NOT NULL,
    stage TEXT NOT NULL,
    mail_class TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    checked_at TEXT NOT NULL,
    invalidation TEXT,
    PRIMARY KEY (issuer, stage, mail_class, revision),
    FOREIGN KEY (issuer, stage, mail_class, revision)
        REFERENCES pb_municipal_route_revisions(issuer, stage, mail_class, revision)
)"""


def initialize_route_checks(db: sqlite3.Connection) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        version = db.execute(
            "SELECT version FROM pb_store_versions WHERE component='municipal_route_checks'"
        ).fetchone()
        schema = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='pb_municipal_route_checks'"
        ).fetchone()
        if version is None and schema is None:
            db.execute(_DDL)
            db.execute("INSERT INTO pb_store_versions VALUES ('municipal_route_checks', 1)")
        elif version != (1,) or schema != (_DDL,):
            raise StorageCorruption("invalid municipal route check schema")
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            rollback_preserving_error(db)
        raise


class _RouteReader(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...
    def current_route(self, scope: RouteScope) -> RouteRevision | None: ...


class RouteCheckStoreMixin:
    def confirm_route_check(
        self: _RouteReader, scope: RouteScope, revision: int, checked_at: datetime
    ) -> bool:
        """Record a successful comparison only while its original authorization is current."""
        if checked_at.utcoffset() is None:
            raise ValueError("route check time must be timezone-aware")
        db = self._require_open()
        db.execute("BEGIN IMMEDIATE")
        try:
            route = self.current_route(scope)
            current = (
                route is not None and route.revision == revision and route.verified_at(checked_at)
            )
            if current:
                updated = db.execute(
                    "INSERT INTO pb_municipal_route_checks VALUES (?, ?, ?, ?, ?, NULL) "
                    "ON CONFLICT(issuer, stage, mail_class, revision) DO UPDATE SET "
                    "checked_at = MAX(checked_at, excluded.checked_at) WHERE invalidation IS NULL",
                    (
                        scope.issuer,
                        scope.stage,
                        scope.mail_class,
                        revision,
                        checked_at.astimezone(UTC).isoformat(timespec="microseconds"),
                    ),
                )
                current = updated.rowcount == 1
            db.execute("COMMIT")
            return current
        except BaseException:
            if db.in_transaction:
                rollback_preserving_error(db)
            raise

    def invalidate_route_check(
        self: _RouteReader, route: RouteRevision, reason: str, now: datetime
    ) -> None:
        """Fence this exact revision even while its formal suspension waits for a mail call."""
        if not reason or now.utcoffset() is None:
            raise ValueError("a dated official route change is required")
        scope = route.scope
        self._require_open().execute(
            "INSERT INTO pb_municipal_route_checks VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(issuer, stage, mail_class, revision) DO UPDATE SET "
            "invalidation = COALESCE(invalidation, excluded.invalidation)",
            (
                scope.issuer,
                scope.stage,
                scope.mail_class,
                route.revision,
                now.astimezone(UTC).isoformat(timespec="microseconds"),
                reason,
            ),
        )

    def route_invalidation(self: _RouteReader, route: RouteRevision) -> str | None:
        scope = route.scope
        row = (
            self._require_open()
            .execute(
                "SELECT invalidation FROM pb_municipal_route_checks WHERE "
                "issuer=? AND stage=? AND mail_class=? AND revision=?",
                (scope.issuer, scope.stage, scope.mail_class, route.revision),
            )
            .fetchone()
        )
        return None if row is None or row[0] is None else str(row[0])

    def route_check_current(self: _RouteReader, route: RouteRevision, now: datetime) -> bool:
        if not route.verified_at(now) or self.current_route(route.scope) != route:
            return False
        scope = route.scope
        row = (
            self._require_open()
            .execute(
                "SELECT checked_at, invalidation FROM pb_municipal_route_checks "
                "WHERE issuer=? AND stage=? AND mail_class=? AND revision=?",
                (scope.issuer, scope.stage, scope.mail_class, route.revision),
            )
            .fetchone()
        )
        if row is not None and row[1] is not None:
            return False
        try:
            observed = route.changed_at if row is None else datetime.fromisoformat(row[0])
            return route.changed_at <= observed <= now and now - observed < MAX_ROUTE_CHECK_AGE
        except (TypeError, ValueError, OverflowError) as exc:
            raise StorageCorruption("invalid municipal route check time") from exc
