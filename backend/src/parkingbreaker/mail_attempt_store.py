"""Durable immutable prepared mailing requests (v6 sections 6.3 and 9.1).

This storage-only seam commits the complete frozen request and its per-attempt
idempotency key before a caller can send it. It does not call Lob, track outcomes,
authorize a re-mail, or coordinate the case and payment state machines. The transaction
caller still generates opaque random case/submission tokens; validation here does not
establish randomness or absence of personal data.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from parkingbreaker.lob_submission import LetterRequest, idempotency_key
from parkingbreaker.mail_request import (
    decode_request,
    envelope_json,
    request_fingerprint,
    validate_request,
)


class PreparationConflict(ValueError):
    """An attempt or submission token is already bound to different frozen data."""


class StorageCorruption(RuntimeError):
    """A stored prepared request failed integrity or format checks."""


class LegacyRequestIncomplete(StorageCorruption):
    """A migrated legacy row lacks the envelope fields required for safe retries."""


def rollback_preserving_error(database: sqlite3.Connection) -> None:
    """Attempt ROLLBACK from inside an ``except`` block without masking the original error.

    A failing ROLLBACK is recorded as a note on the exception being handled; the caller
    still re-raises that original exception.
    """
    original = sys.exception()
    try:
        database.execute("ROLLBACK")
    except Exception as rollback_error:
        if original is not None:
            original.add_note(f"ROLLBACK also failed: {rollback_error!r}")


@dataclass(frozen=True)
class PreparedMailAttempt:
    case_token: str
    attempt: int
    request: LetterRequest
    key: str
    prepared_at: datetime


_LEGACY_COLUMNS = (
    "case_token",
    "attempt",
    "pdf",
    "submission_token",
    "idempotency_key",
    "pdf_sha256",
    "prepared_at",
)
_CURRENT_COLUMNS = (*_LEGACY_COLUMNS, "envelope_json", "request_sha256")
_FRESH_SCHEMA = """\
CREATE TABLE pb_mail_attempts (
    case_token TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt IN (1, 2)),
    pdf BLOB NOT NULL,
    submission_token TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    pdf_sha256 TEXT NOT NULL,
    prepared_at TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    PRIMARY KEY (case_token, attempt)
)
"""
_VERSIONS_SCHEMA = """\
CREATE TABLE pb_store_versions (
    component TEXT PRIMARY KEY,
    version INTEGER NOT NULL
)
"""


def _column_info(database: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(database.execute("PRAGMA table_info(pb_mail_attempts)").fetchall())


def _check_attempt_schema(database: sqlite3.Connection, expected: tuple[str, ...]) -> None:
    info = _column_info(database)
    names = tuple(row[1] for row in info)
    if names != expected:
        raise StorageCorruption("unknown pb_mail_attempts column layout")

    expected_types: tuple[str, ...] = ("TEXT", "INTEGER", "BLOB", "TEXT", "TEXT", "TEXT", "TEXT")
    if expected == _CURRENT_COLUMNS:
        expected_types += ("TEXT", "TEXT")
    for index, row in enumerate(info):
        _, name, column_type, not_null, default, primary_key = row
        if column_type != expected_types[index] or default is not None:
            raise StorageCorruption(f"invalid pb_mail_attempts column definition: {name}")
        if name in {"case_token", "attempt"}:
            if not_null != 1 or primary_key != (1 if name == "case_token" else 2):
                raise StorageCorruption(f"invalid pb_mail_attempts key column: {name}")
        elif name in {"envelope_json", "request_sha256"} and expected == _CURRENT_COLUMNS:
            # Nullable current columns are allowed only when they came from additive
            # migration; fresh tables use NOT NULL and are checked separately below.
            if primary_key:
                raise StorageCorruption(f"invalid pb_mail_attempts column: {name}")
        elif not_null != 1 or primary_key:
            raise StorageCorruption(f"invalid pb_mail_attempts column: {name}")

    sql_row = database.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'pb_mail_attempts'"
    ).fetchone()
    sql = "" if sql_row is None else str(sql_row[0]).upper()
    if "CHECK (ATTEMPT IN (1, 2))" not in sql.replace("\n", " "):
        raise StorageCorruption("pb_mail_attempts attempt constraint is missing")
    unique_indexes = {
        tuple(
            row[0]
            for row in database.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index[1],)
            )
        )
        for index in database.execute("PRAGMA index_list(pb_mail_attempts)").fetchall()
        if index[2] and not index[4]
    }
    required_indexes = {
        ("case_token", "attempt"),
        ("submission_token",),
        ("idempotency_key",),
    }
    if not required_indexes <= unique_indexes:
        raise StorageCorruption("pb_mail_attempts uniqueness constraints are missing")


def check_versions_schema(database: sqlite3.Connection) -> None:
    info = tuple(database.execute("PRAGMA table_info(pb_store_versions)").fetchall())
    if tuple(row[1] for row in info) != ("component", "version"):
        raise StorageCorruption("unknown pb_store_versions column layout")
    component, version = info
    if (
        component[2] != "TEXT"
        or component[3] != 0
        or component[5] != 1
        or version[2] != "INTEGER"
        or version[3] != 1
        or version[5] != 0
        or component[4] is not None
        or version[4] is not None
    ):
        raise StorageCorruption("invalid pb_store_versions column definitions")


def _initialize_schema(database: sqlite3.Connection) -> None:
    """Validate or add the known legacy schema and record its version atomically."""
    database.execute("BEGIN IMMEDIATE")
    try:
        has_attempts = (
            database.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pb_mail_attempts'"
            ).fetchone()
            is not None
        )
        has_versions = (
            database.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pb_store_versions'"
            ).fetchone()
            is not None
        )

        if not has_attempts:
            if has_versions:
                check_versions_schema(database)
                prior = database.execute(
                    "SELECT version FROM pb_store_versions WHERE component = 'mail_attempts'"
                ).fetchone()
                if prior is not None:
                    raise StorageCorruption("mail_attempts version exists without its table")
            database.execute(_FRESH_SCHEMA)
            if not has_versions:
                database.execute(_VERSIONS_SCHEMA)
            _check_attempt_schema(database, _CURRENT_COLUMNS)
            check_versions_schema(database)
            database.execute(
                "INSERT INTO pb_store_versions(component, version) VALUES ('mail_attempts', 2)"
            )
            database.execute("COMMIT")
            return

        names = tuple(row[1] for row in _column_info(database))
        if names not in (_LEGACY_COLUMNS, _CURRENT_COLUMNS):
            raise StorageCorruption("unknown pb_mail_attempts column layout")
        if has_versions:
            check_versions_schema(database)
            version_row = database.execute(
                "SELECT version FROM pb_store_versions WHERE component = 'mail_attempts'"
            ).fetchone()
            version = None if version_row is None else version_row[0]
        else:
            version = None

        if names == _CURRENT_COLUMNS:
            if version != 2:
                raise StorageCorruption("current mail_attempts schema has no known version")
            _check_attempt_schema(database, _CURRENT_COLUMNS)
            database.execute("COMMIT")
            return

        if version is not None:
            raise StorageCorruption("legacy mail_attempts schema has an unsupported version")
        _check_attempt_schema(database, _LEGACY_COLUMNS)
        database.execute("ALTER TABLE pb_mail_attempts ADD COLUMN envelope_json TEXT")
        database.execute("ALTER TABLE pb_mail_attempts ADD COLUMN request_sha256 TEXT")
        if not has_versions:
            database.execute(_VERSIONS_SCHEMA)
        _check_attempt_schema(database, _CURRENT_COLUMNS)
        check_versions_schema(database)
        database.execute(
            "INSERT INTO pb_store_versions(component, version) VALUES ('mail_attempts', 2)"
        )
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def _validate_inputs(
    case_token: str, attempt: int, request: LetterRequest, prepared_at: datetime
) -> None:
    if not isinstance(case_token, str) or not case_token.strip():
        raise ValueError("case_token must be a non-blank string")
    if type(attempt) is not int or attempt not in (1, 2):
        raise ValueError("attempt must be the integer 1 or 2")
    validate_request(request)
    if not isinstance(prepared_at, datetime):
        raise ValueError("prepared_at must be a timezone-aware datetime")
    if prepared_at.tzinfo is None or prepared_at.utcoffset() is None:
        raise ValueError("prepared_at must be timezone-aware")


def _decode_row(row: tuple[object, ...]) -> PreparedMailAttempt:
    try:
        (
            case_token,
            attempt,
            pdf,
            submission_token,
            key,
            pdf_hash,
            prepared_text,
            envelope_text,
            request_hash,
        ) = row
        if envelope_text is None or request_hash is None:
            raise LegacyRequestIncomplete("legacy prepared attempt has no stored envelope request")
        if not isinstance(case_token, str) or not case_token.strip():
            raise ValueError("invalid case token")
        if type(attempt) is not int or attempt not in (1, 2):
            raise ValueError("invalid attempt")
        if not isinstance(pdf, bytes) or not pdf:
            raise ValueError("invalid PDF bytes")
        if not isinstance(submission_token, str) or not submission_token.strip():
            raise ValueError("invalid submission token")
        if not isinstance(key, str) or key != idempotency_key(case_token, attempt):
            raise ValueError("idempotency key does not match case and attempt")
        if not isinstance(pdf_hash, str) or pdf_hash != hashlib.sha256(pdf).hexdigest():
            raise ValueError("PDF hash mismatch")
        if not isinstance(prepared_text, str):
            raise ValueError("invalid prepared timestamp")
        prepared_at = datetime.fromisoformat(prepared_text)
        if prepared_at.tzinfo is None or prepared_at.utcoffset() is None:
            raise ValueError("prepared timestamp is not timezone-aware")
        if not isinstance(envelope_text, str) or not isinstance(request_hash, str):
            raise ValueError("invalid stored request envelope or fingerprint")
        request = decode_request(pdf, submission_token, envelope_text)
        validate_request(request)
        if request.pdf != pdf or request.submission_token != submission_token:
            raise ValueError("request envelope does not match stored request keys")
        if request_fingerprint(request) != request_hash:
            raise ValueError("request fingerprint mismatch")
    except LegacyRequestIncomplete:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise StorageCorruption(f"invalid prepared mail attempt: {exc}") from exc

    return PreparedMailAttempt(
        case_token=case_token,
        attempt=attempt,
        request=request,
        key=key,
        prepared_at=prepared_at,
    )


class SqliteMailAttemptStore:
    """SQLite storage for immutable prepared requests, with guarded readback."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if self.path.is_symlink():
            raise ValueError("database path must not be a symlink")

        created = False
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            if self.path.is_symlink():
                raise ValueError("database path must not be a symlink") from None
        else:
            os.close(descriptor)
            created = True

        if self.path.is_symlink():
            raise ValueError("database path must not be a symlink")
        if created:
            self.path.chmod(0o600)

        self._database: sqlite3.Connection | None = sqlite3.connect(
            self.path, timeout=5.0, isolation_level=None
        )
        try:
            self._database.execute("PRAGMA journal_mode=WAL")
            self._database.execute("PRAGMA synchronous=FULL")
            _initialize_schema(self._database)
        except Exception:
            self.close()
            raise

    def __enter__(self) -> SqliteMailAttemptStore:
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._database is not None:
            self._database.close()
            self._database = None

    def _require_open(self) -> sqlite3.Connection:
        if self._database is None:
            raise RuntimeError("mail attempt store is closed")
        return self._database

    def _load(self, case_token: str, attempt: int) -> PreparedMailAttempt | None:
        database = self._require_open()
        try:
            row = database.execute(
                """SELECT case_token, attempt, pdf, submission_token, idempotency_key,
                          pdf_sha256, prepared_at, envelope_json, request_sha256
                   FROM pb_mail_attempts WHERE case_token = ? AND attempt = ?""",
                (case_token, attempt),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageCorruption(f"cannot load prepared mail attempt: {exc}") from exc
        return None if row is None else _decode_row(row)

    def _recover_legacy(self, case_token: str, attempt: int, request: LetterRequest) -> None:
        """Backfill a legacy row's NULL envelope fields from the identical frozen request.

        Runs inside the caller's open write transaction. Only the two NULL columns are
        written; the frozen identity columns are never changed. A request whose PDF or
        submission token differs from the stored row is refused with the row untouched.
        """
        database = self._require_open()
        try:
            row = database.execute(
                """SELECT pdf, submission_token, idempotency_key, pdf_sha256,
                          envelope_json, request_sha256
                   FROM pb_mail_attempts WHERE case_token = ? AND attempt = ?
                   AND (envelope_json IS NULL OR request_sha256 IS NULL)""",
                (case_token, attempt),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageCorruption(f"cannot load legacy mail attempt: {exc}") from exc
        if row is None:
            raise StorageCorruption("legacy prepared attempt vanished during recovery")
        pdf, submission_token, key, pdf_hash, stored_envelope, stored_fingerprint = row
        candidate_envelope = envelope_json(request)
        candidate_fingerprint = request_fingerprint(request)
        if (
            pdf != request.pdf
            or submission_token != request.submission_token
            or key != idempotency_key(case_token, attempt)
            or pdf_hash != hashlib.sha256(request.pdf).hexdigest()
        ):
            raise PreparationConflict("case and attempt already have a different prepared request")
        if (stored_envelope is not None and stored_envelope != candidate_envelope) or (
            stored_fingerprint is not None and stored_fingerprint != candidate_fingerprint
        ):
            raise PreparationConflict("case and attempt already have a different prepared request")
        updated = database.execute(
            """UPDATE pb_mail_attempts
               SET envelope_json = COALESCE(envelope_json, ?),
                   request_sha256 = COALESCE(request_sha256, ?)
               WHERE case_token = ? AND attempt = ?
               AND (envelope_json IS NULL OR request_sha256 IS NULL)""",
            (candidate_envelope, candidate_fingerprint, case_token, attempt),
        )
        if updated.rowcount != 1:
            raise StorageCorruption("legacy recovery did not update exactly one row")

    def get(self, case_token: str, attempt: int) -> PreparedMailAttempt | None:
        """Load one record, refusing malformed or tampered rows."""
        if not isinstance(case_token, str) or not case_token.strip():
            raise ValueError("case_token must be a non-blank string")
        if type(attempt) is not int or attempt not in (1, 2):
            raise ValueError("attempt must be the integer 1 or 2")
        return self._load(case_token, attempt)

    def prepare(
        self,
        case_token: str,
        attempt: int,
        request: LetterRequest,
        prepared_at: datetime,
        *,
        expected_research: tuple[str, int] | None = None,
    ) -> PreparedMailAttempt:
        """Persist once before send; identical repeats return the original record."""
        _validate_inputs(case_token, attempt, request, prepared_at)
        if expected_research is not None and (
            not isinstance(expected_research, tuple)
            or len(expected_research) != 2
            or not isinstance(expected_research[0], str)
            or not expected_research[0].strip()
            or type(expected_research[1]) is not int
            or expected_research[1] <= 0
        ):
            raise ValueError(
                "expected_research must contain a non-blank intake id and positive revision"
            )
        database = self._require_open()
        owns_transaction = not database.in_transaction
        try:
            if owns_transaction:
                database.execute("BEGIN IMMEDIATE")
            if expected_research is not None:
                intake_id, revision = expected_research
                research = database.execute(
                    "SELECT revision FROM pb_intake_research WHERE intake_id=?", (intake_id,)
                ).fetchone()
                if research is None or research[0] != revision:
                    raise PreparationConflict("research_revision_stale")
            try:
                existing = self._load(case_token, attempt)
            except LegacyRequestIncomplete:
                self._recover_legacy(case_token, attempt, request)
                existing = self._load(case_token, attempt)
            if existing is not None:
                if existing.request != request:
                    raise PreparationConflict(
                        "case and attempt already have a different prepared request"
                    )
                if owns_transaction:
                    database.execute("COMMIT")
                loaded = self._load(case_token, attempt)
                if loaded is None:
                    raise StorageCorruption("prepared attempt disappeared after commit")
                return loaded

            key = idempotency_key(case_token, attempt)
            pdf_hash = hashlib.sha256(request.pdf).hexdigest()
            envelope_text = envelope_json(request)
            request_hash = request_fingerprint(request)
            try:
                database.execute(
                    """INSERT INTO pb_mail_attempts
                       (case_token, attempt, pdf, submission_token, idempotency_key,
                        pdf_sha256, prepared_at, envelope_json, request_sha256)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        case_token,
                        attempt,
                        request.pdf,
                        request.submission_token,
                        key,
                        pdf_hash,
                        prepared_at.isoformat(),
                        envelope_text,
                        request_hash,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PreparationConflict(
                    "submission token or idempotency key is already assigned"
                ) from exc
            self._bind_new_destination(case_token, attempt, request, prepared_at)
            if owns_transaction:
                database.execute("COMMIT")
        except Exception:
            if owns_transaction and database.in_transaction:
                rollback_preserving_error(database)
            raise

        loaded = self._load(case_token, attempt)
        if loaded is None:
            raise StorageCorruption("prepared attempt disappeared after commit")
        return loaded

    def _bind_new_destination(
        self, case_token: str, attempt: int, request: LetterRequest, prepared_at: datetime
    ) -> None:
        """Transaction-store extension, called only on insert inside the freeze transaction.

        Existing requests and recovered legacy rows deliberately never call this hook.
        """
