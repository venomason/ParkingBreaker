"""H3: durable per-operation provenance for submission lookups (submission schema v2).

A lookup forced only by an incident hold is temporary; it must not make later creates
corruption. Canonical v1 operations are permanent, migrate with marker 0, and still reject
a create after a lookup.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.submission_coordinator import run_submission_step
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_submission_coordinator import NOW, prepared

V1_COLUMNS = (
    "operation_id",
    "case_token",
    "attempt",
    "cursor_revision",
    "kind",
    "started_at",
    "outcome_json",
    "completed_at",
)
V1_DDL = """CREATE TABLE pb_submission_operations_v1 (
    operation_id TEXT PRIMARY KEY, case_token TEXT NOT NULL,
    attempt INTEGER NOT NULL, cursor_revision INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('create', 'lookup')), started_at TEXT NOT NULL,
    outcome_json TEXT, completed_at TEXT,
    FOREIGN KEY (case_token, attempt) REFERENCES pb_mail_attempts(case_token, attempt),
    CHECK ((outcome_json IS NULL) = (completed_at IS NULL))
)"""


def _build_v1(path: Path) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    """A real accepted first attempt, then rewritten as the exact canonical v1 schema."""
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        ids = (f"effect-{index}" for index in range(10))
        run_submission_step(
            store,
            FakeLobClient([LobResponse(200, "ltr-1", evidence=LetterEvidence.PRODUCTION)]),
            case.case_token,
            1,
            expected_case_revision=case.revision,
            expected_cursor_revision=None,
            operation_id="op-1",
            clock=lambda: NOW,
            new_effect_id=lambda: next(ids),
        )
    database = sqlite3.connect(path)
    try:
        operations = database.execute(
            "SELECT rowid, operation_id, case_token, attempt, cursor_revision, kind, "
            "started_at, outcome_json, completed_at FROM pb_submission_operations ORDER BY rowid"
        ).fetchall()
        cursors = database.execute("SELECT * FROM pb_submission_cursors ORDER BY rowid").fetchall()
        database.execute("BEGIN IMMEDIATE")
        database.execute(V1_DDL)
        database.execute(
            "INSERT INTO pb_submission_operations_v1 (" + ", ".join(V1_COLUMNS) + ") "
            "SELECT " + ", ".join(V1_COLUMNS) + " FROM pb_submission_operations"
        )
        database.execute("DROP TABLE pb_submission_operations")
        database.execute(
            "ALTER TABLE pb_submission_operations_v1 RENAME TO pb_submission_operations"
        )
        database.execute("UPDATE pb_store_versions SET version=1 WHERE component='submission'")
        database.execute("COMMIT")
        return operations, cursors
    finally:
        database.close()


def test_canonical_v1_migrates_rows_and_stays_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite"
    operations, cursors = _build_v1(path)
    store = SqliteTransactionStore(path)
    try:
        rows = (
            store._require_open()
            .execute(
                "SELECT rowid, operation_id, case_token, attempt, cursor_revision, kind, "
                "started_at, outcome_json, completed_at, temporary_lookup "
                "FROM pb_submission_operations ORDER BY rowid"
            )
            .fetchall()
        )
        assert rows == [(*row, 0) for row in operations]
        assert (
            store._require_open()
            .execute("SELECT * FROM pb_submission_cursors ORDER BY rowid")
            .fetchall()
            == cursors
        )
        assert store._require_open().execute(
            "SELECT version FROM pb_store_versions WHERE component='submission'"
        ).fetchone() == (2,)
    finally:
        store.close()
    reopened = SqliteTransactionStore(path)
    try:
        assert reopened._require_open().execute(
            "SELECT COUNT(*), MIN(temporary_lookup), MAX(temporary_lookup) "
            "FROM pb_submission_operations"
        ).fetchone() == (len(operations), 0, 0)
    finally:
        reopened.close()


def test_interrupted_migration_rolls_schema_and_version_back(tmp_path: Path) -> None:
    path = tmp_path / "rollback.sqlite"
    operations, _ = _build_v1(path)
    database = sqlite3.connect(path)
    try:
        database.execute("BEGIN IMMEDIATE")
        database.execute(
            "ALTER TABLE pb_submission_operations ADD COLUMN temporary_lookup INTEGER "
            "NOT NULL DEFAULT 0 CHECK (temporary_lookup IN (0, 1) AND "
            "(temporary_lookup = 0 OR kind = 'lookup'))"
        )
        database.execute("UPDATE pb_store_versions SET version=2 WHERE component='submission'")
        database.execute("ROLLBACK")
        assert (
            tuple(row[1] for row in database.execute("PRAGMA table_info(pb_submission_operations)"))
            == V1_COLUMNS
        )
        assert database.execute(
            "SELECT version FROM pb_store_versions WHERE component='submission'"
        ).fetchone() == (1,)
        assert database.execute(
            "SELECT rowid, operation_id FROM pb_submission_operations ORDER BY rowid"
        ).fetchall() == [(row[0], row[1]) for row in operations]
    finally:
        database.close()


@pytest.mark.parametrize("mutation", ["version2_without_marker", "unknown_version", "extra_column"])
def test_noncanonical_submission_schemas_fail_closed(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / f"{mutation}.sqlite"
    _build_v1(path)
    database = sqlite3.connect(path)
    try:
        if mutation == "version2_without_marker":
            database.execute("UPDATE pb_store_versions SET version=2 WHERE component='submission'")
        elif mutation == "unknown_version":
            database.execute("UPDATE pb_store_versions SET version=3 WHERE component='submission'")
        else:
            database.execute("ALTER TABLE pb_submission_operations ADD COLUMN extra TEXT")
        database.commit()
    finally:
        database.close()
    with pytest.raises(StorageCorruption):
        SqliteTransactionStore(path)


def test_v1_lookup_then_create_is_still_corruption_after_migration(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    operations, _ = _build_v1(path)
    token = str(operations[0][2])
    attempt = int(str(operations[0][3]))
    started = str(operations[0][6])
    database = sqlite3.connect(path)
    try:
        database.execute("BEGIN IMMEDIATE")
        database.execute("DELETE FROM pb_submission_operations")
        for operation_id, revision, kind in (
            ("create-1", 1, "create"),
            ("lookup-1", 2, "lookup"),
            ("create-2", 3, "create"),
        ):
            database.execute(
                "INSERT INTO pb_submission_operations (operation_id, case_token, attempt, "
                "cursor_revision, kind, started_at, outcome_json, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
                (operation_id, token, attempt, revision, kind, started),
            )
        database.execute(
            "UPDATE pb_submission_cursors SET sends=2, revision=3, lookup_only=1, owner='create-2'"
        )
        database.execute("UPDATE pb_store_versions SET version=1 WHERE component='submission'")
        database.execute("COMMIT")
    finally:
        database.close()
    migrated = SqliteTransactionStore(path)
    try:
        with pytest.raises(StorageCorruption):
            migrated.submission_cursor(token, attempt)
    finally:
        migrated.close()


def test_real_outer_rollback_on_migration_failure_restores_v1_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject failure after ALTER/version update inside the real store open.

    The validator is patched to call the production original first, so the exact
    v1/v2 checks still run; only version 2 then raises. The real outer
    BEGIN IMMEDIATE rollback must restore schema, version and every row/rowid.
    """
    from parkingbreaker import transaction_store as store_module

    path = tmp_path / "outer-rollback.sqlite"
    operations, cursors = _build_v1(path)
    original = store_module._check_submission_operations
    calls: list[int] = []

    def injected(database: sqlite3.Connection, *, version: int) -> None:
        original(database, version=version)
        calls.append(version)
        if version == 2:
            raise RuntimeError("injected failure after ALTER and version update")

    monkeypatch.setattr(store_module, "_check_submission_operations", injected)
    with pytest.raises(RuntimeError, match="injected failure"):
        SqliteTransactionStore(path)
    assert calls == [1, 2]

    database = sqlite3.connect(path)
    try:
        assert (
            tuple(row[1] for row in database.execute("PRAGMA table_info(pb_submission_operations)"))
            == V1_COLUMNS
        )
        assert database.execute(
            "SELECT version FROM pb_store_versions WHERE component='submission'"
        ).fetchone() == (1,)
        assert (
            database.execute(
                "SELECT rowid, operation_id, case_token, attempt, cursor_revision, kind, "
                "started_at, outcome_json, completed_at "
                "FROM pb_submission_operations ORDER BY rowid"
            ).fetchall()
            == operations
        )
        assert (
            database.execute("SELECT * FROM pb_submission_cursors ORDER BY rowid").fetchall()
            == cursors
        )
    finally:
        database.close()


def _rebuilt_v2_history(path: Path, rows: list[tuple[str, int, int, str]]) -> tuple[str, int, str]:
    """Migrate a real v1 fixture to v2, then rewrite its operation history."""
    operations, _ = _build_v1(path)
    migrated = SqliteTransactionStore(path)
    migrated.close()
    token = str(operations[0][2])
    attempt = int(str(operations[0][3]))
    started = str(operations[0][6])
    database = sqlite3.connect(path)
    try:
        database.execute("BEGIN IMMEDIATE")
        database.execute("DELETE FROM pb_submission_operations")
        for operation_id, revision, marker, kind in rows:
            database.execute(
                "INSERT INTO pb_submission_operations (operation_id, case_token, attempt, "
                "cursor_revision, kind, started_at, outcome_json, completed_at, temporary_lookup) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                (operation_id, token, attempt, revision, kind, started, marker),
            )
        database.execute(
            "UPDATE pb_submission_cursors SET sends=?, revision=?, lookup_only=1, owner=NULL",
            (max(1, sum(kind == "create" for _, _, _, kind in rows)), rows[-1][1]),
        )
        database.execute("COMMIT")
    finally:
        database.close()
    return token, attempt, started


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([("lookup-1", 1, 1, "lookup")], id="marker1_first_operation"),
        pytest.param(
            [
                ("create-1", 1, 0, "create"),
                ("lookup-1", 2, 0, "lookup"),
                ("lookup-2", 3, 1, "lookup"),
            ],
            id="marker1_after_permanent_lookup",
        ),
    ],
)
def test_runtime_marker1_is_rejected_where_the_check_allows_insertion(
    tmp_path: Path, rows: list[tuple[str, int, int, str]]
) -> None:
    path = tmp_path / "marker1.sqlite"
    token, attempt, _ = _rebuilt_v2_history(path, rows)
    store = SqliteTransactionStore(path)
    try:
        with pytest.raises(StorageCorruption):
            store.submission_cursor(token, attempt)
    finally:
        store.close()


def test_v2_marker0_lookup_then_create_rejects_even_with_flag_zero(tmp_path: Path) -> None:
    path = tmp_path / "marker0-create.sqlite"
    rows = [
        ("create-1", 1, 0, "create"),
        ("lookup-1", 2, 0, "lookup"),
        ("create-2", 3, 0, "create"),
    ]
    token, attempt, _ = _rebuilt_v2_history(path, rows)
    database = sqlite3.connect(path)
    try:
        database.execute("UPDATE pb_submission_cursors SET lookup_only=0")
        database.commit()
    finally:
        database.close()
    store = SqliteTransactionStore(path)
    try:
        with pytest.raises(StorageCorruption):
            store.submission_cursor(token, attempt)
    finally:
        store.close()
