"""Pre-hold intake records survive restart and refuse conflicting writes."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from parkingbreaker.intake_store import IntakeConflict
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace

NOW = datetime(2026, 10, 15, 21, 0, tzinfo=UTC)


def store_with_workspace(path: Path, token: str = "ws_1") -> SqliteTransactionStore:
    store = SqliteTransactionStore(path)
    store.save_workspace(token, Workspace(notes="mine"), NOW)
    store.set_workspace_owner(token, "alice", NOW)
    return store


def test_intake_round_trip_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    store = store_with_workspace(path)
    record = store.record_intake("case_1", "ws_1", '{"i":1}', '{"p":1}', NOW)
    assert (record.owner, record.revision, record.selected_tier) == ("alice", 1, None)
    store.close()
    reopened = SqliteTransactionStore(path)
    assert reopened.get_intake("case_1") == record
    assert reopened.intake_for_workspace("ws_1") == record
    assert reopened.workspace_owner("ws_1") == "alice"
    assert reopened.workspace_owner("ws_other") is None


def test_identical_replay_returns_record_and_change_is_refused(tmp_path: Path) -> None:
    store = store_with_workspace(tmp_path / "pb.sqlite")
    first = store.record_intake("case_1", "ws_1", '{"i":1}', '{"p":1}', NOW)
    assert store.record_intake("case_2", "ws_1", '{"i":1}', '{"p":2}', NOW) == first
    with pytest.raises(IntakeConflict, match="intake_already_submitted"):
        store.record_intake("case_3", "ws_1", '{"i":2}', '{"p":1}', NOW)


def test_owner_is_fixed(tmp_path: Path) -> None:
    store = store_with_workspace(tmp_path / "pb.sqlite")
    store.set_workspace_owner("ws_1", "alice", NOW)
    with pytest.raises(IntakeConflict):
        store.set_workspace_owner("ws_1", "mallory", NOW)


def test_tier_and_case_lock_after_authorization(tmp_path: Path) -> None:
    store = store_with_workspace(tmp_path / "pb.sqlite")
    store.record_intake("case_1", "ws_1", "{}", "{}", NOW)
    chosen = store.update_intake("case_1", 1, selected_tier="CERTIFIED", case_token=None)
    started = store.update_intake("case_1", 2, selected_tier="CERTIFIED", case_token="case_1")
    assert (started.selected_tier, started.case_token, started.revision) == (
        "CERTIFIED",
        "case_1",
        3,
    )
    with pytest.raises(IntakeConflict, match="stale"):
        store.update_intake("case_1", chosen.revision, selected_tier=None, case_token=None)
    with pytest.raises(IntakeConflict, match="tier_locked"):
        store.update_intake("case_1", 3, selected_tier="STANDARD", case_token="case_1")


def test_unknown_intake_version_is_corruption(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    SqliteTransactionStore(path).close()
    database = sqlite3.connect(path)
    database.execute("UPDATE pb_store_versions SET version=9 WHERE component='intake'")
    database.commit()
    database.close()
    with pytest.raises(StorageCorruption):
        SqliteTransactionStore(path)
