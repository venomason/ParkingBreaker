"""Synthetic T36 execution, failure/restart, fencing and restore exclusion proofs."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import test_retention_plan as fixture
from parkingbreaker import beta_feedback_store, draft_ownership_store
from parkingbreaker import retention_plan as rp
from parkingbreaker import retention_purge as purge
from parkingbreaker import retention_state as rs
from parkingbreaker.payment_records import PaymentCursor, PaymentOutcome, cursor_json
from parkingbreaker.retention_policy import (
    ClosureKind,
    DataScope,
    PolicyAdoption,
    UnresolvedBlockers,
)

CLEAN = UnresolvedBlockers()


class Crash(BaseException):
    """Emulate process death, including the window after an external deletion."""


class Store:
    def __init__(self) -> None:
        self.items = {(fixture.OWNER, fixture.PHOTO_REF): fixture.PHOTO_BYTES}
        self.calls: list[tuple[str, str]] = []
        self.fail: set[str] = set()
        self.crash_after_delete = False
        self.noop = False

    def get(self, owner: str, ref: str) -> bytes | None:
        return self.items.get((owner, ref))

    def delete(self, owner: str, ref: str) -> None:
        self.calls.append((owner, ref))
        if ref in self.fail:
            raise OSError("/private/customer/path " + fixture.SENTINEL)
        if not self.noop:
            self.items.pop((owner, ref), None)
        if self.crash_after_delete:
            raise Crash


def database(tmp_path: Path, *, history: int = 0) -> sqlite3.Connection:
    return fixture.make_database(
        tmp_path / "synthetic.sqlite", case_workspace=True, history=history
    )


def plan(connection: sqlite3.Connection, store: Store) -> rp.RetentionPlan:
    return fixture.build_plan(connection, blob_store=store)


def advance(
    connection: sqlite3.Connection,
    manifest: rp.RetentionPlan,
    store: Store,
    *,
    batch_size: int = 500,
    blockers: UnresolvedBlockers | None = CLEAN,
) -> purge.PurgeReceipt:
    return purge.execute_retention_purge(
        connection,
        manifest,
        synthetic=True,
        planned_at=fixture.EVALUATED,
        evaluated_at=fixture.EVALUATED + timedelta(days=1),
        blockers=blockers,
        blob_store=store,
        batch_size=batch_size,
    )


def finish(
    connection: sqlite3.Connection, manifest: rp.RetentionPlan, store: Store
) -> purge.PurgeReceipt:
    for _ in range(100):
        receipt = advance(connection, manifest, store)
        if receipt.status in {"scoped_complete", "partial_blob_failure"}:
            return receipt
    pytest.fail("bounded synthetic purge failed to converge")


def original_triggers(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    return connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
        "AND name NOT GLOB 'pb_purge_*' ORDER BY name"
    ).fetchall()


def install_real_immutable_tables(connection: sqlite3.Connection) -> None:
    draft_rows = connection.execute("SELECT * FROM pb_draft_attempts").fetchall()
    notice_rows = connection.execute("SELECT * FROM pb_beta_feedback_notices").fetchall()
    connection.execute("DROP TABLE pb_draft_attempts")
    draft_ownership_store.initialize_draft_ownership(connection)
    connection.executemany("INSERT INTO pb_draft_attempts VALUES (?, ?, ?, ?, ?, ?, ?)", draft_rows)
    connection.execute("DROP TABLE pb_beta_feedback_notices")
    beta_feedback_store.initialize_beta_feedback(connection)
    connection.executemany(
        "INSERT INTO pb_beta_feedback_notices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", notice_rows
    )


def test_trigger_safe_scrub_preserves_every_audit_column(tmp_path: Path) -> None:
    connection, store = database(tmp_path), Store()
    install_real_immutable_tables(connection)
    connection.execute("PRAGMA foreign_keys = ON")
    triggers = original_triggers(connection)
    draft_before = connection.execute("SELECT * FROM pb_draft_attempts").fetchone()
    notice_before = connection.execute("SELECT * FROM pb_beta_feedback_notices").fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="finish once"):
        connection.execute("UPDATE pb_draft_attempts SET result_json = NULL")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("UPDATE pb_beta_feedback_notices SET payload_json = '{}'")
    manifest = plan(connection, store)
    receipt = finish(connection, manifest, store)
    assert receipt.status == "scoped_complete"
    assert original_triggers(connection) == triggers
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    draft_after = connection.execute("SELECT * FROM pb_draft_attempts").fetchone()
    notice_after = connection.execute("SELECT * FROM pb_beta_feedback_notices").fetchone()
    assert draft_after == (*draft_before[:-1], None)
    assert notice_after == (*notice_before[:6], purge.REDACTED_JSON, *notice_before[7:])
    for table, (_key, _fence, columns) in purge._SOURCES.items():
        for column in columns:
            rows = connection.execute(f"SELECT {column} FROM {table}").fetchall()
            assert all(fixture.SENTINEL not in str(row) for row in rows)
            assert all(fixture.CONTACT_SENTINEL not in str(row) for row in rows)
    for statement in (
        "UPDATE pb_draft_attempts SET result_json = '{}'",
        "DELETE FROM pb_draft_attempts",
        "UPDATE pb_beta_feedback_notices SET payload_json = '{}'",
        "DELETE FROM pb_beta_feedback_notices",
        "UPDATE pb_workspaces SET payload_json = '{}'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement)
    assert connection.execute("SELECT COUNT(*) FROM pb_purge_targets").fetchone() == (
        len(manifest.targets),
    )
    assert store.calls == [(fixture.OWNER, fixture.PHOTO_REF)]


@pytest.mark.parametrize("field", ["plan_digest", "binding_digest", "decision_digest", "owner_dir"])
def test_manifest_tampering_refuses_without_mutation(tmp_path: Path, field: str) -> None:
    connection, store = database(tmp_path), Store()
    original = plan(connection, store)
    manifest = {
        "plan_digest": dataclasses.replace(original, plan_digest="0" * 64),
        "binding_digest": dataclasses.replace(original, binding_digest="0" * 64),
        "decision_digest": dataclasses.replace(original, decision_digest="0" * 64),
        "owner_dir": dataclasses.replace(original, owner_dir="0" * 64),
    }[field]
    before = list(connection.iterdump())
    with pytest.raises(purge.RetentionPurgeRefused):
        advance(connection, manifest, store)
    assert list(connection.iterdump()) == before
    assert not store.calls


def test_target_and_summary_tampering_and_wrong_original_bound_refuse(tmp_path: Path) -> None:
    connection, store = database(tmp_path), Store()
    manifest = plan(connection, store)
    for changed in (
        dataclasses.replace(manifest, targets=manifest.targets[:-1]),
        dataclasses.replace(
            manifest, summary=dataclasses.replace(manifest.summary, target_count=1)
        ),
        dataclasses.replace(manifest, workspace_token="other-workspace"),
    ):
        with pytest.raises(purge.RetentionPurgeRefused):
            advance(connection, changed, store)
    with pytest.raises(purge.RetentionPurgeRefused, match="plan_digest_mismatch"):
        purge.execute_retention_purge(
            connection,
            manifest,
            synthetic=True,
            planned_at=fixture.EVALUATED,
            evaluated_at=fixture.EVALUATED,
            blockers=UnresolvedBlockers(),
            blob_store=store,
            max_plan_rows=20_001,
        )
    assert not store.calls


def test_recomputed_digest_cannot_authorize_malformed_service_content(tmp_path: Path) -> None:
    connection, store = database(tmp_path), Store()
    connection.execute("UPDATE pb_beta_feedback_notices SET owner = 'wrong-owner'")
    original = plan(connection, store)
    assert not original.summary.eligible
    # A digest is not a signature. Re-hashing a forged manifest must not grant authority.
    forged = dataclasses.replace(
        original, summary=dataclasses.replace(original.summary, eligible=True, blocked_reasons=())
    )
    forged = dataclasses.replace(
        forged,
        plan_digest=purge._manifest_digest(
            forged,
            rs.case_snapshot(connection, fixture.CASE),
            fixture.EVALUATED,
            rp.DEFAULT_MAX_PLAN_ROWS,
        ),
    )
    before = list(connection.iterdump())
    with pytest.raises(purge.RetentionPurgeRefused):
        advance(connection, forged, store)
    assert list(connection.iterdump()) == before
    assert not store.calls


@pytest.mark.parametrize(
    "mutation", ["policy", "closure", "workspace", "intake", "content", "schema"]
)
@pytest.mark.parametrize("started", [False, True])
def test_revision_and_source_drift_before_and_after_restart(
    tmp_path: Path,
    mutation: str,
    started: bool,
) -> None:
    connection, store = database(tmp_path), Store()
    manifest = plan(connection, store)
    if started:
        advance(connection, manifest, store, batch_size=1)
    if mutation == "policy":
        rs.record_policy(
            connection,
            command_id="new-policy",
            adoption=PolicyAdoption.ADOPTED,
            timezone=ZoneInfo("UTC"),
            at=fixture.EVALUATED,
            adoption_evidence_ref="new-evidence",
            expected_version=1,
        )
    elif mutation == "closure":
        rs.record_closure(
            connection,
            command_id="new-closure",
            case_token=fixture.CASE,
            kind=ClosureKind.ABANDONED,
            anchor_at=fixture.ANCHOR,
            source_ref="new-closure-source",
            at=fixture.EVALUATED,
            expected_generation=1,
        )
    elif mutation == "schema":
        connection.execute("CREATE INDEX new_plan_drift ON pb_workspaces(revision)")
    else:
        table = "pb_intake_records" if mutation == "intake" else "pb_workspaces"
        if started:
            # Simulate a privileged restore bypassing the fence; regular writes already refuse.
            connection.execute(f"DROP TRIGGER pb_purge_fence_{table}_update")
        assignment = "payload_json = '{}'" if mutation == "content" else "revision = revision + 1"
        connection.execute(f"UPDATE {table} SET {assignment}")
        if started:
            connection.execute(purge._FENCES[f"pb_purge_fence_{table}_update"])
    connection.close()
    connection = sqlite3.connect(tmp_path / "synthetic.sqlite", isolation_level=None)
    before = list(connection.iterdump())
    with pytest.raises(purge.RetentionPurgeRefused):
        advance(connection, manifest, store)
    assert list(connection.iterdump()) == before
    assert not store.calls


@pytest.mark.parametrize("scope", list(DataScope))
@pytest.mark.parametrize("started", [False, True])
def test_every_active_hold_scope_refuses_and_release_still_changes_revision(
    tmp_path: Path,
    scope: DataScope,
    started: bool,
) -> None:
    connection, store = database(tmp_path), Store()
    manifest = plan(connection, store)
    if started:
        advance(connection, manifest, store, batch_size=1)
    rs.record_hold(
        connection,
        command_id="hold",
        case_token=fixture.CASE,
        hold_id="h",
        scope=scope,
        source_ref="hold-source",
        at=fixture.EVALUATED,
    )
    with pytest.raises(purge.RetentionPurgeRefused, match="hold_active"):
        advance(connection, manifest, store)
    rs.release_hold(
        connection,
        command_id="release",
        case_token=fixture.CASE,
        hold_id="h",
        at=fixture.EVALUATED,
        expected_generation=1,
    )
    with pytest.raises(purge.RetentionPurgeRefused, match="revision_drift"):
        advance(connection, manifest, store)
    assert not store.calls


@pytest.mark.parametrize(
    "field", ["money", "submission", "original_mail", "deadline", "incident", "recovery"]
)
def test_unresolved_blockers_refuse_partial_work(tmp_path: Path, field: str) -> None:
    connection, store = database(tmp_path), Store()
    manifest = plan(connection, store)
    advance(connection, manifest, store, batch_size=1)
    before = list(connection.iterdump())
    with pytest.raises(purge.RetentionPurgeRefused, match="unresolved_effects"):
        advance(connection, manifest, store, blockers=UnresolvedBlockers(**{field: True}))
    assert list(connection.iterdump()) == before
    assert not store.calls


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("letter_state", "SUBMITTING"),
        ("letter_state", "IN_TRANSIT"),
        ("payment_state", "CAPTURE_PENDING"),
        ("payment_state", "CAPTURE_RETRYING"),
        ("payment_state", "AUTHORIZED"),
        ("payment_state", "unrecognized"),
    ],
)
def test_authoritative_uncertain_mail_payment_overrides_clean_blockers(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    connection, store = database(tmp_path), Store()
    connection.execute(f"ALTER TABLE pb_cases ADD COLUMN {column} TEXT")
    connection.execute(f"UPDATE pb_cases SET {column} = ?", (value,))
    manifest = plan(connection, store)
    assert manifest.summary.eligible
    with pytest.raises(purge.RetentionPurgeRefused, match="unresolved_effects"):
        advance(connection, manifest, store)
    assert not store.calls


def test_pending_effect_and_missing_blockers_fail_closed(tmp_path: Path) -> None:
    connection, store = database(tmp_path), Store()
    connection.execute("CREATE TABLE pb_pending_effects (case_token TEXT, completed_at TEXT)")
    connection.execute("INSERT INTO pb_pending_effects VALUES (?, NULL)", (fixture.CASE,))
    manifest = plan(connection, store)
    with pytest.raises(purge.RetentionPurgeRefused, match="unresolved_effects"):
        advance(connection, manifest, store)
    connection.execute("UPDATE pb_pending_effects SET completed_at = 'resolved'")
    with pytest.raises(purge.RetentionPurgeRefused, match="unresolved_effects"):
        advance(connection, manifest, store, blockers=None)


def test_caller_transaction_is_untouched(tmp_path: Path) -> None:
    connection, store = database(tmp_path), Store()
    manifest = plan(connection, store)
    connection.execute("BEGIN")
    connection.execute("UPDATE pb_workspaces SET revision = 3")
    with pytest.raises(purge.RetentionPurgeRefused, match="caller_transaction_active"):
        advance(connection, manifest, store)
    assert connection.in_transaction
    assert connection.execute("SELECT revision FROM pb_workspaces").fetchone() == (3,)
    connection.rollback()
    assert connection.execute("SELECT revision FROM pb_workspaces").fetchone() == (2,)
    assert not store.calls


def test_more_than_5000_payloads_and_blobs_resume_with_restart(tmp_path: Path) -> None:
    connection, store = database(tmp_path), Store()
    count = 5003
    rows = []
    for n in range(count):
        data = f"synthetic-object-{n}".encode()
        ref = "photo_" + hashlib.sha256(data).hexdigest()
        store.items[(fixture.OWNER, ref)] = data
        rows.append((fixture.WORKSPACE, n + 1, json.dumps({"photo": ref}), "synthetic"))
    connection.execute("BEGIN")
    connection.executemany("INSERT INTO pb_workspace_history VALUES (?, ?, ?, ?)", rows)
    connection.commit()
    manifest = plan(connection, store)
    assert len(manifest.targets) > 5000
    assert len(manifest.blobs) == count + 1
    first = advance(connection, manifest, store, batch_size=137)
    assert first.redacted_count == 137
    assert not store.calls
    connection.close()
    connection = sqlite3.connect(tmp_path / "synthetic.sqlite", isolation_level=None)
    receipt = finish(connection, manifest, store)
    assert receipt.status == "scoped_complete"
    assert receipt.redacted_count == len(manifest.targets)
    assert len(store.calls) == count + 1
    assert len(set(store.calls)) == count + 1
    assert not store.items
    assert advance(connection, manifest, store) == receipt
    assert len(store.calls) == count + 1


def test_partial_blob_failure_is_content_free_and_retry_is_idempotent(tmp_path: Path) -> None:
    connection, store = database(tmp_path), Store()
    data = b"second synthetic object"
    ref = "photo_" + hashlib.sha256(data).hexdigest()
    store.items[(fixture.OWNER, ref)] = data
    connection.execute(
        "INSERT INTO pb_workspace_history VALUES (?, 1, ?, 'synthetic')",
        (fixture.WORKSPACE, json.dumps({"photo": ref})),
    )
    store.fail.add(fixture.PHOTO_REF)
    manifest = plan(connection, store)
    receipt = finish(connection, manifest, store)
    assert receipt.status == "partial_blob_failure"
    assert dict(receipt.blob_counts) == {"deleted": 1, "failed": 1}
    connection.close()
    connection = sqlite3.connect(tmp_path / "synthetic.sqlite", isolation_level=None)
    store.fail.clear()
    receipt = advance(connection, manifest, store)
    assert receipt.status == "scoped_complete"
    assert dict(receipt.blob_counts) == {"deleted": 2}
    assert store.calls.count((fixture.OWNER, ref)) == 1
    assert store.calls.count((fixture.OWNER, fixture.PHOTO_REF)) == 2
    assert advance(connection, manifest, store) == receipt
    public = json.dumps(receipt.to_dict()) + repr(receipt)
    for secret in (
        fixture.SENTINEL,
        fixture.CONTACT_SENTINEL,
        fixture.OWNER,
        fixture.CASE,
        fixture.WORKSPACE,
        fixture.PHOTO_REF,
        "/private/customer/path",
    ):
        assert secret not in public
    assert not receipt.service_purge_complete
    assert "sqlite_wal_and_free_pages" in receipt.outstanding_scopes


def test_crash_after_external_delete_reconciles_absence(tmp_path: Path) -> None:
    connection, store = database(tmp_path), Store()
    install_real_immutable_tables(connection)
    manifest = plan(connection, store)
    triggers = original_triggers(connection)
    store.crash_after_delete = True
    with pytest.raises(Crash):
        advance(connection, manifest, store)
    assert original_triggers(connection) == triggers
    assert not connection.in_transaction
    assert not store.items
    assert connection.execute("SELECT COUNT(*) FROM pb_purge_cases").fetchone() == (1,)
    assert connection.execute("SELECT COUNT(*) FROM pb_purge_targets").fetchone() == (0,)
    with pytest.raises(purge.RetentionPurgeRefused, match="access_fenced"):
        purge.assert_service_access(connection, case_token=fixture.CASE)
    connection.close()
    connection = sqlite3.connect(tmp_path / "synthetic.sqlite", isolation_level=None)
    store.crash_after_delete = False
    receipt = finish(connection, manifest, store)
    assert dict(receipt.blob_counts) == {"absent": 1}
    assert len(store.calls) == 1


def test_trigger_and_content_rollback_if_scrubbing_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, store = database(tmp_path), Store()
    install_real_immutable_tables(connection)
    manifest = plan(connection, store)
    triggers = original_triggers(connection)
    original = purge._locator
    calls = 0

    def interrupted(target: rp.PlanTarget, binding: rp._Binding) -> tuple[str, tuple[object, ...]]:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise Crash
        return original(target, binding)

    monkeypatch.setattr(purge, "_locator", interrupted)
    with pytest.raises(Crash):
        advance(connection, manifest, store)
    assert original_triggers(connection) == triggers
    assert connection.execute("SELECT COUNT(*) FROM pb_purge_targets").fetchone() == (0,)
    assert (
        fixture.SENTINEL
        in connection.execute("SELECT payload_json FROM pb_workspaces").fetchone()[0]
    )
    monkeypatch.setattr(purge, "_locator", original)
    assert finish(connection, manifest, store).status == "scoped_complete"


@pytest.mark.parametrize("mode", ["shared", "restricted", "nullable_shared", "new_shared"])
def test_shared_and_restricted_references_preserve_blobs(tmp_path: Path, mode: str) -> None:
    connection, store = database(tmp_path), Store()
    if mode == "restricted":
        connection.execute("CREATE TABLE pb_mail_attempts (case_token TEXT, pdf BLOB)")
        connection.execute(
            "INSERT INTO pb_mail_attempts VALUES (?, ?)", (fixture.CASE, fixture.PHOTO_REF.encode())
        )
    if mode == "new_shared":
        manifest = plan(connection, store)
        advance(connection, manifest, store, batch_size=1)
    if mode != "restricted":
        connection.execute(
            "INSERT INTO pb_intake_owners VALUES ('other', ?, 'synthetic')", (fixture.OWNER,)
        )
        connection.execute(
            "INSERT INTO pb_workspaces VALUES ('other', ?, 'synthetic', 1)",
            ("{}" if mode == "nullable_shared" else json.dumps({"photo": fixture.PHOTO_REF}),),
        )
        if mode == "nullable_shared":
            # NULL agency_json defeats the old planner's summed instr expression.
            connection.execute(
                "INSERT INTO pb_intake_records VALUES ('other', 'other', ?, '{}', NULL, "
                "NULL, NULL, 'synthetic', 1)",
                (json.dumps({"photo": fixture.PHOTO_REF}),),
            )
    if mode != "new_shared":
        manifest = plan(connection, store)
    receipt = finish(connection, manifest, store)
    assert receipt.status == "scoped_complete"
    assert dict(receipt.blob_counts) == {"restricted" if mode == "restricted" else "shared": 1}
    assert store.items
    assert not store.calls
    if mode == "restricted":
        assert connection.execute("SELECT pdf FROM pb_mail_attempts").fetchone() == (
            fixture.PHOTO_REF.encode(),
        )


@pytest.mark.parametrize("mode", ["noop", "digest_drift", "missing"])
def test_blob_deletion_verifies_bytes_and_absence(tmp_path: Path, mode: str) -> None:
    connection, store = database(tmp_path), Store()
    manifest = plan(connection, store)
    advance(connection, manifest, store, batch_size=1)
    if mode == "noop":
        store.noop = True
    elif mode == "digest_drift":
        store.items[(fixture.OWNER, fixture.PHOTO_REF)] = b"different bytes"
    else:
        store.items.clear()
    receipt = finish(connection, manifest, store)
    expected = {"noop": "failed", "digest_drift": "protected", "missing": "absent"}[mode]
    assert dict(receipt.blob_counts) == {expected: 1}
    assert len(store.calls) == (1 if mode == "noop" else 0)


@pytest.mark.parametrize("selector", ["case_token", "workspace_token", "intake_id"])
def test_access_fences_partial_and_completed_purges(tmp_path: Path, selector: str) -> None:
    connection, store = database(tmp_path), Store()
    value = {
        "case_token": fixture.CASE,
        "workspace_token": fixture.WORKSPACE,
        "intake_id": fixture.INTAKE,
    }[selector]
    manifest = plan(connection, store)
    assert (
        purge.guarded_service_read(connection, lambda: "visible", **{selector: value}) == "visible"
    )
    for receipt in (
        advance(connection, manifest, store, batch_size=1),
        finish(connection, manifest, store),
    ):
        assert receipt.redacted_count > 0
        with pytest.raises(purge.RetentionPurgeRefused, match="access_fenced"):
            purge.guarded_service_read(
                connection, lambda: pytest.fail("reader must not run"), **{selector: value}
            )
    assert (
        purge.guarded_service_read(connection, lambda: "other", workspace_token="other") == "other"
    )
    for table in ("pb_purge_cases", "pb_purge_targets"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(f"DELETE FROM {table}")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}")
    with pytest.raises(sqlite3.IntegrityError, match="fenced"):
        connection.execute("UPDATE pb_cases SET case_token = 'replacement-case'")
    with pytest.raises(sqlite3.IntegrityError, match="fenced"):
        connection.execute("UPDATE pb_intake_owners SET owner = 'replacement-owner'")


def backup_of(connection: sqlite3.Connection) -> sqlite3.Connection:
    backup = sqlite3.connect(":memory:", isolation_level=None)
    connection.backup(backup)
    return backup


def test_backup_restore_refuses_old_data_without_tombstones(tmp_path: Path) -> None:
    connection, store = database(tmp_path), Store()
    backup = backup_of(connection)
    manifest = plan(connection, store)
    advance(connection, manifest, store, batch_size=1)
    assert not backup.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'pb_purge_cases'"
    ).fetchone()
    with pytest.raises(purge.RetentionPurgeRefused, match="restore_excluded"):
        purge.validate_retention_restore(backup, connection, evaluated_at=fixture.EVALUATED)
    finish(connection, manifest, store)
    with pytest.raises(purge.RetentionPurgeRefused, match="restore_excluded"):
        purge.validate_retention_restore(backup, connection, evaluated_at=fixture.EVALUATED)
    # Direct import of an old row is blocked independently of the validation API.
    old_workspace = backup.execute("SELECT * FROM pb_workspaces").fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="fenced"):
        connection.execute(
            "INSERT OR REPLACE INTO pb_workspaces VALUES (?, ?, ?, ?)", old_workspace
        )


def test_restore_rejects_expiry_even_without_purge_or_with_preservation_hold(
    tmp_path: Path,
) -> None:
    connection = database(tmp_path)
    for held in (False, True):
        if held:
            rs.record_hold(
                connection,
                command_id="h",
                case_token=fixture.CASE,
                hold_id="h",
                scope=DataScope.SERVICE_DATA,
                source_ref="h-source",
                at=fixture.EVALUATED,
            )
        backup = backup_of(connection)
        with pytest.raises(purge.RetentionPurgeRefused, match="restore_excluded"):
            purge.validate_retention_restore(backup, connection, evaluated_at=fixture.EVALUATED)
        backup.close()


def test_restore_accepts_only_unexpired_known_cases_and_does_not_write(tmp_path: Path) -> None:
    connection = database(tmp_path)
    backup = backup_of(connection)
    before = list(connection.iterdump()), list(backup.iterdump())
    at = fixture.ANCHOR + timedelta(days=100)
    purge.validate_retention_restore(backup, connection, evaluated_at=at)
    assert (list(connection.iterdump()), list(backup.iterdump())) == before
    with pytest.raises(purge.RetentionPurgeRefused, match="restore_excluded"):
        purge.validate_retention_restore(
            backup, connection, evaluated_at=fixture.ANCHOR.replace(year=2021)
        )
    backup.execute("UPDATE pb_cases SET case_token = 'unknown-case'")
    with pytest.raises(purge.RetentionPurgeRefused, match="restore_excluded"):
        purge.validate_retention_restore(backup, connection, evaluated_at=at)


@pytest.mark.parametrize("table", ["pb_beta_notice_records", "pb_beta_feedback_records"])
def test_unrecognized_notice_schemas_fail_closed(tmp_path: Path, table: str) -> None:
    connection, store = database(tmp_path), Store()
    connection.execute(f"CREATE TABLE {table} (payload_json TEXT)")
    connection.execute(f"INSERT INTO {table} VALUES (?)", (fixture.SENTINEL,))
    manifest = plan(connection, store)
    with pytest.raises(purge.RetentionPurgeRefused, match="plan_not_eligible"):
        advance(connection, manifest, store)
    assert connection.execute(f"SELECT payload_json FROM {table}").fetchone() == (fixture.SENTINEL,)
    assert not store.calls


def test_unknown_trigger_cannot_be_silently_disabled(tmp_path: Path) -> None:
    connection, store = database(tmp_path), Store()
    connection.execute(
        "CREATE TRIGGER unknown_guard BEFORE UPDATE ON pb_workspaces "
        "BEGIN SELECT RAISE(ABORT, 'unknown guard'); END"
    )
    manifest = plan(connection, store)
    before = list(connection.iterdump())
    with pytest.raises(purge.RetentionPurgeRefused, match="schema_drift"):
        advance(connection, manifest, store)
    assert list(connection.iterdump()) == before


@pytest.mark.parametrize("batch", [0, -1, 501, True])
def test_invalid_bounds_refuse(tmp_path: Path, batch: int) -> None:
    connection, store = database(tmp_path), Store()
    with pytest.raises(purge.RetentionPurgeRefused, match="invalid_input"):
        advance(connection, plan(connection, store), store, batch_size=batch)


def test_non_synthetic_execution_refuses(tmp_path: Path) -> None:
    connection, store = database(tmp_path), Store()
    with pytest.raises(purge.RetentionPurgeRefused, match="synthetic_only"):
        purge.execute_retention_purge(
            connection,
            plan(connection, store),
            synthetic=False,
            planned_at=fixture.EVALUATED,
            evaluated_at=fixture.EVALUATED,
            blockers=UnresolvedBlockers(),
            blob_store=store,
        )


@pytest.mark.parametrize("table", ["pb_submission_operations", "pb_payment_operations"])
def test_pending_operations_override_clean_case_state(tmp_path: Path, table: str) -> None:
    connection, store = database(tmp_path), Store()
    connection.execute(f"CREATE TABLE {table} (case_token TEXT, completed_at TEXT)")
    connection.execute(f"INSERT INTO {table} VALUES (?, NULL)", (fixture.CASE,))
    manifest = plan(connection, store)
    with pytest.raises(purge.RetentionPurgeRefused, match="unresolved_effects"):
        advance(connection, manifest, store)
    assert not store.calls


@pytest.mark.parametrize("kind", ["submission", "payment"])
def test_current_uncertain_or_malformed_cursors_override_clean_blockers(
    tmp_path: Path,
    kind: str,
) -> None:
    connection, store = database(tmp_path), Store()
    table, column = (
        ("pb_submission_cursors", "outcome_json")
        if kind == "submission"
        else ("pb_payment_cursor", "payload")
    )
    connection.execute(f"CREATE TABLE {table} (case_token TEXT, {column} TEXT)")
    payload = '{"kind":"uncertain","reason":"synthetic"}'
    if kind == "payment":
        payload = cursor_json(
            PaymentCursor(
                case_token=fixture.CASE,
                effect_id="synthetic-effect",
                kind="capture",
                authorization_ref="pi_synthetic",
                amount_cents=1999,
                key="synthetic-key",
                first_at=fixture.EVALUATED,
                failed_attempts=1,
                next_at=fixture.EVALUATED,
                revision=1,
                case_revision=1,
                owner=None,
                call_kind="retrieve",
                outcome=PaymentOutcome("uncertain", "pi_synthetic", 1999),
                done=False,
            )
        )
    connection.execute(f"INSERT INTO {table} VALUES (?, ?)", (fixture.CASE, payload))
    manifest = plan(connection, store)
    with pytest.raises(purge.RetentionPurgeRefused, match="unresolved_effects"):
        advance(connection, manifest, store)
    assert not store.calls


def test_retired_uncertain_mail_cursor_does_not_block_current_final_attempt(tmp_path: Path) -> None:
    connection, store = database(tmp_path), Store()
    connection.execute("ALTER TABLE pb_cases ADD COLUMN attempt INTEGER")
    connection.execute("UPDATE pb_cases SET attempt = 2")
    connection.execute(
        "CREATE TABLE pb_submission_cursors (case_token TEXT, attempt INTEGER, outcome_json TEXT)"
    )
    connection.executemany(
        "INSERT INTO pb_submission_cursors VALUES (?, ?, ?)",
        [
            (fixture.CASE, 1, '{"kind":"uncertain","reason":"synthetic"}'),
            (fixture.CASE, 2, '{"kind":"not_sent"}'),
        ],
    )
    assert finish(connection, plan(connection, store), store).status == "scoped_complete"


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("pb_draft_attempts", "outcome", None),
        ("pb_ticket_intake_operations", "status", "pending"),
    ],
)
def test_active_drafting_and_intake_work_refuse(
    tmp_path: Path,
    table: str,
    column: str,
    value: str | None,
) -> None:
    connection, store = database(tmp_path), Store()
    connection.execute(f"UPDATE {table} SET {column} = ?", (value,))
    with pytest.raises(purge.RetentionPurgeRefused, match="unresolved_effects"):
        advance(connection, plan(connection, store), store)


def test_restore_orphan_payloads_cannot_bypass_case_iteration(tmp_path: Path) -> None:
    connection = database(tmp_path)
    backup = backup_of(connection)
    backup.execute("DELETE FROM pb_cases")
    with pytest.raises(purge.RetentionPurgeRefused, match="restore_excluded"):
        purge.validate_retention_restore(
            backup, connection, evaluated_at=fixture.ANCHOR + timedelta(days=100)
        )


def test_restore_guard_holds_authority_snapshot_and_does_not_enter_for_expiry(
    tmp_path: Path,
) -> None:
    connection = database(tmp_path)
    backup = backup_of(connection)
    with purge.retention_restore_guard(
        backup, connection, evaluated_at=fixture.ANCHOR + timedelta(days=100)
    ):
        assert connection.in_transaction
        assert backup.in_transaction
        concurrent = sqlite3.connect(tmp_path / "synthetic.sqlite", isolation_level=None, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                concurrent.execute("BEGIN IMMEDIATE")
        finally:
            concurrent.close()
    assert not connection.in_transaction
    assert not backup.in_transaction
    with (
        pytest.raises(purge.RetentionPurgeRefused, match="restore_excluded"),
        purge.retention_restore_guard(backup, connection, evaluated_at=fixture.EVALUATED),
    ):
        pytest.fail("expired restore callback must not run")


def test_blob_deletion_holds_authority_write_lock(tmp_path: Path) -> None:
    connection = database(tmp_path)
    blocked = False

    class ConcurrentStore(Store):
        def delete(self, owner: str, ref: str) -> None:
            nonlocal blocked
            other = sqlite3.connect(tmp_path / "synthetic.sqlite", isolation_level=None, timeout=0)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.execute("BEGIN IMMEDIATE")
                blocked = True
            finally:
                other.close()
            super().delete(owner, ref)

    store = ConcurrentStore()
    assert finish(connection, plan(connection, store), store).status == "scoped_complete"
    assert blocked


def test_compatible_planning_and_second_case_after_schema_installation(tmp_path: Path) -> None:
    connection, store = database(tmp_path), Store()
    original = plan(connection, store)
    compatible = purge.plan_retention_purge(
        connection,
        case_token=fixture.CASE,
        evaluated_at=fixture.EVALUATED,
        blockers=CLEAN,
        blob_store=store,
    )
    assert compatible == original
    finish(connection, original, store)
    with pytest.raises(purge.RetentionPurgeRefused, match="access_fenced"):
        purge.plan_retention_purge(
            connection,
            case_token=fixture.CASE,
            evaluated_at=fixture.EVALUATED,
            blockers=CLEAN,
            blob_store=store,
        )
    connection.execute("INSERT INTO pb_cases VALUES ('second-case', 'second-workspace')")
    connection.execute(
        "INSERT INTO pb_workspaces VALUES ('second-workspace', '{}', 'synthetic', 1)"
    )
    connection.execute(
        "INSERT INTO pb_intake_owners VALUES ('second-workspace', 'second-owner', 'synthetic')"
    )
    connection.execute(
        "INSERT INTO pb_intake_records VALUES "
        "('second-intake', 'second-workspace', '{}', '{}', NULL, NULL, "
        "'second-case', 'synthetic', 1)"
    )
    rs.record_closure(
        connection,
        command_id="second-closure",
        case_token="second-case",
        kind=ClosureKind.CLOSED,
        anchor_at=fixture.ANCHOR,
        source_ref="synthetic",
        at=fixture.POLICY_AT,
    )
    second = purge.plan_retention_purge(
        connection,
        case_token="second-case",
        evaluated_at=fixture.EVALUATED,
        blockers=CLEAN,
        blob_store=store,
    )
    assert finish(connection, second, store).status == "scoped_complete"
    assert connection.execute("SELECT COUNT(*) FROM pb_purge_cases").fetchone() == (2,)
