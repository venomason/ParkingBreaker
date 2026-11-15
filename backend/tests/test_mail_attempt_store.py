from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from mail_samples import make_request
from parkingbreaker.lob_submission import LetterRequest, idempotency_key
from parkingbreaker.mail_attempt_store import (
    LegacyRequestIncomplete,
    PreparationConflict,
    SqliteMailAttemptStore,
    StorageCorruption,
)

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
REQUEST = make_request()


def test_prepared_request_survives_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteMailAttemptStore(path) as store:
        assert store.get("case-1", 1) is None
        saved = store.prepare("case-1", 1, REQUEST, NOW)
        assert saved.key == idempotency_key("case-1", 1)
        assert saved.request == REQUEST
        assert saved.prepared_at == NOW
    with SqliteMailAttemptStore(path) as reopened:
        assert reopened.get("case-1", 1) == saved
    assert path.stat().st_mode & 0o777 == 0o600


def test_identical_prepare_preserves_original_record(tmp_path: Path) -> None:
    with SqliteMailAttemptStore(tmp_path / "cases.sqlite") as store:
        first = store.prepare("case-1", 1, REQUEST, NOW)
        second = store.prepare("case-1", 1, REQUEST, NOW + timedelta(minutes=10))
        assert second == first


@pytest.mark.parametrize(
    "changed",
    [
        replace(REQUEST, pdf=b"different bytes"),
        replace(REQUEST, submission_token="other-token"),
        replace(REQUEST, sender=replace(REQUEST.sender, line1="456 Other Street")),
        replace(REQUEST, recipient=replace(REQUEST.recipient, line2="Suite 3")),
        replace(REQUEST, options=replace(REQUEST.options, color=True)),
        replace(REQUEST, options=replace(REQUEST.options, double_sided=True)),
        replace(REQUEST, options=replace(REQUEST.options, return_receipt=True)),
    ],
)
def test_same_attempt_cannot_replace_request(tmp_path: Path, changed: LetterRequest) -> None:
    with SqliteMailAttemptStore(tmp_path / "cases.sqlite") as store:
        original = store.prepare("case-1", 1, REQUEST, NOW)
        with pytest.raises(PreparationConflict):
            store.prepare("case-1", 1, changed, NOW)
        assert store.get("case-1", 1) == original


def test_distinct_attempts_have_distinct_keys_and_tokens(tmp_path: Path) -> None:
    with SqliteMailAttemptStore(tmp_path / "cases.sqlite") as store:
        first = store.prepare("case-1", 1, REQUEST, NOW)
        second_request = replace(REQUEST, submission_token="synthetic-random-token-2")
        second = store.prepare("case-1", 2, second_request, NOW)
        assert first.key != second.key
        assert store.get("case-1", 1) == first
        assert store.get("case-1", 2) == second
        with pytest.raises(PreparationConflict):
            store.prepare("case-2", 1, REQUEST, NOW)
        assert store.get("case-2", 1) is None


@pytest.mark.parametrize("attempt", [False, True, 0, -1, 3])
def test_invalid_attempt_is_rejected(tmp_path: Path, attempt: int) -> None:
    with SqliteMailAttemptStore(tmp_path / "cases.sqlite") as store:
        with pytest.raises(ValueError, match="attempt"):
            store.prepare("case-1", attempt, REQUEST, NOW)
        with pytest.raises(ValueError, match="attempt"):
            store.get("case-1", attempt)


def test_naive_timestamp_is_rejected(tmp_path: Path) -> None:
    with SqliteMailAttemptStore(tmp_path / "cases.sqlite") as store:
        with pytest.raises(ValueError, match="timezone-aware"):
            store.prepare("case-1", 1, REQUEST, NOW.replace(tzinfo=None))
        assert store.get("case-1", 1) is None


@pytest.mark.parametrize("case_token", ["", "   "])
def test_empty_case_token_is_rejected(tmp_path: Path, case_token: str) -> None:
    with (
        SqliteMailAttemptStore(tmp_path / "cases.sqlite") as store,
        pytest.raises(ValueError, match="case_token"),
    ):
        store.prepare(case_token, 1, REQUEST, NOW)


@pytest.mark.parametrize("letter_request", [make_request(b"", "token"), make_request(b"pdf", " ")])
def test_empty_request_is_rejected(tmp_path: Path, letter_request: LetterRequest) -> None:
    with SqliteMailAttemptStore(tmp_path / "cases.sqlite") as store:
        with pytest.raises(ValueError, match=r"[Pp][Dd][Ff]|submission_token"):
            store.prepare("case-1", 1, letter_request, NOW)
        assert store.get("case-1", 1) is None


@pytest.mark.parametrize(
    ("column", "value"),
    [("pdf", b"changed packet"), ("idempotency_key", "wrong key"), ("prepared_at", "not-a-date")],
)
def test_corrupt_request_is_not_loaded(tmp_path: Path, column: str, value: bytes | str) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteMailAttemptStore(path) as store:
        store.prepare("case-1", 1, REQUEST, NOW)
    with sqlite3.connect(path) as database:
        database.execute(f"UPDATE pb_mail_attempts SET {column} = ?", (value,))
    with SqliteMailAttemptStore(path) as reopened, pytest.raises(StorageCorruption):
        reopened.get("case-1", 1)


def test_competing_prepare_cannot_overwrite_committed_request(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteMailAttemptStore(path):
        pass
    barrier = Barrier(2)

    def prepare(token: str) -> LetterRequest | None:
        with SqliteMailAttemptStore(path) as store:
            barrier.wait(timeout=5)
            request = make_request(token.encode(), token)
            try:
                return store.prepare("case-1", 1, request, NOW).request
            except PreparationConflict:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(prepare, token) for token in ("token-a", "token-b")]
        results = [future.result(timeout=10) for future in futures]
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    with SqliteMailAttemptStore(path) as reopened:
        saved = reopened.get("case-1", 1)
        assert saved is not None
        assert saved.request == winners[0]


def test_committed_prepare_survives_abrupt_process_exit(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    script = """\
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from mail_samples import make_request
from parkingbreaker.mail_attempt_store import SqliteMailAttemptStore
store = SqliteMailAttemptStore(Path(sys.argv[1]))
store.prepare(
    'case-1', 1, make_request(b'packet', 'crash-token'), datetime(2026, 10, 11, tzinfo=UTC)
)
os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-c", script, str(path)],
        check=True,
        timeout=10,
        cwd=Path(__file__).parent,
    )
    with SqliteMailAttemptStore(path) as store:
        saved = store.get("case-1", 1)
        assert saved is not None
        assert saved.request == make_request(b"packet", "crash-token")
        assert saved.key == idempotency_key("case-1", 1)


def test_symlink_database_path_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "existing.sqlite"
    target.write_bytes(b"original")
    link = tmp_path / "link.sqlite"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        SqliteMailAttemptStore(link)
    assert target.read_bytes() == b"original"


def legacy_database(path: Path) -> tuple[object, ...]:
    with sqlite3.connect(path) as database:
        database.execute("""CREATE TABLE pb_mail_attempts (
            case_token TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK (attempt IN (1, 2)),
            pdf BLOB NOT NULL,
            submission_token TEXT NOT NULL UNIQUE,
            idempotency_key TEXT NOT NULL UNIQUE,
            pdf_sha256 TEXT NOT NULL,
            prepared_at TEXT NOT NULL,
            PRIMARY KEY (case_token, attempt)
        )""")
        original = (
            "legacy-case",
            1,
            b"legacy-pdf",
            "legacy-token",
            idempotency_key("legacy-case", 1),
            hashlib.sha256(b"legacy-pdf").hexdigest(),
            NOW.isoformat(),
        )
        database.execute("INSERT INTO pb_mail_attempts VALUES (?, ?, ?, ?, ?, ?, ?)", original)
    return original


def test_legacy_migration_preserves_records_without_inventing_envelopes(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    original = legacy_database(path)
    with SqliteMailAttemptStore(path) as store:
        with pytest.raises(LegacyRequestIncomplete):
            store.get("legacy-case", 1)
        # A request that is not the frozen legacy identity is refused, row untouched.
        with pytest.raises(PreparationConflict):
            store.prepare("legacy-case", 1, REQUEST, NOW)
        saved = store.prepare("new-case", 1, REQUEST, NOW)
        assert saved.request == REQUEST
    with sqlite3.connect(path) as database:
        retained = database.execute("""SELECT case_token, attempt, pdf, submission_token,
            idempotency_key, pdf_sha256, prepared_at FROM pb_mail_attempts
            WHERE case_token = 'legacy-case'""").fetchone()
        assert retained == original
        assert database.execute(
            "SELECT version FROM pb_store_versions WHERE component = 'mail_attempts'"
        ).fetchone() == (2,)
    with SqliteMailAttemptStore(path) as store, pytest.raises(LegacyRequestIncomplete):
        store.get("legacy-case", 1)


def test_unknown_schema_version_cannot_be_silently_migrated(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteMailAttemptStore(path) as store:
        original = store.prepare("case-1", 1, REQUEST, NOW)
    with sqlite3.connect(path) as database:
        database.execute("UPDATE pb_store_versions SET version = 999")
    with pytest.raises(StorageCorruption):
        SqliteMailAttemptStore(path)
    with sqlite3.connect(path) as database:
        assert database.execute("SELECT version FROM pb_store_versions").fetchone() == (999,)
        assert database.execute("SELECT pdf FROM pb_mail_attempts").fetchone() == (
            original.request.pdf,
        )


@pytest.mark.parametrize("column", ["envelope_json", "request_sha256", "submission_token"])
def test_material_request_corruption_fails_readback(tmp_path: Path, column: str) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteMailAttemptStore(path) as store:
        store.prepare("case-1", 1, REQUEST, NOW)
    with sqlite3.connect(path) as database:
        database.execute(f"UPDATE pb_mail_attempts SET {column} = ?", ("changed",))
    with SqliteMailAttemptStore(path) as store, pytest.raises(StorageCorruption):
        store.get("case-1", 1)


def test_partial_unique_index_cannot_authorize_schema_migration(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with sqlite3.connect(path) as database:
        database.execute("""CREATE TABLE pb_mail_attempts (
            case_token TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK (attempt IN (1, 2)),
            pdf BLOB NOT NULL,
            submission_token TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            pdf_sha256 TEXT NOT NULL,
            prepared_at TEXT NOT NULL,
            PRIMARY KEY (case_token, attempt)
        )""")
        database.execute("""CREATE UNIQUE INDEX partial_token
            ON pb_mail_attempts(submission_token) WHERE attempt = 2""")
    with pytest.raises(StorageCorruption, match="uniqueness"):
        SqliteMailAttemptStore(path)
    with sqlite3.connect(path) as database:
        assert len(database.execute("PRAGMA table_info(pb_mail_attempts)").fetchall()) == 7
        assert (
            database.execute(
                "SELECT name FROM sqlite_master WHERE name = 'pb_store_versions'"
            ).fetchone()
            is None
        )
