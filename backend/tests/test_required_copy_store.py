"""Private and revision-bound required-copy persistence."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest

from parkingbreaker.intake_store import IntakeConflict
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.required_copy_store import (
    RequiredCopyStoreMixin,
    SavedRequiredCopies,
    initialize_required_copy_store,
)
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace

NOW = datetime(2026, 11, 2, tzinfo=UTC)
RESEARCH = '{"source":"official"}'
SHA = hashlib.sha256(RESEARCH.encode()).hexdigest()
ITEM = {"requirement_id": "a" * 64, "page_refs": ["photo_" + "b" * 64], "confirmed": True}
TEXT = json.dumps([ITEM])


class Copies(RequiredCopyStoreMixin):
    def __init__(self, database: sqlite3.Connection) -> None:
        self.database = database

    def _require_open(self) -> sqlite3.Connection:
        return self.database


def seed(path: Path) -> None:
    with SqliteTransactionStore(path) as store:
        db = store._require_open()
        initialize_required_copy_store(db)
        for key, owner in (("one", "alice"), ("two", "bob"), ("three", "alice")):
            store.save_workspace(key, Workspace(), NOW)
            store.set_workspace_owner(key, owner, NOW)
            store.record_intake(key, key, "{}", "{}", NOW)
            db.execute(
                "INSERT INTO pb_cases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    key,
                    key,
                    NOW.isoformat(),
                    NOW.isoformat(),
                    1999,
                    "RESEARCHING",
                    "AUTHORIZED",
                    1,
                    1,
                ),
            )
            db.execute("UPDATE pb_intake_records SET case_token=? WHERE intake_id=?", (key, key))
            store.save_research(key, RESEARCH, "{}", NOW, None)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "copies.sqlite"
    seed(path)
    with SqliteTransactionStore(path) as store:
        yield store._require_open()


def save(db: sqlite3.Connection, text: str = TEXT, revision: int = 0) -> SavedRequiredCopies:
    return Copies(db).save_required_copies("one", "alice", SHA, text, revision, NOW)


def test_restart_migration_and_private_cases(tmp_path: Path) -> None:
    path = tmp_path / "copies.sqlite"
    seed(path)
    with SqliteTransactionStore(path) as store:
        first = save(store._require_open())
        initialize_required_copy_store(store._require_open())
    with SqliteTransactionStore(path) as store:
        db = store._require_open()
        initialize_required_copy_store(db)
        copies = Copies(db)
        assert copies.get_required_copies("one", "alice") == first
        assert copies.get_required_copies("two", "bob") is None
        assert copies.get_required_copies("three", "alice") is None
        for key, owner in (("one", "bob"), ("missing", "alice")):
            with pytest.raises(IntakeConflict, match=r"^not_found$"):
                copies.get_required_copies(key, owner)
            with pytest.raises(IntakeConflict, match=r"^not_found$"):
                copies.save_required_copies(key, owner, SHA, TEXT, 0, NOW)


def test_revision_replay_remove_and_research(database: sqlite3.Connection) -> None:
    first = save(database)
    assert save(database) == first
    removed = save(database, "[]", 1)
    assert removed.revision == 2
    assert save(database, "[]", 1) == removed
    with pytest.raises(IntakeConflict, match="required_copies_revision_stale"):
        save(database)
    database.execute("UPDATE pb_intake_research SET research_json='{}' WHERE intake_id='one'")
    with pytest.raises(IntakeConflict, match="research_revision_stale"):
        save(database, "[]", 2)
    assert Copies(database).get_required_copies("one", "alice") == removed


@pytest.mark.parametrize(
    "state", ["AWAITING_ADOPTION", "FROZEN", "IN_TRANSIT", "STOPPED", "RETURNED"]
)
def test_locked_states_refuse_even_replay(database: sqlite3.Connection, state: str) -> None:
    save(database)
    database.execute("UPDATE pb_cases SET letter_state=? WHERE case_token='one'", (state,))
    with pytest.raises(IntakeConflict, match="required_copies_locked"):
        save(database)


@pytest.mark.parametrize("state", ["RESEARCHING", "NEEDS_CUSTOMER", "DRAFTING"])
def test_allowed_states(database: sqlite3.Connection, state: str) -> None:
    database.execute("UPDATE pb_cases SET letter_state=? WHERE case_token='one'", (state,))
    assert save(database).revision == 1


@pytest.mark.parametrize(
    "text",
    [
        "{}",
        "[1]",
        "[NaN]",
        json.dumps([dict(ITEM, confirmed=False)]),
        json.dumps([dict(ITEM, confirmed=1)]),
        json.dumps([dict(ITEM, secret="no")]),
        json.dumps([ITEM, ITEM]),
        json.dumps([dict(ITEM, page_refs=[])]),
        json.dumps([dict(ITEM, requirement_id="bad")]),
        json.dumps([dict(ITEM, page_refs=["photo_" + "b" * 64] * 11)]),
        json.dumps(
            [
                dict(ITEM, requirement_id=f"{i:064x}", page_refs=["photo_" + "b" * 64] * 10)
                for i in range(3)
            ]
        ),
    ],
)
def test_invalid_selections_fail_without_write(database: sqlite3.Connection, text: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        save(database, text)
    assert Copies(database).get_required_copies("one", "alice") is None


@pytest.mark.parametrize(
    ("column", "value"), [("selections_json", "{}"), ("research_sha256", "bad"), ("owner", "bob")]
)
def test_corrupt_records_refused(database: sqlite3.Connection, column: str, value: str) -> None:
    save(database)
    database.execute(f"UPDATE pb_required_copies SET {column}=? WHERE case_id='one'", (value,))
    with pytest.raises(StorageCorruption):
        Copies(database).get_required_copies("one", "alice")


def test_address_lock_serializes_with_other_connection(tmp_path: Path) -> None:
    path = tmp_path / "race.sqlite"
    seed(path)
    started = Event()

    def competing_save() -> None:
        with sqlite3.connect(path, isolation_level=None) as connection:
            started.set()
            with pytest.raises(IntakeConflict, match="required_copies_locked"):
                save(connection)

    with SqliteTransactionStore(path) as store, ThreadPoolExecutor(max_workers=1) as pool:
        db = store._require_open()
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO pb_address_locks VALUES ('one','alice',?)", (NOW.isoformat(),))
        future = pool.submit(competing_save)
        assert started.wait(5)
        db.execute("COMMIT")
        future.result(timeout=10)
        assert Copies(db).get_required_copies("one", "alice") is None


def test_two_connections_reject_stale_change(tmp_path: Path) -> None:
    path = tmp_path / "concurrency.sqlite"
    seed(path)
    with SqliteTransactionStore(path) as a, SqliteTransactionStore(path) as b:
        assert save(a._require_open()).revision == 1
        with pytest.raises(IntakeConflict, match="required_copies_revision_stale"):
            save(b._require_open(), "[]", 0)
        assert save(b._require_open(), "[]", 1).revision == 2


def test_address_lock_refuses_replay(database: sqlite3.Connection) -> None:
    save(database)
    database.execute("INSERT INTO pb_address_locks VALUES ('one','alice',?)", (NOW.isoformat(),))
    with pytest.raises(IntakeConflict, match="required_copies_locked"):
        save(database)


def test_existing_draft_blocks_documents(database: sqlite3.Connection) -> None:
    database.execute(
        "INSERT INTO pb_intake_drafts VALUES ('one','text','[]','[]',NULL,NULL,NULL,NULL,?,1)",
        (NOW.isoformat(),),
    )
    with pytest.raises(IntakeConflict, match="required_copies_locked"):
        save(database)


def test_mail_reservation_blocks_documents(database: sqlite3.Connection) -> None:
    database.execute(
        "INSERT INTO pb_mail_attempts VALUES ('one',1,X'00','submission','idempotency',?,?,'{}',?)",
        ("a" * 64, NOW.isoformat(), "b" * 64),
    )
    with pytest.raises(IntakeConflict, match="required_copies_locked"):
        save(database)


def test_no_started_case_refuses_write(database: sqlite3.Connection) -> None:
    database.execute("UPDATE pb_intake_records SET case_token=NULL WHERE intake_id='one'")
    with pytest.raises(IntakeConflict, match="required_copies_locked"):
        save(database)


@pytest.mark.parametrize("revision", [0, -1, "invalid"])
def test_corrupt_revision(database: sqlite3.Connection, revision: int | str) -> None:
    save(database)
    database.execute("PRAGMA ignore_check_constraints=ON")
    database.execute("UPDATE pb_required_copies SET revision=? WHERE case_id='one'", (revision,))
    with pytest.raises(StorageCorruption, match="invalid required copies record"):
        Copies(database).get_required_copies("one", "alice")


def test_maximum_supported_selection_and_duplicate_json_refusal(
    database: sqlite3.Connection,
) -> None:
    items = [dict(ITEM, requirement_id=f"{i:064x}") for i in range(20)]
    assert save(database, json.dumps(items)).revision == 1
    with pytest.raises(ValueError, match="invalid selections"):
        save(database, json.dumps([*items, dict(ITEM, requirement_id="f" * 64)]), 1)
    duplicate_key = (
        '[{"requirement_id":"'
        + "a" * 64
        + '","requirement_id":"'
        + "b" * 64
        + '","page_refs":["photo_'
        + "b" * 64
        + '"],"confirmed":true}]'
    )
    with pytest.raises(ValueError, match=r"."):
        save(database, duplicate_key, 1)


def test_schema_version_tampering_and_storage_errors_propagate(
    database: sqlite3.Connection,
) -> None:
    database.execute("UPDATE pb_store_versions SET version=9 WHERE component='required_copies'")
    with pytest.raises(StorageCorruption, match="invalid required copies schema"):
        initialize_required_copy_store(database)
    assert not database.in_transaction
    database.execute("DROP TABLE pb_required_copies")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        save(database)
    assert not database.in_transaction
