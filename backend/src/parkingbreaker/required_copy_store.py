"""Research-bound customer document selections on the transaction connection."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from parkingbreaker.intake_store import IntakeConflict
from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.transaction_records import _reject_constant, _unique_object

_DDL = """CREATE TABLE pb_required_copies (
    case_id TEXT PRIMARY KEY REFERENCES pb_intake_records(intake_id),
    owner TEXT NOT NULL,
    research_sha256 TEXT NOT NULL,
    selections_json TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1)
)"""
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_REF = re.compile(r"photo_[a-f0-9]{64}\Z")


@dataclass(frozen=True)
class SavedRequiredCopies:
    research_sha256: str
    selections_json: str
    revision: int


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


def _validate(research: object, selections: object, revision: object) -> None:
    if not isinstance(research, str) or not _HASH.fullmatch(research):
        raise ValueError("invalid research hash")
    if type(revision) is not int or revision < 1:
        raise ValueError("invalid required copies revision")
    if not isinstance(selections, str):
        raise ValueError("invalid selections")
    items = json.loads(
        selections, object_pairs_hook=_unique_object, parse_constant=_reject_constant
    )
    if not isinstance(items, list) or len(items) > 20:
        raise ValueError("invalid selections")
    seen = set()
    total = 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {"requirement_id", "page_refs", "confirmed"}:
            raise ValueError("invalid selection")
        key = item["requirement_id"]
        refs = item["page_refs"]
        if (
            not isinstance(key, str)
            or not _HASH.fullmatch(key)
            or key in seen
            or item["confirmed"] is not True
            or not isinstance(refs, list)
            or not 1 <= len(refs) <= 10
            or any(not isinstance(ref, str) or not _REF.fullmatch(ref) for ref in refs)
        ):
            raise ValueError("invalid selection")
        seen.add(key)
        total += len(refs)
    if total > 20:
        raise ValueError("too many required pages")


def initialize_required_copy_store(database: sqlite3.Connection) -> None:
    """Install the independent versioned table, preserving all existing records."""
    try:
        database.execute("BEGIN IMMEDIATE")
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='required_copies'"
        ).fetchone()
        table = database.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='pb_required_copies'"
        ).fetchone()
        if version is None:
            if table is not None:
                raise StorageCorruption("unversioned required copies schema")
            database.execute(_DDL)
            database.execute("INSERT INTO pb_store_versions VALUES ('required_copies', 1)")
        elif (
            type(version[0]) is not int
            or version[0] != 1
            or table is None
            or " ".join(table[0].upper().split()) != " ".join(_DDL.upper().split())
        ):
            raise StorageCorruption("invalid required copies schema")
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def _owned(database: sqlite3.Connection, case_id: str, owner: str) -> tuple[str | None, str | None]:
    row = database.execute(
        "SELECT r.case_token,c.letter_state FROM pb_intake_records r "
        "JOIN pb_intake_owners o ON o.workspace_token=r.workspace_token "
        "LEFT JOIN pb_cases c ON c.case_token=r.case_token "
        "WHERE r.intake_id=? AND o.owner=?",
        (case_id, owner),
    ).fetchone()
    if row is None:
        raise IntakeConflict("not_found")
    return row[0], row[1]


def _read(database: sqlite3.Connection, case_id: str, owner: str) -> SavedRequiredCopies | None:
    row = database.execute(
        "SELECT owner,research_sha256,selections_json,revision "
        "FROM pb_required_copies WHERE case_id=?",
        (case_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        if row[0] != owner:
            raise ValueError("invalid selection owner")
        _validate(row[1], row[2], row[3])
    except (ValueError, TypeError, RecursionError) as exc:
        raise StorageCorruption("invalid required copies record") from exc
    return SavedRequiredCopies(row[1], row[2], row[3])


class RequiredCopyStoreMixin:
    def get_required_copies(
        self: _Connected, case_id: str, owner: str
    ) -> SavedRequiredCopies | None:
        database = self._require_open()
        _owned(database, case_id, owner)
        return _read(database, case_id, owner)

    def save_required_copies(
        self: _Connected,
        case_id: str,
        owner: str,
        research_sha256: str,
        selections_json: str,
        expected_revision: int,
        now: datetime,
    ) -> SavedRequiredCopies:
        _validate(research_sha256, selections_json, 1)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("invalid expected revision")
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            token, state = _owned(database, case_id, owner)
            current = _read(database, case_id, owner)
            research = database.execute(
                "SELECT research_json FROM pb_intake_research WHERE intake_id=?", (case_id,)
            ).fetchone()
            if (
                research is None
                or hashlib.sha256(research[0].encode()).hexdigest() != research_sha256
            ):
                raise IntakeConflict("research_revision_stale")
            if state not in {"RESEARCHING", "NEEDS_CUSTOMER", "DRAFTING"} or any(
                (
                    database.execute(
                        "SELECT 1 FROM pb_address_locks WHERE case_id=?", (case_id,)
                    ).fetchone(),
                    database.execute(
                        "SELECT 1 FROM pb_intake_drafts WHERE intake_id=?", (case_id,)
                    ).fetchone(),
                    database.execute(
                        "SELECT 1 FROM pb_mail_attempts WHERE case_token=?", (token,)
                    ).fetchone(),
                )
            ):
                raise IntakeConflict("required_copies_locked")
            revision = 0 if current is None else current.revision
            if (
                current is not None
                and expected_revision == revision - 1
                and (
                    current.research_sha256 == research_sha256
                    and current.selections_json == selections_json
                )
            ):
                database.execute("COMMIT")
                return current
            if expected_revision != revision:
                raise IntakeConflict("required_copies_revision_stale")
            database.execute(
                "INSERT INTO pb_required_copies VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(case_id) DO UPDATE SET research_sha256=excluded.research_sha256, "
                "selections_json=excluded.selections_json, revision=excluded.revision",
                (case_id, owner, research_sha256, selections_json, revision + 1),
            )
            database.execute("COMMIT")
            return SavedRequiredCopies(research_sha256, selections_json, revision + 1)
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
