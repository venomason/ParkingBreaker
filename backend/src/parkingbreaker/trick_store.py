"""Book of Tricks sightings per place, append-only, on the transaction store's connection.

A sighting says that a known trick appeared on one official page fetched for a place (the
resolved agency id), with the page SHA-256 and the exact sentence. The same trick on the
same page is stored once; a changed page (new SHA-256) is a new sighting, so the record
grows as places change. No customer data is stored, and nothing here changes a case.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.tricks import Sighting

_DDL = """CREATE TABLE pb_trick_sightings (
    place_key TEXT NOT NULL,
    trick_id TEXT NOT NULL,
    page_sha256 TEXT NOT NULL,
    url TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (place_key, trick_id, page_sha256)
)"""


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


def initialize_tricks(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='tricks'"
        ).fetchone()
        row = database.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='pb_trick_sightings'"
        ).fetchone()
        if version is None:
            if row is not None:
                raise StorageCorruption("unversioned trick sightings schema")
            database.execute(_DDL)
            database.execute("INSERT INTO pb_store_versions VALUES ('tricks', 1)")
        elif (
            version != (1,)
            or row is None
            or " ".join(row[0].upper().split()) != " ".join(_DDL.upper().split())
        ):
            raise StorageCorruption("malformed trick sightings schema")
        for action in ("UPDATE", "DELETE"):
            database.execute(
                f"CREATE TRIGGER IF NOT EXISTS pb_trick_sightings_no_{action.lower()} "
                f"BEFORE {action} ON pb_trick_sightings "
                "BEGIN SELECT RAISE(ABORT, 'trick sightings are append-only'); END"
            )
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


class TrickStoreMixin:
    def record_trick_sightings(
        self: _Connected, place_key: str, sightings: Iterable[Sighting], seen_at: datetime
    ) -> int:
        """Store new sightings for a place in their own transaction; returns how many were new."""
        if not place_key.strip():
            raise ValueError("place key required")
        if seen_at.tzinfo is None or seen_at.utcoffset() is None:
            raise ValueError("seen_at must be timezone-aware")
        database = self._require_open()
        if database.in_transaction:
            raise RuntimeError("trick sightings need their own transaction")
        database.execute("BEGIN IMMEDIATE")
        try:
            added = 0
            for sighting in sightings:
                cursor = database.execute(
                    "INSERT OR IGNORE INTO pb_trick_sightings VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        place_key,
                        sighting.trick_id,
                        sighting.sha256,
                        sighting.url,
                        sighting.excerpt,
                        seen_at.isoformat(),
                    ),
                )
                added += cursor.rowcount
            database.execute("COMMIT")
            return added
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def trick_sightings(self: _Connected, place_key: str) -> tuple[Sighting, ...]:
        rows = (
            self._require_open()
            .execute(
                "SELECT trick_id, url, page_sha256, excerpt FROM pb_trick_sightings "
                "WHERE place_key=? ORDER BY first_seen_at, trick_id, page_sha256",
                (place_key,),
            )
            .fetchall()
        )
        return tuple(Sighting(place_key, t, u, s, e) for t, u, s, e in rows)
