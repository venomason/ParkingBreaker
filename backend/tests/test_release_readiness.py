"""Release readiness must fail closed unless the newest runtime evidence is genuine.

Every case uses a synthetic SQLite file and a real monitor receipt written by
``record_monitor_poll``; no deployment, network or provider is touched.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from parkingbreaker.health import (
    CaptureObservation,
    DiskObservation,
    HealthSnapshot,
    SweepObservation,
)
from parkingbreaker.release_readiness import (
    MAX_RECEIPT_BYTES,
    RECEIPT,
    ready,
    record_monitor_poll,
)

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
BOUNDARY = NOW - timedelta(minutes=10)
Row = tuple[str | None, str | None, str | None, int | None]


def healthy() -> HealthSnapshot:
    return HealthSnapshot(
        sweep=SweepObservation(NOW - timedelta(seconds=30), NOW - timedelta(seconds=20), "ok", ()),
        stuck=(),
        captures=CaptureObservation(40, 0),
        disk=DiskObservation(50 * 1024**3, 100 * 1024**3),
        last_backup_at=NOW - timedelta(hours=1),
        no_backup=False,
        oldest_webhook_backlog_at=None,
        webhook_backlog_known=True,
        holds=(),
    )


def blind() -> HealthSnapshot:
    return HealthSnapshot(None, None, None, None, None, False, None, False, None)


def database(path: Path, rows: Sequence[Row]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE pb_sweep_runs ("
            "run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, "
            "finished_at TEXT, outcome TEXT, processed INTEGER, failed INTEGER)"
        )
        connection.executemany(
            "INSERT INTO pb_sweep_runs (started_at, finished_at, outcome, processed, failed) "
            "VALUES (?, ?, ?, 0, ?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()
    return path


def ok_run(start: datetime, end: datetime) -> Row:
    return (start.isoformat(), end.isoformat(), "ok", 0)


def receive(
    data: Path,
    state: Path,
    started: datetime,
    completed: datetime,
    snapshot: HealthSnapshot | None = None,
) -> dict[str, object]:
    record_monitor_poll(
        healthy() if snapshot is None else snapshot, data, state, started, completed
    )
    return payload(state)


def payload(state: Path) -> dict[str, object]:
    raw: object = json.loads((state / RECEIPT).read_text())
    assert isinstance(raw, dict)
    return cast("dict[str, object]", raw)


def write(state: Path, value: dict[str, object]) -> None:
    (state / RECEIPT).write_text(json.dumps(value))


def evidence(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    data.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    return data, state


def ready_with_ok_run(tmp_path: Path) -> tuple[Path, Path]:
    data, state = evidence(tmp_path)
    database(
        data / "parkingbreaker.sqlite",
        [ok_run(BOUNDARY + timedelta(minutes=1), BOUNDARY + timedelta(minutes=2))],
    )
    receive(data, state, NOW - timedelta(seconds=10), NOW)
    return data, state


def test_genuine_completed_poll_and_post_launch_ok_sweep_are_ready(tmp_path: Path) -> None:
    data, state = ready_with_ok_run(tmp_path)
    assert ready(data, state, BOUNDARY, NOW) is True


@pytest.mark.parametrize(
    ("outcome", "failed"),
    [("failed", 1), ("partial", 0)],
)
def test_newer_failed_or_partial_run_is_never_hidden_by_an_older_success(
    tmp_path: Path, outcome: str, failed: int
) -> None:
    data, state = ready_with_ok_run(tmp_path)
    connection = sqlite3.connect(data / "parkingbreaker.sqlite")
    try:
        connection.execute(
            "INSERT INTO pb_sweep_runs (started_at, finished_at, outcome, processed, failed) "
            "VALUES (?, ?, ?, 1, ?)",
            (
                (BOUNDARY + timedelta(minutes=3)).isoformat(),
                (BOUNDARY + timedelta(minutes=4)).isoformat(),
                outcome,
                failed,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    assert ready(data, state, BOUNDARY, NOW) is False


def test_unfinished_newer_run_is_never_hidden_by_an_older_success(tmp_path: Path) -> None:
    data, state = ready_with_ok_run(tmp_path)
    connection = sqlite3.connect(data / "parkingbreaker.sqlite")
    try:
        connection.execute(
            "INSERT INTO pb_sweep_runs (started_at, finished_at, outcome, processed, failed) "
            "VALUES (?, NULL, NULL, 0, NULL)",
            ((BOUNDARY + timedelta(minutes=5)).isoformat(),),
        )
        connection.commit()
    finally:
        connection.close()
    assert ready(data, state, BOUNDARY, NOW) is False


def test_latest_ok_run_with_any_failure_is_not_ready(tmp_path: Path) -> None:
    data, state = ready_with_ok_run(tmp_path)
    connection = sqlite3.connect(data / "parkingbreaker.sqlite")
    try:
        connection.execute(
            "INSERT INTO pb_sweep_runs (started_at, finished_at, outcome, processed, failed) "
            "VALUES (?, ?, 'ok', 1, 1)",
            (
                (BOUNDARY + timedelta(minutes=3)).isoformat(),
                (BOUNDARY + timedelta(minutes=4)).isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    assert ready(data, state, BOUNDARY, NOW) is False


def test_latest_ok_run_without_finished_timestamp_fails_closed(tmp_path: Path) -> None:
    data, state = ready_with_ok_run(tmp_path)
    connection = sqlite3.connect(data / "parkingbreaker.sqlite")
    try:
        connection.execute(
            "INSERT INTO pb_sweep_runs (started_at, finished_at, outcome, processed, failed) "
            "VALUES (?, NULL, 'ok', 0, 0)",
            ((BOUNDARY + timedelta(minutes=3)).isoformat(),),
        )
        connection.commit()
    finally:
        connection.close()
    assert ready(data, state, BOUNDARY, NOW) is False


def test_failed_then_actual_new_ok_run_is_ready(tmp_path: Path) -> None:
    data, state = ready_with_ok_run(tmp_path)
    connection = sqlite3.connect(data / "parkingbreaker.sqlite")
    try:
        connection.execute(
            "INSERT INTO pb_sweep_runs (started_at, finished_at, outcome, processed, failed) "
            "VALUES (?, ?, 'failed', 1, 1)",
            (
                (BOUNDARY + timedelta(minutes=3)).isoformat(),
                (BOUNDARY + timedelta(minutes=4)).isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO pb_sweep_runs (started_at, finished_at, outcome, processed, failed) "
            "VALUES (?, ?, 'ok', 1, 0)",
            (
                (BOUNDARY + timedelta(minutes=5)).isoformat(),
                (BOUNDARY + timedelta(minutes=6)).isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    assert ready(data, state, BOUNDARY, NOW) is True


def test_absent_receipt_fails_closed(tmp_path: Path) -> None:
    data, state = evidence(tmp_path)
    database(
        data / "parkingbreaker.sqlite",
        [ok_run(BOUNDARY + timedelta(minutes=1), BOUNDARY + timedelta(minutes=2))],
    )
    assert ready(data, state, BOUNDARY, NOW) is False


@pytest.mark.parametrize(
    ("started", "completed"),
    [
        (NOW - timedelta(minutes=5), NOW - timedelta(seconds=151)),
        (NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)),
        (NOW - timedelta(seconds=1), NOW + timedelta(seconds=1)),
        (BOUNDARY - timedelta(seconds=1), BOUNDARY + timedelta(seconds=1)),
    ],
)
def test_stale_future_or_prelaunch_receipt_fails_closed(
    tmp_path: Path, started: datetime, completed: datetime
) -> None:
    data, state = evidence(tmp_path)
    database(
        data / "parkingbreaker.sqlite",
        [ok_run(BOUNDARY + timedelta(minutes=1), BOUNDARY + timedelta(minutes=2))],
    )
    receive(data, state, started, completed)
    assert ready(data, state, BOUNDARY, NOW) is False


@pytest.mark.parametrize("field", ["started_at", "completed_at"])
def test_naive_receipt_timestamp_fails_closed(tmp_path: Path, field: str) -> None:
    data, state = ready_with_ok_run(tmp_path)
    broken = payload(state)
    broken[field] = datetime(2026, 11, 8, 11, 59).isoformat()
    write(state, broken)
    assert ready(data, state, BOUNDARY, NOW) is False


def test_naive_boundary_or_now_fails_closed(tmp_path: Path) -> None:
    data, state = ready_with_ok_run(tmp_path)
    naive = datetime(2026, 11, 8, 11, 59)
    assert ready(data, state, naive, NOW) is False
    assert ready(data, state, BOUNDARY, naive) is False


def test_not_before_after_now_fails_closed(tmp_path: Path) -> None:
    data, state = ready_with_ok_run(tmp_path)
    assert ready(data, state, NOW + timedelta(seconds=1), NOW) is False


def test_blind_poll_removes_prior_receipt_and_fails_closed(tmp_path: Path) -> None:
    data, state = ready_with_ok_run(tmp_path)
    assert ready(data, state, BOUNDARY, NOW) is True
    record_monitor_poll(blind(), data, state, NOW, NOW + timedelta(seconds=1))
    assert not (state / RECEIPT).exists()
    assert ready(data, state, BOUNDARY, NOW) is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pid", 2**31),  # a PID no live process owns
        ("process_birth", "not-the-live-process-birth"),  # recycled PID
        ("pid", 0),
        ("pid", True),
    ],
)
def test_dead_or_recycled_process_identity_fails_closed(
    tmp_path: Path, field: str, value: object
) -> None:
    data, state = ready_with_ok_run(tmp_path)
    broken = payload(state)
    broken[field] = value
    write(state, broken)
    assert ready(data, state, BOUNDARY, NOW) is False


def test_replaced_database_same_path_fails_closed(tmp_path: Path) -> None:
    data, state = ready_with_ok_run(tmp_path)
    replacement = tmp_path / "replacement.sqlite"
    database(
        replacement,
        [ok_run(BOUNDARY + timedelta(minutes=1), BOUNDARY + timedelta(minutes=2))],
    )
    os.replace(replacement, data / "parkingbreaker.sqlite")
    assert ready(data, state, BOUNDARY, NOW) is False


def test_wrong_database_directory_fails_closed(tmp_path: Path) -> None:
    _data, state = ready_with_ok_run(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    database(
        other / "parkingbreaker.sqlite",
        [ok_run(BOUNDARY + timedelta(minutes=1), BOUNDARY + timedelta(minutes=2))],
    )
    assert ready(other, state, BOUNDARY, NOW) is False


@pytest.mark.parametrize(
    "corrupt",
    [
        b"{not json",
        b"[]",
        b'{"version": 1}',
        json.dumps({"version": 1, "observer": "x"}).encode(),
    ],
)
def test_malformed_receipt_fails_closed(tmp_path: Path, corrupt: bytes) -> None:
    data, state = ready_with_ok_run(tmp_path)
    (state / RECEIPT).write_bytes(corrupt)
    assert ready(data, state, BOUNDARY, NOW) is False


def test_extra_or_wrongly_typed_receipt_fields_fail_closed(tmp_path: Path) -> None:
    data, state = ready_with_ok_run(tmp_path)
    extra = payload(state)
    extra["extra"] = "value"
    write(state, extra)
    assert ready(data, state, BOUNDARY, NOW) is False

    for field, value in (
        ("version", 2),
        ("version", True),
        ("observer", "someone.else"),
        ("database_identity", "00:00"),
        ("started_at", 123),
    ):
        broken = payload(state)
        broken[field] = value
        write(state, broken)
        assert ready(data, state, BOUNDARY, NOW) is False, field


def test_oversized_receipt_fails_closed(tmp_path: Path) -> None:
    data, state = ready_with_ok_run(tmp_path)
    (state / RECEIPT).write_bytes(b"x" * (MAX_RECEIPT_BYTES + 1))
    assert ready(data, state, BOUNDARY, NOW) is False


def test_symlinked_receipt_fails_closed(tmp_path: Path) -> None:
    data, state = ready_with_ok_run(tmp_path)
    target = tmp_path / "elsewhere.json"
    target.write_text((state / RECEIPT).read_text())
    (state / RECEIPT).unlink()
    (state / RECEIPT).symlink_to(target)
    assert ready(data, state, BOUNDARY, NOW) is False


def test_missing_corrupt_database_or_table_fails_closed(tmp_path: Path) -> None:
    data, state = ready_with_ok_run(tmp_path)
    (data / "parkingbreaker.sqlite").unlink()
    assert ready(data, state, BOUNDARY, NOW) is False
    assert not (data / "parkingbreaker.sqlite").exists()

    database(
        data / "parkingbreaker.sqlite",
        [ok_run(BOUNDARY + timedelta(minutes=1), BOUNDARY + timedelta(minutes=2))],
    )
    receive(data, state, NOW - timedelta(seconds=10), NOW)
    (data / "parkingbreaker.sqlite").write_bytes(b"not a database")
    assert ready(data, state, BOUNDARY, NOW) is False

    tableless = tmp_path / "tableless"
    tableless.mkdir()
    connection = sqlite3.connect(tableless / "parkingbreaker.sqlite")
    try:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.commit()
    finally:
        connection.close()
    receive(tableless, state, NOW - timedelta(seconds=10), NOW)
    assert ready(tableless, state, BOUNDARY, NOW) is False


def test_ready_never_creates_the_database(tmp_path: Path) -> None:
    data, state = evidence(tmp_path)
    assert ready(data, state, BOUNDARY, NOW) is False
    assert not (data / "parkingbreaker.sqlite").exists()
    assert list(data.iterdir()) == []


def test_ready_leaves_database_bytes_and_case_rows_unchanged(tmp_path: Path) -> None:
    data, state = evidence(tmp_path)
    path = database(
        data / "parkingbreaker.sqlite",
        [ok_run(BOUNDARY + timedelta(minutes=1), BOUNDARY + timedelta(minutes=2))],
    )
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE pb_cases (case_token TEXT PRIMARY KEY, letter_state TEXT)")
        connection.execute("INSERT INTO pb_cases VALUES ('case_1', 'DRAFTING')")
        connection.commit()
    finally:
        connection.close()
    before = path.read_bytes()
    inode = path.stat().st_ino
    receive(data, state, NOW - timedelta(seconds=10), NOW)

    assert ready(data, state, BOUNDARY, NOW) is True
    assert path.read_bytes() == before
    assert path.stat().st_ino == inode
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT * FROM pb_cases").fetchall() == [("case_1", "DRAFTING")]
    finally:
        connection.close()
    assert [entry.name for entry in data.iterdir()] == ["parkingbreaker.sqlite"]
