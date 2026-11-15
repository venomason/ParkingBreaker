from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.transaction_records import workspace_json
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
HISTORY_SIZE = 2048


def _seed_history(path: Path, size: int) -> tuple[Workspace, str]:
    workspace = Workspace(notes="last valid workspace")
    payload = workspace_json(workspace)
    timestamp = NOW.isoformat()
    with SqliteTransactionStore(path):
        pass
    with sqlite3.connect(path) as database:
        database.executemany(
            "INSERT INTO pb_workspace_history VALUES (?, ?, ?, ?)",
            (("workspace", revision, payload, timestamp) for revision in range(1, size + 1)),
        )
        database.execute(
            "INSERT INTO pb_workspaces VALUES (?, ?, ?, ?)",
            ("workspace", payload, timestamp, size),
        )
    return workspace, payload


class _CursorSpy:
    def __init__(self, cursor: sqlite3.Cursor, owner: _ConnectionSpy) -> None:
        self._cursor = cursor
        self._owner = owner

    def __iter__(self) -> _CursorSpy:
        return self

    def __next__(self) -> tuple[Any, ...]:
        row = next(self._cursor)
        self._owner.history_rows_read += 1
        return cast(tuple[Any, ...], row)

    def fetchall(self) -> list[tuple[Any, ...]]:
        if self._owner.is_history_query:
            raise AssertionError("workspace history must not use fetchall")
        return self._cursor.fetchall()

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        rows = self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)
        if self._owner.is_history_query:
            self._owner.history_rows_read += len(rows)
        return rows

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _ConnectionSpy:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.is_history_query = False
        self.history_reads = 0
        self.history_rows_read = 0

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> _CursorSpy | sqlite3.Cursor:
        cursor = self._connection.execute(sql, parameters)
        is_history_query = "FROM pb_workspace_history" in sql and "ORDER BY revision" in sql
        if is_history_query:
            self.history_reads += 1
            self.is_history_query = True
            return _CursorSpy(cursor, self)
        return cursor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _snapshot(path: Path) -> tuple[tuple[tuple[Any, ...], ...], tuple[tuple[Any, ...], ...]]:
    with sqlite3.connect(path) as database:
        current = tuple(database.execute("SELECT * FROM pb_workspaces").fetchall())
        history = tuple(
            database.execute("SELECT * FROM pb_workspace_history ORDER BY revision").fetchall()
        )
    return current, history


def test_workspace_write_streams_and_validates_every_history_record(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    _seed_history(path, HISTORY_SIZE)
    with SqliteTransactionStore(path) as store:
        connection = _ConnectionSpy(store._require_open())
        store._database = connection  # type: ignore[assignment]

        saved = store.save_workspace(
            "workspace", Workspace(notes="next workspace"), NOW + timedelta(minutes=1), HISTORY_SIZE
        )

        assert saved.revision == HISTORY_SIZE + 1
        assert connection.history_reads == 1
        assert connection.history_rows_read == HISTORY_SIZE
        assert store.get_workspace("workspace") == saved
    current, history = _snapshot(path)
    assert current[0][3] == HISTORY_SIZE + 1
    assert len(history) == HISTORY_SIZE + 1


def test_corrupt_middle_history_record_blocks_write_atomically(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    _seed_history(path, HISTORY_SIZE)
    with sqlite3.connect(path) as database:
        database.execute(
            "UPDATE pb_workspace_history SET payload_json='{}' WHERE revision=?",
            (HISTORY_SIZE // 2,),
        )
    before = _snapshot(path)

    with SqliteTransactionStore(path) as store, pytest.raises(StorageCorruption, match="workspace"):
        store.save_workspace(
            "workspace",
            Workspace(notes="must not persist"),
            NOW + timedelta(minutes=1),
            HISTORY_SIZE,
        )

    assert _snapshot(path) == before
