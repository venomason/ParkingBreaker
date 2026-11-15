"""F01b: notice-only photos must exist under the validated actual case owner.

Real accepted NO_CHARGE cases, real SQLite backup/restore and the actual owner-scoped
LocalBlobStore. Notice refs are deliberately absent from workspace/history/intake JSON,
so only the F01 notice traversal can make them required. Assertions report categories,
counts and opaque hashes; never payload text, identifiers, refs or bytes.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from parkingbreaker import recovery
from parkingbreaker.beta_feedback_store import NoticePage, NoticeReceipt
from parkingbreaker.blob_store import LocalBlobStore
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_beta_admission import NOW
from test_beta_recovery import _beta_ready, _reconcile
from test_blob_store import png

OWNER = "owner-1"
OTHER = "owner-2"
MISSING_REF = "photo_" + "d" * 64
SENTINEL = "SENTINEL-PRIVATE-NOTICE-TEXT"


def _folder(owner: str) -> str:
    return hashlib.sha256(owner.encode()).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _db_hash(data: Path) -> str:
    return _file_hash(data / recovery.DATABASE)


def _notice_case(tmp_path: Path) -> tuple[Path, Path, object]:
    data, bundle, case, _packet = _beta_ready(tmp_path)
    return data, bundle, case


def _record(
    data: Path,
    request_id: str,
    pages: tuple[NoticePage, ...],
    *,
    correction_of: str | None = None,
    customer_seen_on: str | None = None,
    printed_on: str | None = None,
    stated_deadline_text: str | None = None,
) -> NoticeReceipt:
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        return store.record_beta_feedback_notice(
            "case_1",
            OWNER,
            request_id,
            pages,
            NOW,
            correction_of=correction_of,
            customer_seen_on=customer_seen_on,
            printed_on=printed_on,
            stated_deadline_text=stated_deadline_text,
        )


def test_notice_only_photos_roundtrip_restore_and_fake_reconcile(tmp_path: Path) -> None:
    data, bundle, _case = _notice_case(tmp_path)
    photos = LocalBlobStore(data / "photos")
    envelope = photos.put(OWNER, png("white"))
    page_one = photos.put(OWNER, png("black"))
    page_two = photos.put(OWNER, png("red"))
    original_only = photos.put(OWNER, png("green"))

    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        original = store.record_beta_feedback_notice(
            "case_1",
            OWNER,
            "req-original",
            (NoticePage("envelope", envelope), NoticePage("page", original_only)),
            NOW,
            customer_seen_on="2026-11-01",
            printed_on="2026-11-02",
            stated_deadline_text=SENTINEL,
        )
        correction = store.record_beta_feedback_notice(
            "case_1",
            OWNER,
            "req-correction",
            (
                NoticePage("envelope", envelope),
                NoticePage("page", page_one),
                NoticePage("page", page_two),
            ),
            NOW,
            correction_of=original.notice_id,
            stated_deadline_text=SENTINEL,
        )
        # Prove notice-only: no legacy payload mentions any of these refs.
        db = store._require_open()
        legacy: list[str] = []
        for table, column in (
            ("pb_workspaces", "payload_json"),
            ("pb_workspace_history", "payload_json"),
            ("pb_intake_records", "intake_json"),
            ("pb_intake_records", "prehold_json"),
            ("pb_intake_records", "agency_json"),
        ):
            legacy.extend(row[0] for row in db.execute(f"SELECT {column} FROM {table}"))
        saved_refs = (envelope, page_one, page_two, original_only)
        assert all(ref not in text for ref in saved_refs for text in legacy)
        recovery.backup(data, bundle)

    manifest = recovery.verify(bundle)
    for ref in saved_refs:
        assert f"photos/{_folder(OWNER)}/{ref}" in manifest["artifacts"]
    assert manifest["artifacts"]

    restored = tmp_path / "restored"
    payments = FakePaymentClient()
    lob = FakeLobClient([])
    report = _reconcile(bundle, lob, payments, restored)
    assert report["status"] == "READY_FAKE"
    assert lob.calls == []
    assert payments.calls == []
    assert (restored / "FAKE_RECOVERY_ONLY").is_file()

    with SqliteTransactionStore(restored / recovery.DATABASE) as store:
        kept = store.list_beta_feedback_notices("case_1", OWNER).notices
        assert [item.request_id for item in kept] == ["req-original", "req-correction"]
        assert kept[0].pages == (
            NoticePage("envelope", envelope),
            NoticePage("page", original_only),
        )
        assert kept[1].pages == (
            NoticePage("envelope", envelope),
            NoticePage("page", page_one),
            NoticePage("page", page_two),
        )
        assert kept[1].correction_of == original.notice_id
        assert kept[0].customer_seen_on == "2026-11-01"
        assert kept[0].printed_on == "2026-11-02"
        assert kept[0].stated_deadline_text == SENTINEL
        assert kept[0].received_at == original.received_at
        assert kept[1].received_at == correction.received_at

    restored_photos = LocalBlobStore(restored / "photos")
    assert restored_photos.get(OWNER, envelope) == png("white")
    assert restored_photos.get(OWNER, original_only) == png("green")
    assert restored_photos.get(OTHER, envelope) is None
    assert restored_photos.get(OWNER, page_two) == png("red")
    recovery.verify(bundle)  # unchanged authority bundle


def test_missing_notice_photo_refuses_and_retains_evidence(tmp_path: Path) -> None:
    data, bundle, _case = _notice_case(tmp_path)
    photos = LocalBlobStore(data / "photos")
    available = photos.put(OWNER, png("white"))
    _record(
        data,
        "req-missing",
        (NoticePage("envelope", available), NoticePage("page", MISSING_REF)),
    )
    before_db = _db_hash(data)
    before_notices = SqliteTransactionStore(data / recovery.DATABASE).list_beta_feedback_notices(
        "case_1", OWNER
    )
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        before_rows = (
            store._require_open()
            .execute(
                "SELECT sequence, payload_sha256 FROM pb_beta_feedback_notices ORDER BY sequence"
            )
            .fetchall()
        )
    with pytest.raises(ValueError, match="referenced photo missing") as failure:
        recovery.backup(data, bundle)
    assert "referenced photo missing" in str(failure.value)
    assert (bundle / "INCOMPLETE").is_file()
    assert not (bundle / "COMPLETE").exists()
    assert _db_hash(data) == before_db
    assert photos.get(OWNER, available) == png("white")
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        after_rows = (
            store._require_open()
            .execute(
                "SELECT sequence, payload_sha256 FROM pb_beta_feedback_notices ORDER BY sequence"
            )
            .fetchall()
        )
    assert after_rows == before_rows
    assert (
        SqliteTransactionStore(data / recovery.DATABASE).list_beta_feedback_notices("case_1", OWNER)
        == before_notices
    )


def test_wrong_owner_only_photo_refuses_despite_global_basename_match(tmp_path: Path) -> None:
    """The key RED/GREEN: one ref, same bytes, only under the wrong owner directory."""
    data, bundle, _case = _notice_case(tmp_path)
    photos = LocalBlobStore(data / "photos")
    foreign = photos.put(OTHER, png("black"))
    _record(data, "req-foreign", (NoticePage("page", foreign),))
    before_db = _db_hash(data)
    foreign_path = data / "photos" / _folder(OTHER) / foreign
    before_photo = _file_hash(foreign_path)
    # Generic global ref/basename discovery is satisfied by the foreign artifact.
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        _records, refs = recovery.record_hashes(store._require_open())
    assert foreign in refs
    with pytest.raises(ValueError, match="referenced owned photo missing") as failure:
        recovery.backup(data, bundle)
    assert "referenced owned photo missing" in str(failure.value)
    assert (bundle / "INCOMPLETE").is_file()
    assert _db_hash(data) == before_db
    assert _file_hash(foreign_path) == before_photo
    assert not (data / "photos" / _folder(OWNER) / foreign).exists()


def test_wrong_owner_bundle_rejected_independently_of_manifest(tmp_path: Path) -> None:
    data, bundle, _case = _notice_case(tmp_path)
    photos = LocalBlobStore(data / "photos")
    ref = photos.put(OWNER, png("white"))
    _record(data, "req-bundle", (NoticePage("page", ref),))
    recovery.backup(data, bundle)
    recovery.verify(bundle)

    corrupted = tmp_path / "corrupted"
    shutil.copytree(bundle, corrupted)
    source = corrupted / "photos" / _folder(OWNER) / ref
    destination = corrupted / "photos" / _folder(OTHER) / ref
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
    manifest = recovery._manifest(corrupted)
    manifest["artifacts"] = recovery._artifacts(corrupted)
    (corrupted / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    # Manifest and generic basename/hash checks are internally consistent...
    with pytest.raises(ValueError, match="referenced owned photo missing") as failure:
        recovery.verify(corrupted)
    assert "referenced owned photo missing" in str(failure.value)
    # ...while the untouched valid bundle still verifies exactly.
    recovery.verify(bundle)


def test_invalid_notice_binding_and_version_fail_closed_sanitized(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data, bundle, _case = _notice_case(tmp_path)
    photos = LocalBlobStore(data / "photos")
    bad_ref = photos.put(OWNER, png("blue"))
    payload = {
        "case_id": "case_1",
        "owner": OTHER,
        "request_id": "req-bad",
        "pages": [{"role": "page", "photo_ref": bad_ref}],
        "customer_seen_on": None,
        "printed_on": None,
        "stated_deadline_text": SENTINEL,
        "correction_of": None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        db = store._require_open()
        db.execute(
            "INSERT INTO pb_beta_feedback_notices "
            "(case_id, owner, notice_id, request_id, received_at, payload_json, "
            "payload_sha256, correction_of, needs_confirmation, review_state) "
            "VALUES ('case_1', ?, ?, 'req-bad', ?, ?, ?, NULL, 1, 'unreviewed')",
            (OTHER, "a" * 32, NOW.astimezone().isoformat(), encoded, digest),
        )
        db.commit()
    with pytest.raises(ValueError, match=r"^invalid notice photo references$") as failure:
        recovery.backup(data, bundle)
    assert str(failure.value) == "invalid notice photo references"
    assert SENTINEL not in str(failure.value)
    captured = capsys.readouterr()
    assert SENTINEL not in captured.err
    assert SENTINEL not in captured.out
    assert "backup_failed" in captured.err
    assert (bundle / "INCOMPLETE").is_file()

    versioned = tmp_path / "versioned"
    versioned.mkdir()
    data2, bundle2, _case2 = _notice_case(versioned)
    with SqliteTransactionStore(data2 / recovery.DATABASE) as store:
        db = store._require_open()
        db.execute("UPDATE pb_store_versions SET version=2 WHERE component='beta_feedback'")
        db.commit()
    with pytest.raises(ValueError, match="invalid notice photo references"):
        recovery.backup(data2, bundle2)
    assert (bundle2 / "INCOMPLETE").is_file()
    assert _db_hash(data2)  # readable snapshot preserved


def test_legacy_database_without_notice_component_keeps_behavior(tmp_path: Path) -> None:
    data = tmp_path / "authority"
    data.mkdir()
    photos = LocalBlobStore(data / "photos")
    ordinary = photos.put(OWNER, png("white"))
    data, bundle, _case, _packet = _beta_ready(tmp_path, photo_ref=ordinary)
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        db = store._require_open()
        db.execute("DROP TRIGGER pb_beta_feedback_notices_no_update")
        db.execute("DROP TRIGGER pb_beta_feedback_notices_no_delete")
        db.execute("DROP TABLE pb_beta_feedback_notices")
        db.execute("DELETE FROM pb_store_versions WHERE component='beta_feedback'")
        db.commit()
    manifest = recovery.backup(data, bundle)
    assert f"photos/{_folder(OWNER)}/{ordinary}" in manifest["artifacts"]
    recovery.verify(bundle)
