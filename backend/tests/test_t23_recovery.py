"""Seeded online backup and isolated restore contracts, using actual customer storage."""

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from parkingbreaker import recovery
from parkingbreaker.blob_store import LocalBlobStore
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_blob_store import png
from test_submission_coordinator import prepared

PNG = png()


def test_online_backup_copies_wal_records_photos_and_embedded_pdf(tmp_path: Path) -> None:
    data = tmp_path / "authority"
    data.mkdir()
    ref = LocalBlobStore(data / "photos").put("synthetic-owner", PNG)
    with SqliteTransactionStore(data / "parkingbreaker.sqlite") as store:
        case = prepared(store)
        # Keep an open WAL connection and a durable customer-owned intake/packet.
        store.save_workspace("photo-workspace", Workspace(photo_refs=(ref,)), case.authorized_at)
        manifest = recovery.backup(data, tmp_path / "backup")
        assert manifest["records"]["pb_cases"]
        assert manifest["records"]["pb_intake_records"]
        assert list(manifest["artifacts"].values()).count(hashlib.sha256(PNG).hexdigest()) == 1
        assert any(name.startswith("pdfs/") for name in manifest["artifacts"])
        # Later authoritative changes must not enter the snapshot.
        store.save_workspace("later", Workspace(position="later exact words"), case.authorized_at)
    report = recovery.restore(tmp_path / "backup", tmp_path / "isolated")
    assert report["records_match"] is True
    assert report["artifacts_match"] is True
    assert report["elapsed_seconds"] >= 0
    assert (tmp_path / "isolated/RESTORE_QUARANTINE.json").is_file()
    with SqliteTransactionStore(tmp_path / "isolated/parkingbreaker.sqlite") as restored:
        assert restored.get_case(case.case_token) == case
        assert restored.get_workspace("later") is None
        packet = restored.get(case.case_token, 1)
        assert packet is not None
        assert hashlib.sha256(packet.request.pdf).hexdigest() in manifest["artifacts"].values()
    assert LocalBlobStore(tmp_path / "isolated/photos").get("synthetic-owner", ref) == PNG


def test_corrupt_restore_remains_isolated_and_preserves_backup(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with SqliteTransactionStore(data / "parkingbreaker.sqlite") as store:
        prepared(store)
    recovery.backup(data, tmp_path / "backup")
    db = tmp_path / "backup/parkingbreaker.sqlite"
    db.write_bytes(db.read_bytes() + b"corruption")
    before = db.read_bytes()
    with pytest.raises(ValueError, match="hash"):
        recovery.restore(tmp_path / "backup", tmp_path / "restore")
    assert db.read_bytes() == before
    assert (tmp_path / "restore/RESTORE_QUARANTINE.json").exists()
    assert '"event": "restore_failed"' in (tmp_path / "recovery-alerts.jsonl").read_text()


def test_missing_photo_and_replication_failure_keep_evidence(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    ref = "photo_" + "a" * 64
    with SqliteTransactionStore(data / "parkingbreaker.sqlite") as store:
        from test_transaction_lifecycle import NOW

        store.save_workspace("missing", Workspace(photo_refs=(ref,)), NOW)
    with pytest.raises(ValueError, match="photo"):
        recovery.backup(data, tmp_path / "incomplete")
    assert (tmp_path / "incomplete/INCOMPLETE").exists()
    assert (tmp_path / "incomplete/parkingbreaker.sqlite").exists()
    assert '"event": "backup_failed"' in (tmp_path / "recovery-alerts.jsonl").read_text()
    # A failed replica never deletes the source snapshot or old alerts.
    with pytest.raises(ValueError, match="incomplete"):
        recovery.replicate(tmp_path / "incomplete", tmp_path / "replica")
    alerts = [
        json.loads(line)["event"]
        for line in (tmp_path / "recovery-alerts.jsonl").read_text().splitlines()
    ]
    assert alerts == ["backup_failed", "replication_failed"]


def test_restore_refuses_existing_or_source_locations(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="isolated"):
        recovery.restore(tmp_path, tmp_path)
    assert tmp_path.exists()


def test_customer_row_tampering_is_detected_even_with_updated_db_hash(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with SqliteTransactionStore(data / "parkingbreaker.sqlite") as store:
        prepared(store)
    bundle = tmp_path / "bundle"
    recovery.backup(data, bundle)
    with sqlite3.connect(bundle / "parkingbreaker.sqlite") as db:
        db.execute("DELETE FROM pb_intake_records")
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["database_sha256"] = hashlib.sha256(
        (bundle / "parkingbreaker.sqlite").read_bytes()
    ).hexdigest()
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="record"):
        recovery.restore(bundle, tmp_path / "restore")


def test_exported_pdf_must_match_the_frozen_database_bytes(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with SqliteTransactionStore(data / "parkingbreaker.sqlite") as store:
        prepared(store)
    bundle = tmp_path / "bundle"
    recovery.backup(data, bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    pdf = next((bundle / "pdfs").iterdir())
    pdf.write_bytes(b"%PDF-tampered")
    manifest["artifacts"][pdf.relative_to(bundle).as_posix()] = hashlib.sha256(
        pdf.read_bytes()
    ).hexdigest()
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="PDF"):
        recovery.restore(bundle, tmp_path / "restore")


def test_backup_pins_records_while_photo_copy_overlaps_new_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_transaction_lifecycle import NOW

    data = tmp_path / "data"
    data.mkdir()
    ref = LocalBlobStore(data / "photos").put("synthetic-owner", PNG)
    original = recovery._write
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        store.save_workspace("original", Workspace(position="Exact words.", photo_refs=(ref,)), NOW)

        def concurrent_write(path: Path, payload: bytes) -> None:
            if path.name == ref:
                store.save_workspace("concurrent", Workspace(position="Another exact reason."), NOW)
            original(path, payload)

        monkeypatch.setattr(recovery, "_write", concurrent_write)
        recovery.backup(data, tmp_path / "bundle")
        assert store.get_workspace("concurrent") is not None
    monkeypatch.setattr(recovery, "_write", original)
    recovery.restore(tmp_path / "bundle", tmp_path / "restore")
    with SqliteTransactionStore(tmp_path / "restore" / recovery.DATABASE) as restored:
        assert restored.get_workspace("concurrent") is None
        assert restored.get_workspace("original") is not None


def test_another_owners_identical_photo_cannot_hide_missing_owned_bytes(tmp_path: Path) -> None:
    from test_transaction_lifecycle import NOW

    data = tmp_path / "authority"
    data.mkdir()
    ref = LocalBlobStore(data / "photos").put("wrong-owner", PNG)
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        store.create_owned_workspace(
            "right-owner", "customer-workspace", Workspace(photo_refs=(ref,)), NOW
        )
    with pytest.raises(ValueError, match="owned photo"):
        recovery.backup(data, tmp_path / "backup")


def test_replica_target_failure_does_not_overwrite_old_recovery_evidence(tmp_path: Path) -> None:
    data = tmp_path / "authority"
    data.mkdir()
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        prepared(store)
    bundle = tmp_path / "bundle"
    recovery.backup(data, bundle)
    replica = tmp_path / "old-replica"
    replica.mkdir()
    (replica / "old-evidence").write_bytes(b"keep exactly")
    with pytest.raises(ValueError, match="independent"):
        recovery.replicate(bundle, replica)
    assert (replica / "old-evidence").read_bytes() == b"keep exactly"
    assert recovery.verify(bundle)


def test_backup_after_unclean_stop_captures_committed_rows_left_only_in_the_wal(
    tmp_path: Path,
) -> None:
    """A killed API leaves committed rows in -wal; the online backup must still include them."""
    data = tmp_path / "authority"
    data.mkdir()
    seed = (
        "import os, sys\n"
        "from datetime import UTC, datetime\n"
        "from pathlib import Path\n"
        "from parkingbreaker.transaction_store import SqliteTransactionStore\n"
        "from parkingbreaker.workspace import Workspace\n"
        "s = SqliteTransactionStore(Path(sys.argv[1]) / 'parkingbreaker.sqlite')\n"
        "s.save_workspace('wal-only', Workspace(position='Exact words, kept.'),"
        " datetime(2026, 10, 22, tzinfo=UTC))\n"
        "os._exit(0)\n"  # no close, no checkpoint: the commit lives only in the WAL
    )
    subprocess.run([sys.executable, "-c", seed, str(data)], check=True)
    assert (data / "parkingbreaker.sqlite-wal").stat().st_size > 0
    recovery.backup(data, tmp_path / "bundle")
    recovery.restore(tmp_path / "bundle", tmp_path / "restored")
    with SqliteTransactionStore(tmp_path / "restored" / recovery.DATABASE) as restored:
        record = restored.get_workspace("wal-only")
    assert record is not None
    assert record.workspace.position == "Exact words, kept."


def test_backup_room_policy_boundaries() -> None:
    from parkingbreaker.recovery_policy import backup_room

    assert backup_room(free_bytes=3_000, needed_bytes=1_000, reserve_bytes=2_000)
    assert not backup_room(free_bytes=2_999, needed_bytes=1_000, reserve_bytes=2_000)
    assert not backup_room(free_bytes=0, needed_bytes=0, reserve_bytes=1)


def test_backup_never_fills_the_authority_disk_and_leaves_evidence_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backup that cannot fit must fail with an alert, before writing anything."""
    data = tmp_path / "authority"
    data.mkdir()
    LocalBlobStore(data / "photos").put("synthetic-owner", PNG)
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        prepared(store)
    alerts = tmp_path / "recovery-alerts.jsonl"
    alerts.write_text('{"event": "earlier"}\n')
    database = (data / recovery.DATABASE).read_bytes()

    class Full:
        free = 1024

    monkeypatch.setattr(shutil, "disk_usage", lambda _path: Full())
    with pytest.raises(ValueError, match="free space"):
        recovery.backup(data, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()
    assert alerts.read_text().splitlines() == [
        '{"event": "earlier"}',
        '{"event": "backup_insufficient_space"}',
    ]
    assert (data / recovery.DATABASE).read_bytes() == database


def test_backup_reserve_is_configurable_for_the_owner_cli(tmp_path: Path) -> None:
    data = tmp_path / "authority"
    data.mkdir()
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        prepared(store)
    command = [sys.executable, "-m", "parkingbreaker.recovery_cli", "backup", "--data", str(data)]
    refused = subprocess.run(
        [*command, "--output", str(tmp_path / "no"), "--reserve-bytes", str(1 << 62)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode == 1
    assert "backup_failed" in refused.stderr
    assert not (tmp_path / "no").exists()
    allowed = subprocess.run(
        [*command, "--output", str(tmp_path / "yes"), "--reserve-bytes", "0"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert allowed.returncode == 0, allowed.stderr
    assert (tmp_path / "yes" / "COMPLETE").is_file()
