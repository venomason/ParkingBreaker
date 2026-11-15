from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from parkingbreaker import retention_inventory as ri
from parkingbreaker.retention_inventory import (
    FileClass,
    InventoryRefusal,
    InventoryRefused,
    TableClass,
    inventory_snapshot,
)

SENTINEL = "SENTINEL-CUSTOMER-TEXT-7f3a9c"
ID_SENTINEL = "CUSTOMER-ID-8472"


def make_db(path: Path, *, rows: int = 0, unknown_rows: int = 0, wal: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE pb_draft_attempts (id INTEGER PRIMARY KEY, result_json TEXT)"
        )
        connection.execute("CREATE TABLE pb_store_versions (version INTEGER)")
        connection.execute("CREATE TABLE pb_payment_operations (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE pb_ai_journal (entry_json TEXT)")
        for _index in range(rows):
            connection.execute(
                "INSERT INTO pb_draft_attempts (result_json) VALUES (?)",
                (f'{{"text":"{SENTINEL}","owner":"{ID_SENTINEL}"}}',),
            )
        if unknown_rows:
            connection.execute('CREATE TABLE "weird ""unknown"" table" (value TEXT)')
            connection.executemany(
                'INSERT INTO "weird ""unknown"" table" (value) VALUES (?)',
                [(f"{SENTINEL}-{index}",) for index in range(unknown_rows)],
            )
        connection.commit()
    finally:
        connection.close()


def write(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def digest_tree(root: Path) -> tuple[dict[str, str], set[str]]:
    hashes: dict[str, str] = {}
    paths: set[str] = set()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        paths.add(relative)
        if path.is_file() and not path.is_symlink():
            hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes, paths


def tree_state(root: Path) -> tuple[dict[str, str], set[str], set[str]]:
    hashes, paths = digest_tree(root)
    sidecars = {p for p in paths if p.endswith(("-wal", "-shm", "-journal"))}
    return hashes, paths, sidecars


def test_sentinels_paths_and_sql_never_leak(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "snapshot"
    make_db(root / "parkingbreaker.sqlite", rows=2, unknown_rows=1)
    write(root / "customer-secret-name.txt", b"obviously secret")
    report = inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
    encoded = json.dumps(report.to_dict(), sort_keys=True)
    rendered = repr(report) + str(report.to_dict())
    out, err = capsys.readouterr()
    for text in (encoded, rendered, out, err):
        assert SENTINEL not in text
        assert ID_SENTINEL not in text
        assert str(root) not in text
        assert "parkingbreaker.sqlite" not in text
        assert "weird" not in text
        assert "SELECT" not in text.upper()
    assert report.file_count_for(FileClass.DECLARED_SQLITE_DATABASE) == 1
    assert report.file_count_for(FileClass.PROTECTED_UNKNOWN_COPY) == 1
    assert report.row_count_for(TableClass.PROTECTED_CUSTOMER_CONTENT) == 2
    assert report.row_count_for(TableClass.PROTECTED_UNKNOWN) == 1
    assert report.unknown_table_count == 1
    assert report.safety_status == "clear"


def test_large_counts_are_exact_and_not_truncated(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    first = root / "parkingbreaker.sqlite"
    make_db(first, rows=6007, unknown_rows=6001)
    second = root / "backup" / "parkingbreaker.sqlite"
    second.parent.mkdir(parents=True)
    shutil.copyfile(first, second)
    for index in range(2001):
        write(root / "photos" / ("a" * 64) / f"photo_{index:064x}", b"p")
        write(root / "nested" / f"scratch-{index}.tmp", b"t")
        write(root / "misc" / f"note-{index}.dat", b"u")
    write(root / "photo_blob", b"o")
    report = inventory_snapshot(
        root, database_paths=("parkingbreaker.sqlite", "backup/parkingbreaker.sqlite")
    )
    assert report.declared_database_count == 2
    assert report.inspected_database_count == 2
    assert report.inspected_database_count == 2
    assert report.table_count_for(TableClass.PROTECTED_CUSTOMER_CONTENT) == 2
    assert report.row_count_for(TableClass.PROTECTED_CUSTOMER_CONTENT) == 2 * 6007
    assert report.table_count_for(TableClass.PROTECTED_UNKNOWN) == 2
    assert report.row_count_for(TableClass.PROTECTED_UNKNOWN) == 2 * 6001
    assert report.file_count_for(FileClass.OWNER_SCOPED_PHOTO_BLOB) == 2001
    assert report.file_count_for(FileClass.TEMPORARY_COPY_CANDIDATE) == 2001
    assert report.file_count_for(FileClass.PROTECTED_UNKNOWN_COPY) == 2002
    assert report.file_count == 2001 + 2001 + 2001 + 1 + 1 + 1
    assert report.directory_count == 5


def test_classes_and_policy_fields(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    make_db(root / "parkingbreaker.sqlite")
    write(root / "pdfs" / ("b" * 64 + ".pdf"), b"%PDF")
    write(root / "manifest.json", b"{}")
    write(root / "photos" / ("c" * 64) / ("photo_" + "d" * 64), b"JPG")
    write(root / "unknown.sqlite", b"x")
    write(root / "orphan-wal", b"")
    write(root / "orphan-shm", b"")
    write(root / "orphan-journal", b"")
    report = inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
    assert report.file_count_for(FileClass.EXPORTED_FROZEN_PDF) == 1
    assert report.file_count_for(FileClass.BACKUP_CONTROL_METADATA) == 1
    assert report.file_count_for(FileClass.OWNER_SCOPED_PHOTO_BLOB) == 1
    assert report.file_count_for(FileClass.UNINSPECTED_SQLITE_COPY_CANDIDATE) == 1
    assert report.file_count_for(FileClass.SQLITE_WAL) == 1
    assert report.file_count_for(FileClass.SQLITE_SHM) == 1
    assert report.file_count_for(FileClass.SQLITE_ROLLBACK_JOURNAL) == 1
    assert report.inventory_scope == "supplied_snapshot_root"
    assert report.retention_policy_activated is False
    assert report.purge_eligible is False
    assert report.closure_evaluated is False
    assert report.external_provider_copies == "not_inspected"
    assert report.out_of_scope_copies == "not_inspected"
    assert report.schema_version == 1
    assert report.table_count_for(TableClass.RESTRICTED_FINANCIAL_PROVIDER_EVIDENCE) == 1
    assert report.table_count_for(TableClass.RESTRICTED_IDENTITY_OPERATIONAL) == 1
    assert report.table_count_for(TableClass.STRUCTURAL_METADATA) == 1
    assert report.row_count_for(TableClass.PROTECTED_UNKNOWN) == 0


def test_without_rowid_and_quoted_unknown_table(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    database = sqlite3.connect(root / "parkingbreaker.sqlite")
    try:
        database.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID")
        database.executemany("INSERT INTO kv VALUES (?,?)", [(str(i), "v") for i in range(4)])
        database.execute('CREATE TABLE "has "" quote" (x)')
        database.execute('INSERT INTO "has "" quote" VALUES (1)')
        database.commit()
    finally:
        database.close()
    report = inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
    assert report.unknown_table_count == 2
    assert report.row_count_for(TableClass.PROTECTED_UNKNOWN) == 5
    assert report.safety_status == "clear"


@pytest.mark.parametrize(
    "bad",
    [
        "/absolute.sqlite",
        "../escape.sqlite",
        "a/../b.sqlite",
        "a//b.sqlite",
        "./a.sqlite",
        "file:parkingbreaker.sqlite",
        "parkingbreaker.sqlite?mode=ro",
        "parkingbreaker.sqlite#frag",
        "a\x00.sqlite",
        "",
    ],
)
def test_invalid_database_paths_refused(tmp_path: Path, bad: str) -> None:
    root = tmp_path / "snapshot"
    make_db(root / "parkingbreaker.sqlite")
    with pytest.raises(InventoryRefusal) as caught:
        inventory_snapshot(root, database_paths=(bad,))
    assert caught.value.reason is InventoryRefused.INVALID_DATABASE_PATH
    assert str(caught.value) == "invalid_database_path"


def test_duplicate_missing_and_relative_root(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    make_db(root / "parkingbreaker.sqlite")
    with pytest.raises(InventoryRefusal) as duplicate:
        inventory_snapshot(root, database_paths=("parkingbreaker.sqlite", "parkingbreaker.sqlite"))
    assert duplicate.value.reason is InventoryRefused.DUPLICATE_DATABASE_PATH
    with pytest.raises(InventoryRefusal) as missing:
        inventory_snapshot(root, database_paths=("missing/parkingbreaker.sqlite",))
    assert missing.value.reason is InventoryRefused.MISSING_DATABASE
    with pytest.raises(InventoryRefusal) as relative:
        inventory_snapshot(Path("relative"), database_paths=())
    assert relative.value.reason is InventoryRefused.ROOT_NOT_ABSOLUTE


def test_symlinks_and_hardlinks_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    make_db(root / "parkingbreaker.sqlite")
    (root / "child").mkdir()
    (root / "child" / "link").symlink_to(root / "child")
    with pytest.raises(InventoryRefusal) as caught:
        inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
    assert caught.value.reason is InventoryRefused.SYMLINK_IN_PATH

    other = tmp_path / "other"
    make_db(other / "parkingbreaker.sqlite")
    root_link = tmp_path / "rootlink"
    root_link.symlink_to(other)
    with pytest.raises(InventoryRefusal):
        inventory_snapshot(root_link, database_paths=("parkingbreaker.sqlite",))

    ancestor = tmp_path / "ancestor"
    ancestor.mkdir()
    (ancestor / "sub").symlink_to(other)
    with pytest.raises(InventoryRefusal):
        inventory_snapshot(ancestor / "sub", database_paths=("parkingbreaker.sqlite",))


def test_db_symlink_nested_photo_symlink_and_hardlink(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    make_db(root / "parkingbreaker.sqlite")
    (root / "alias.sqlite").symlink_to(root / "parkingbreaker.sqlite")
    with pytest.raises(InventoryRefusal) as caught:
        inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
    assert caught.value.reason is InventoryRefused.SYMLINK_IN_PATH

    clean = tmp_path / "clean"
    make_db(clean / "parkingbreaker.sqlite")
    (clean / "photos").mkdir()
    (clean / "photos" / "link").symlink_to(clean / "photos")
    with pytest.raises(InventoryRefusal):
        inventory_snapshot(clean, database_paths=("parkingbreaker.sqlite",))

    linked = tmp_path / "linked"
    make_db(linked / "parkingbreaker.sqlite")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    os.link(outside, linked / "smuggled.txt")
    with pytest.raises(InventoryRefusal) as hard:
        inventory_snapshot(linked, database_paths=("parkingbreaker.sqlite",))
    assert hard.value.reason is InventoryRefused.HARDLINKED_ENTRY


def test_fifo_and_socket_refused(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    make_db(root / "parkingbreaker.sqlite")
    os.mkfifo(root / "pipe")
    with pytest.raises(InventoryRefusal) as caught:
        inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
    assert caught.value.reason is InventoryRefused.UNSAFE_ENTRY

    second = tmp_path / "second"
    make_db(second / "parkingbreaker.sqlite")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(second / "sock"))
        with pytest.raises(InventoryRefusal) as caught:
            inventory_snapshot(second, database_paths=("parkingbreaker.sqlite",))
        assert caught.value.reason is InventoryRefused.UNSAFE_ENTRY
    finally:
        server.close()


def test_wal_writer_blocks_inspection_and_creates_nothing(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    path = root / "parkingbreaker.sqlite"
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE pb_draft_attempts (result_json TEXT)")
        writer.execute("INSERT INTO pb_draft_attempts VALUES ('committed-in-wal')")
        writer.commit()
        assert (root / "parkingbreaker.sqlite-wal").exists()
        report = inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
        assert report.inspected_database_count == 0
        assert report.uninspected_database_count == 1
        assert report.safety_status == "blocked"
        assert report.inventory_complete is False
        assert "database_inspection_blocked" in report.reason_codes
        assert report.row_count_for(TableClass.PROTECTED_CUSTOMER_CONTENT) == 0
        assert report.file_count_for(FileClass.SQLITE_WAL) == 1
        assert (root / "parkingbreaker.sqlite-wal").exists()
        assert (root / "parkingbreaker.sqlite-shm").exists()
    finally:
        writer.close()


def test_completed_standalone_snapshot_counts_exact_and_is_untouched(tmp_path: Path) -> None:
    live = tmp_path / "live"
    live.mkdir()
    source = sqlite3.connect(live / "parkingbreaker.sqlite")
    try:
        source.execute("CREATE TABLE pb_draft_attempts (result_json TEXT)")
        source.executemany("INSERT INTO pb_draft_attempts VALUES (?)", [("a",), ("b",), ("c",)])
        source.commit()
        root = tmp_path / "snapshot"
        root.mkdir()
        destination = sqlite3.connect(root / "parkingbreaker.sqlite")
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    before = tree_state(root)
    report = inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
    after = tree_state(root)
    assert before == after
    assert report.row_count_for(TableClass.PROTECTED_CUSTOMER_CONTENT) == 3
    assert report.inspected_database_count == 1
    assert report.safety_status == "clear"


def test_wal_sidecar_marker_and_incomplete_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    make_db(root / "parkingbreaker.sqlite")
    (root / "parkingbreaker.sqlite-wal").write_bytes(b"")
    report = inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
    assert report.uninspected_database_count == 1
    assert report.safety_status == "blocked"
    assert report.file_count_for(FileClass.SQLITE_WAL) == 1

    marker_root = tmp_path / "marker"
    make_db(marker_root / "parkingbreaker.sqlite")
    (marker_root / "INCOMPLETE").write_bytes(b"")
    blocked = inventory_snapshot(marker_root, database_paths=("parkingbreaker.sqlite",))
    assert blocked.inspected_database_count == 0
    assert blocked.safety_status == "blocked"
    assert blocked.file_count_for(FileClass.BACKUP_CONTROL_METADATA) == 1


def test_deterministic_mutation_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "snapshot"
    make_db(root / "parkingbreaker.sqlite", rows=1)
    original = ri._inspect_database

    def mutating(fd: int, expected: ri._StatKey) -> ri._DatabaseTally:
        result = original(fd, expected)
        (root / "appeared.txt").write_bytes(b"new")
        return result

    monkeypatch.setattr(ri, "_inspect_database", mutating)
    with pytest.raises(InventoryRefusal) as caught:
        inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
    assert caught.value.reason is InventoryRefused.CONCURRENT_MUTATION


def test_database_replacement_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "snapshot"
    make_db(root / "parkingbreaker.sqlite", rows=1)
    original = ri._inspect_database

    def replacing(fd: int, expected: ri._StatKey) -> ri._DatabaseTally:
        result = original(fd, expected)
        fresh = root / "fresh.sqlite"
        make_db(fresh, rows=1)
        os.replace(fresh, root / "parkingbreaker.sqlite")
        return result

    monkeypatch.setattr(ri, "_inspect_database", replacing)
    with pytest.raises(InventoryRefusal) as caught:
        inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
    assert caught.value.reason is InventoryRefused.CONCURRENT_MUTATION


def test_virtual_view_and_malformed(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    database = sqlite3.connect(root / "parkingbreaker.sqlite")
    try:
        database.execute("CREATE TABLE base (x)")
        database.execute("INSERT INTO base VALUES (1)")
        database.execute("CREATE VIEW looked AS SELECT * FROM base")
        database.execute("CREATE VIRTUAL TABLE searched USING fts5(body)")
        database.execute("INSERT INTO searched VALUES ('needle')")
        database.commit()
    finally:
        database.close()
    report = inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
    assert report.unknown_schema_object_count == 2
    assert report.row_count_unavailable_count == 2
    assert report.safety_status == "blocked"
    assert report.inventory_complete is False
    assert report.row_count_for(TableClass.PROTECTED_CUSTOMER_CONTENT) == 0

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "parkingbreaker.sqlite").write_bytes(b"this is not a database")
    with pytest.raises(InventoryRefusal) as caught:
        inventory_snapshot(broken, database_paths=("parkingbreaker.sqlite",))
    assert caught.value.reason is InventoryRefused.MALFORMED_DATABASE
    assert str(caught.value) == "malformed_database"


def test_failed_inventory_leaves_fixture_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    make_db(root / "parkingbreaker.sqlite", rows=2)
    write(root / "unknown.bin", b"payload")
    before = tree_state(root)
    with pytest.raises(InventoryRefusal):
        inventory_snapshot(root, database_paths=("missing.sqlite",))
    after = tree_state(root)
    assert before == after
    assert not (root / "parkingbreaker.sqlite-wal").exists()
    assert not (root / "parkingbreaker.sqlite-shm").exists()

    report = inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
    assert report.row_count_for(TableClass.PROTECTED_CUSTOMER_CONTENT) == 2
    assert tree_state(root)[0] == before[0]
    assert tree_state(root)[1] == before[1]
    assert not (root / "parkingbreaker.sqlite-wal").exists()
    assert not (root / "parkingbreaker.sqlite-shm").exists()


def test_lifecycle_rows_never_imply_closure_or_eligibility(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    database = sqlite3.connect(root / "parkingbreaker.sqlite")
    try:
        database.execute("CREATE TABLE pb_cases (state TEXT)")
        database.executemany(
            "INSERT INTO pb_cases VALUES (?)",
            [("DELIVERED",), ("RETURNED",), ("CLOSED",), ("UNKNOWN",)],
        )
        database.commit()
    finally:
        database.close()
    report = inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
    assert report.row_count_for(TableClass.RESTRICTED_IDENTITY_OPERATIONAL) == 4
    assert report.closure_evaluated is False
    assert report.purge_eligible is False
    assert report.retention_policy_activated is False
    serialized = json.dumps(report.to_dict())
    assert "DELIVERED" not in serialized


def test_unsupported_basename_refused_before_any_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "snapshot"
    make_db(root / "auth.json")
    calls: list[str] = []

    def sentinel(*_args: object, **_kwargs: object) -> object:
        calls.append("opened")
        raise AssertionError("unsupported declaration must not be opened")

    monkeypatch.setattr(ri, "_open_root", sentinel)
    monkeypatch.setattr(ri, "_inspect_database", sentinel)
    with pytest.raises(InventoryRefusal) as caught:
        inventory_snapshot(root, database_paths=("auth.json",))
    assert caught.value.reason is InventoryRefused.UNSUPPORTED_DATABASE_BASENAME
    assert str(caught.value) == "unsupported_database_basename"
    assert calls == []


def test_undeclared_unsupported_named_copy_is_metadata_only(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    make_db(root / "parkingbreaker.sqlite", rows=1)
    make_db(root / "auth.json")
    report = inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
    assert report.inspected_database_count == 1
    assert report.declared_database_count == 1
    assert report.file_count_for(FileClass.PROTECTED_UNKNOWN_COPY) == 1
    assert report.safety_status == "clear"


def test_internal_sqlite_tables_counted_protected_unknown(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    database = sqlite3.connect(root / "parkingbreaker.sqlite")
    try:
        database.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)")
        database.execute("INSERT INTO sample (value) VALUES ('one')")
        database.execute("CREATE TABLE measured (x INTEGER)")
        database.execute("INSERT INTO measured VALUES (1)")
        database.execute("ANALYZE")
        database.commit()
    finally:
        database.close()
    report = inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
    # sample, measured, sqlite_sequence and sqlite_stat1; sqlite_schema is the catalog outline.
    assert report.table_count_for(TableClass.PROTECTED_UNKNOWN) == 4
    assert report.unknown_table_count == 4
    assert report.row_count_for(TableClass.PROTECTED_UNKNOWN) == 5
    assert report.safety_status == "clear"


def test_directory_replacement_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "snapshot"
    make_db(root / "parkingbreaker.sqlite", rows=1)
    empty = root / "empty"
    empty.mkdir()
    original = ri._inspect_database

    def replacing(fd: int, expected: ri._StatKey) -> ri._DatabaseTally:
        result = original(fd, expected)
        os.rename(empty, tmp_path / "moved-empty")
        empty.mkdir()
        return result

    monkeypatch.setattr(ri, "_inspect_database", replacing)
    with pytest.raises(InventoryRefusal) as caught:
        inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
    assert caught.value.reason is InventoryRefused.CONCURRENT_MUTATION


def test_root_directory_replacement_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "snapshot"
    make_db(root / "parkingbreaker.sqlite", rows=1)
    original = ri._inspect_database

    def replacing(fd: int, expected: ri._StatKey) -> ri._DatabaseTally:
        result = original(fd, expected)
        os.rename(root, tmp_path / "moved-root")
        root.mkdir()
        make_db(root / "parkingbreaker.sqlite")
        return result

    monkeypatch.setattr(ri, "_inspect_database", replacing)
    with pytest.raises(InventoryRefusal) as caught:
        inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
    assert caught.value.reason is InventoryRefused.CONCURRENT_MUTATION


def test_fifo_swap_refused_immediately_in_bounded_subprocess(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "src"
    script = """
import os, sys, sqlite3
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import parkingbreaker.retention_inventory as ri

root = Path(sys.argv[2])
root.mkdir()
db = root / "parkingbreaker.sqlite"
connection = sqlite3.connect(db)
connection.execute("CREATE TABLE sample (x)")
connection.execute("INSERT INTO sample VALUES (1)")
connection.commit()
connection.close()

original = ri._open_pinned
state = {"swapped": False}
def swapping(root_fd, relative):
    if not state["swapped"]:
        state["swapped"] = True
        os.rename(db, root / "aside.sqlite.bak")
        os.mkfifo(db)
    return original(root_fd, relative)
ri._open_pinned = swapping
try:
    ri.inventory_snapshot(root, database_paths=("parkingbreaker.sqlite",))
except ri.InventoryRefusal as error:
    print(error.reason.value)
else:
    print("accepted")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(source), str(tmp_path / "snapshot")],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "unsafe_entry"
