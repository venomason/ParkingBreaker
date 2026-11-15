"""Behavioral and fault-injection atomicity tests for T14-01 intake recovery."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest

from parkingbreaker.intake_store import IntakeConflict
from parkingbreaker.transaction_store import RevisionConflict, SqliteTransactionStore
from parkingbreaker.workspace import Workspace

NOW = datetime(2026, 10, 15, 21, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 15, 22, 0, tzinfo=UTC)

SYNTHETIC_STATEMENT = (
    "  Line 1: I parked lawfully at 10:00 AM.\nLine 2: Euro symbol € & Special characters <>!  "
)
SYNTHETIC_PHOTOS = ("photo_front.jpg", "photo_back.jpg")
SYNTHETIC_PROVISIONS = frozenset(["prov_meter", "prov_sign"])

SYNTHETIC_FACTS = {
    "statement": SYNTHETIC_STATEMENT,
    "photos": list(SYNTHETIC_PHOTOS),
    "provisions": sorted(SYNTHETIC_PROVISIONS),
}
SYNTHETIC_INTAKE_JSON = json.dumps(SYNTHETIC_FACTS)
assert json.loads(SYNTHETIC_INTAKE_JSON)["statement"] == SYNTHETIC_STATEMENT


class HookedConnection:
    """Proxy wrapper for sqlite3.Connection to inject faults before/after COMMIT or statements."""

    def __init__(self, target: sqlite3.Connection) -> None:
        self._target = target
        self.raise_before_commit: Exception | None = None
        self.raise_after_commit: Exception | None = None
        self.raise_on_statement: tuple[str, Exception] | None = None

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if self.raise_on_statement is not None:
            pattern, exc = self.raise_on_statement
            if pattern in sql:
                self.raise_on_statement = None
                raise exc

        if sql.strip().upper() == "COMMIT":
            if self.raise_before_commit is not None:
                exc = self.raise_before_commit
                self.raise_before_commit = None
                raise exc
            res = self._target.execute(sql, parameters)
            if self.raise_after_commit is not None:
                exc = self.raise_after_commit
                self.raise_after_commit = None
                raise exc
            return res
        return self._target.execute(sql, parameters)

    @property
    def in_transaction(self) -> bool:
        return self._target.in_transaction

    def close(self) -> None:
        self._target.close()


@pytest.mark.parametrize(
    ("fault_stage", "pattern"),
    [
        ("before_owner", "INSERT INTO pb_intake_owners"),
        ("before_workspace", "INSERT INTO pb_workspaces"),
        ("before_history", "INSERT INTO pb_workspace_history"),
        ("before_commit", "COMMIT"),
    ],
)
def test_create_owned_workspace_fault_injection_rollbacks(
    tmp_path: Path, fault_stage: str, pattern: str
) -> None:
    """Workspace and owner creation rolls back completely if failure occurs."""
    path = tmp_path / "pb.sqlite"
    store = SqliteTransactionStore(path)
    try:
        conn = store._require_open()
        hooked = HookedConnection(conn)
        store._database = hooked  # type: ignore[assignment]

        if fault_stage == "before_commit":
            hooked.raise_before_commit = RuntimeError(f"Simulated fault at {fault_stage}")
        else:
            hooked.raise_on_statement = (
                pattern,
                RuntimeError(f"Simulated fault at {fault_stage}"),
            )

        ws = Workspace(notes="Initial notes", position="Draft pos")

        with pytest.raises(RuntimeError, match=f"Simulated fault at {fault_stage}"):
            store.create_owned_workspace("owner_1", "ws_1", ws, NOW)
    finally:
        store.close()

    # Reopen store and verify total rollback
    reopened = SqliteTransactionStore(path)
    try:
        assert reopened.get_workspace("ws_1") is None
        assert reopened.workspace_owner("ws_1") is None
        assert len(reopened.workspace_history("ws_1")) == 0

        # Verify no orphan rows in database
        check_conn = reopened._require_open()
        owners = check_conn.execute(
            "SELECT * FROM pb_intake_owners WHERE workspace_token='ws_1'"
        ).fetchall()
        assert len(owners) == 0
        workspaces = check_conn.execute(
            "SELECT * FROM pb_workspaces WHERE workspace_token='ws_1'"
        ).fetchall()
        assert len(workspaces) == 0

        # Orphan checks in both directions: no workspace owner without workspace or vice versa
        unowned_workspaces = check_conn.execute(
            "SELECT w.workspace_token FROM pb_workspaces w "
            "LEFT JOIN pb_intake_owners o ON w.workspace_token = o.workspace_token "
            "WHERE o.workspace_token IS NULL"
        ).fetchall()
        assert len(unowned_workspaces) == 0

        missing_workspaces = check_conn.execute(
            "SELECT o.workspace_token FROM pb_intake_owners o "
            "LEFT JOIN pb_workspaces w ON o.workspace_token = w.workspace_token "
            "WHERE w.workspace_token IS NULL"
        ).fetchall()
        assert len(missing_workspaces) == 0
    finally:
        reopened.close()


def test_create_owned_workspace_lost_reply_recovery(tmp_path: Path) -> None:
    """Exact retry or new token request recovers safely after COMMIT succeeds but reply is lost."""
    path = tmp_path / "pb.sqlite"
    store = SqliteTransactionStore(path)
    try:
        conn = store._require_open()
        hooked = HookedConnection(conn)
        store._database = hooked  # type: ignore[assignment]
        hooked.raise_after_commit = RuntimeError("Lost reply after commit")

        ws = Workspace(notes="Exact notes", position="")

        with pytest.raises(RuntimeError, match="Lost reply after commit"):
            store.create_owned_workspace("owner_1", "ws_1", ws, NOW)
    finally:
        store.close()

    # Reopen and confirm commit succeeded
    reopened = SqliteTransactionStore(path)
    try:
        saved_first = reopened.get_workspace("ws_1")
        assert saved_first is not None
        assert saved_first.workspace.notes == "Exact notes"
        assert reopened.workspace_owner("ws_1") == "owner_1"

        # Request with a NEW would-be token for the same owner returns the original workspace
        replayed = reopened.create_owned_workspace("owner_1", "ws_would_be_new", ws, NOW)
        assert replayed.workspace_token == "ws_1"
        assert replayed.workspace.notes == "Exact notes"

        # Verify requested new would-be token does not exist after replay
        assert reopened.get_workspace("ws_would_be_new") is None

        # Verify row counts, history, and orphan checks
        check_conn = reopened._require_open()
        total_ws = check_conn.execute("SELECT COUNT(*) FROM pb_workspaces").fetchone()[0]
        assert total_ws == 1

        total_owners = check_conn.execute("SELECT COUNT(*) FROM pb_intake_owners").fetchone()[0]
        assert total_owners == 1

        owner_rows = check_conn.execute(
            "SELECT * FROM pb_intake_owners WHERE owner='owner_1'"
        ).fetchall()
        assert len(owner_rows) == 1

        ws_rows = check_conn.execute(
            "SELECT * FROM pb_workspaces WHERE workspace_token='ws_1'"
        ).fetchall()
        assert len(ws_rows) == 1

        would_be_rows = check_conn.execute(
            "SELECT * FROM pb_workspaces WHERE workspace_token='ws_would_be_new'"
        ).fetchall()
        assert len(would_be_rows) == 0

        history = reopened.workspace_history("ws_1")
        assert len(history) == 1

        unowned_workspaces = check_conn.execute(
            "SELECT w.workspace_token FROM pb_workspaces w "
            "LEFT JOIN pb_intake_owners o ON w.workspace_token = o.workspace_token "
            "WHERE o.workspace_token IS NULL"
        ).fetchall()
        assert len(unowned_workspaces) == 0

        missing_workspaces = check_conn.execute(
            "SELECT o.workspace_token FROM pb_intake_owners o "
            "LEFT JOIN pb_workspaces w ON o.workspace_token = w.workspace_token "
            "WHERE w.workspace_token IS NULL"
        ).fetchall()
        assert len(missing_workspaces) == 0
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("fault_stage", "pattern"),
    [
        ("before_history", "INSERT INTO pb_workspace_history"),
        ("before_intake", "INSERT INTO pb_intake_records"),
        ("before_commit", "COMMIT"),
    ],
)
def test_intake_snapshot_fault_injection_rollbacks(
    tmp_path: Path, fault_stage: str, pattern: str
) -> None:
    """Intake submission rolls back workspace mutation and intake row before COMMIT."""
    path = tmp_path / "pb.sqlite"
    store = SqliteTransactionStore(path)
    try:
        ws = Workspace(notes="Note A", position="Initial Draft")
        store.create_owned_workspace("owner_1", "ws_1", ws, NOW)

        conn = store._require_open()
        hooked = HookedConnection(conn)
        store._database = hooked  # type: ignore[assignment]

        if fault_stage == "before_commit":
            hooked.raise_before_commit = RuntimeError(f"Simulated fault at {fault_stage}")
        else:
            hooked.raise_on_statement = (
                pattern,
                RuntimeError(f"Simulated fault at {fault_stage}"),
            )

        with pytest.raises(RuntimeError, match=f"Simulated fault at {fault_stage}"):
            store.record_intake_with_workspace_snapshot(
                intake_id="case_1",
                workspace_token="ws_1",
                intake_json='{"statement":"Submitted Statement"}',
                prehold_json='{"status":"held"}',
                customer_statement="Submitted Statement",
                photo_refs=("p1.png",),
                provision_selections=frozenset(["sel1"]),
                now=LATER,
            )
    finally:
        store.close()

    # Reopen and verify complete rollback
    reopened = SqliteTransactionStore(path)
    try:
        current_ws = reopened.get_workspace("ws_1")
        assert current_ws is not None
        assert current_ws.workspace.position == "Initial Draft"
        assert current_ws.workspace.photo_refs == ()
        assert reopened.intake_for_workspace("ws_1") is None
        assert len(reopened.workspace_history("ws_1")) == 1
    finally:
        reopened.close()


def test_intake_snapshot_lost_reply_and_exact_retry(tmp_path: Path) -> None:
    """COMMIT succeeds but reply is lost: reopen and exact retry recovers original intake."""
    path = tmp_path / "pb.sqlite"
    store = SqliteTransactionStore(path)
    try:
        ws = Workspace(notes="Note B", position="Initial Draft B")
        store.create_owned_workspace("owner_2", "ws_2", ws, NOW)

        conn = store._require_open()
        hooked = HookedConnection(conn)
        store._database = hooked  # type: ignore[assignment]
        hooked.raise_after_commit = RuntimeError("Lost reply after intake commit")

        with pytest.raises(RuntimeError, match="Lost reply after intake commit"):
            store.record_intake_with_workspace_snapshot(
                intake_id="case_2",
                workspace_token="ws_2",
                intake_json=SYNTHETIC_INTAKE_JSON,
                prehold_json='{"status":"held_prehold"}',
                customer_statement=SYNTHETIC_STATEMENT,
                photo_refs=SYNTHETIC_PHOTOS,
                provision_selections=SYNTHETIC_PROVISIONS,
                now=LATER,
            )
    finally:
        store.close()

    # Reopen and exact retry
    reopened = SqliteTransactionStore(path)
    try:
        retry_rec = reopened.record_intake_with_workspace_snapshot(
            intake_id="case_2",
            workspace_token="ws_2",
            intake_json=SYNTHETIC_INTAKE_JSON,
            prehold_json='{"status":"held_prehold"}',
            customer_statement=SYNTHETIC_STATEMENT,
            photo_refs=SYNTHETIC_PHOTOS,
            provision_selections=SYNTHETIC_PROVISIONS,
            now=LATER,
        )

        assert retry_rec.intake_id == "case_2"
        assert retry_rec.workspace_token == "ws_2"
        assert retry_rec.prehold_json == '{"status":"held_prehold"}'

        current_ws = reopened.get_workspace("ws_2")
        assert current_ws is not None
        assert current_ws.workspace.position == SYNTHETIC_STATEMENT
        assert current_ws.workspace.photo_refs == SYNTHETIC_PHOTOS
        assert current_ws.workspace.provision_selections == SYNTHETIC_PROVISIONS

        history = reopened.workspace_history("ws_2")
        assert len(history) == 2
        assert tuple(r.revision for r in history) == (1, 2)
    finally:
        reopened.close()


def test_legacy_intake_without_snapshot_repairs_safely(tmp_path: Path) -> None:
    """Legacy intake row with missing snapshot repairs safely using original facts."""
    path = tmp_path / "pb.sqlite"
    store = SqliteTransactionStore(path)
    try:
        ws = Workspace(notes="Note Legacy", position="Old Position")
        store.create_owned_workspace("owner_legacy", "ws_legacy", ws, NOW)

        rec_legacy = store.record_intake(
            intake_id="case_legacy",
            workspace_token="ws_legacy",
            intake_json=SYNTHETIC_INTAKE_JSON,
            prehold_json='{"prehold":"original"}',
            now=NOW,
        )
        assert rec_legacy.intake_id == "case_legacy"
    finally:
        store.close()

    # Reopen and repair snapshot using exact same original facts
    reopened = SqliteTransactionStore(path)
    try:
        repaired = reopened.record_intake_with_workspace_snapshot(
            intake_id="case_legacy",
            workspace_token="ws_legacy",
            intake_json=SYNTHETIC_INTAKE_JSON,
            prehold_json='{"prehold":"original"}',
            customer_statement=SYNTHETIC_STATEMENT,
            photo_refs=SYNTHETIC_PHOTOS,
            provision_selections=SYNTHETIC_PROVISIONS,
            now=LATER,
        )

        assert repaired.intake_id == "case_legacy"
        assert repaired.prehold_json == '{"prehold":"original"}'

        ws_repaired = reopened.get_workspace("ws_legacy")
        assert ws_repaired is not None
        assert ws_repaired.workspace.position == SYNTHETIC_STATEMENT
        assert ws_repaired.workspace.photo_refs == SYNTHETIC_PHOTOS
        assert ws_repaired.workspace.provision_selections == SYNTHETIC_PROVISIONS

        history_count = len(reopened.workspace_history("ws_legacy"))

        # Repeated exact replay should be idempotent and not append duplicate history
        reopened.record_intake_with_workspace_snapshot(
            intake_id="case_legacy",
            workspace_token="ws_legacy",
            intake_json=SYNTHETIC_INTAKE_JSON,
            prehold_json='{"prehold":"original"}',
            customer_statement=SYNTHETIC_STATEMENT,
            photo_refs=SYNTHETIC_PHOTOS,
            provision_selections=SYNTHETIC_PROVISIONS,
            now=LATER,
        )
        assert len(reopened.workspace_history("ws_legacy")) == history_count
    finally:
        reopened.close()


def test_stale_save_rejected_and_submitted_snapshot_preserved(tmp_path: Path) -> None:
    """Stale save is rejected; submitted snapshot and unrelated fields remain preserved."""
    path = tmp_path / "pb.sqlite"

    conn1 = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    init_store = SqliteTransactionStore(path)
    ws_initial = Workspace(
        notes="Preserved Notes",
        position="Initial Position",
        letter_edits=("edit1",),
        answers={"q1": "a1"},
    )
    init_store.create_owned_workspace("owner_stale", "ws_stale", ws_initial, NOW)
    init_store.close()

    # Connection 1 reads draft (revision 1)
    store1 = SqliteTransactionStore(path)
    draft1 = store1.get_workspace("ws_stale")
    assert draft1 is not None
    assert draft1.revision == 1

    # Connection 2 submits intake + snapshot -> updates workspace to revision 2
    store2 = SqliteTransactionStore(path)
    try:
        store2.record_intake_with_workspace_snapshot(
            intake_id="case_stale",
            workspace_token="ws_stale",
            intake_json=SYNTHETIC_INTAKE_JSON,
            prehold_json='{"status":"held"}',
            customer_statement=SYNTHETIC_STATEMENT,
            photo_refs=SYNTHETIC_PHOTOS,
            provision_selections=SYNTHETIC_PROVISIONS,
            now=LATER,
        )
    finally:
        store2.close()

    # Connection 1 attempts stale save using expected_revision=1
    stale_ws = Workspace(notes="Stale Overwrite Attempt", position="Overwritten")
    try:
        with pytest.raises(RevisionConflict, match="workspace revision is stale"):
            store1.save_workspace("ws_stale", stale_ws, LATER, expected_revision=draft1.revision)
    finally:
        store1.close()

    # Reopen connection and verify submitted snapshot and unrelated draft fields remain preserved
    reopened = SqliteTransactionStore(path)
    try:
        current = reopened.get_workspace("ws_stale")
        assert current is not None
        assert current.revision == 2
        assert current.workspace.notes == "Preserved Notes"
        assert current.workspace.letter_edits == ("edit1",)
        assert current.workspace.answers == {"q1": "a1"}
        assert current.workspace.position == SYNTHETIC_STATEMENT
        assert current.workspace.photo_refs == SYNTHETIC_PHOTOS
        assert current.workspace.provision_selections == SYNTHETIC_PROVISIONS
    finally:
        reopened.close()
    conn1.close()


def test_concurrent_racing_identical_owner_creation(tmp_path: Path) -> None:
    """Two independent connections racing identical owner creation succeed."""
    path = tmp_path / "pb.sqlite"

    init_store = SqliteTransactionStore(path)
    init_store.close()

    barrier = Barrier(2)

    def run_owner_create(ws_token: str) -> tuple[bool, str | Exception]:
        store = SqliteTransactionStore(path)
        try:
            barrier.wait(timeout=5.0)
            ws = Workspace(notes="Race Owner Notes")
            rec = store.create_owned_workspace("owner_race_same", ws_token, ws, NOW)
            token = rec.workspace_token
            store.close()
            return True, token
        except Exception as exc:
            store.close()
            return False, exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(run_owner_create, "ws_race_a")
        f2 = executor.submit(run_owner_create, "ws_race_b")
        ok1, res1 = f1.result()
        ok2, res2 = f2.result()

    assert ok1
    assert ok2
    assert res1 == res2  # Both return the same workspace token

    # Reopen and verify database state and orphan checks
    verifier = SqliteTransactionStore(path)
    try:
        conn = verifier._require_open()
        total_ws = conn.execute("SELECT COUNT(*) FROM pb_workspaces").fetchone()[0]
        assert total_ws == 1

        total_owners = conn.execute("SELECT COUNT(*) FROM pb_intake_owners").fetchone()[0]
        assert total_owners == 1

        owners = conn.execute(
            "SELECT workspace_token, owner FROM pb_intake_owners WHERE owner='owner_race_same'"
        ).fetchall()
        assert len(owners) == 1

        unowned_workspaces = conn.execute(
            "SELECT w.workspace_token FROM pb_workspaces w "
            "LEFT JOIN pb_intake_owners o ON w.workspace_token = o.workspace_token "
            "WHERE o.workspace_token IS NULL"
        ).fetchall()
        assert len(unowned_workspaces) == 0

        missing_workspaces = conn.execute(
            "SELECT o.workspace_token FROM pb_intake_owners o "
            "LEFT JOIN pb_workspaces w ON o.workspace_token = w.workspace_token "
            "WHERE w.workspace_token IS NULL"
        ).fetchall()
        assert len(missing_workspaces) == 0
    finally:
        verifier.close()


def test_concurrent_racing_identical_intake_submissions(tmp_path: Path) -> None:
    """Two independent connections racing identical intake submissions succeed."""
    path = tmp_path / "pb.sqlite"

    init_store = SqliteTransactionStore(path)
    init_store.create_owned_workspace(
        "owner_race_intake", "ws_race_intake", Workspace(notes="N"), NOW
    )
    init_store.close()

    barrier = Barrier(2)

    def run_submit(case_id: str) -> tuple[bool, str | Exception]:
        store = SqliteTransactionStore(path)
        try:
            barrier.wait(timeout=5.0)
            rec = store.record_intake_with_workspace_snapshot(
                intake_id=case_id,
                workspace_token="ws_race_intake",
                intake_json=SYNTHETIC_INTAKE_JSON,
                prehold_json='{"prehold":"same"}',
                customer_statement=SYNTHETIC_STATEMENT,
                photo_refs=SYNTHETIC_PHOTOS,
                provision_selections=SYNTHETIC_PROVISIONS,
                now=LATER,
            )
            ret_id = rec.intake_id
            store.close()
            return True, ret_id
        except Exception as exc:
            store.close()
            return False, exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(run_submit, "case_same_1")
        f2 = executor.submit(run_submit, "case_same_2")
        ok1, res1 = f1.result()
        ok2, res2 = f2.result()

    assert ok1
    assert ok2
    assert res1 == res2  # Both return the exact same original intake_id

    verifier = SqliteTransactionStore(path)
    try:
        intake_rec = verifier.intake_for_workspace("ws_race_intake")
        assert intake_rec is not None
        assert intake_rec.intake_id == res1
        assert intake_rec.intake_json == SYNTHETIC_INTAKE_JSON
        assert intake_rec.prehold_json == '{"prehold":"same"}'

        ws = verifier.get_workspace("ws_race_intake")
        assert ws is not None
        assert ws.workspace.position == SYNTHETIC_STATEMENT
        assert ws.workspace.photo_refs == SYNTHETIC_PHOTOS
        assert ws.workspace.provision_selections == SYNTHETIC_PROVISIONS

        history = verifier.workspace_history("ws_race_intake")
        assert len(history) == 2
        assert tuple(r.revision for r in history) == (1, 2)

        conn = verifier._require_open()
        rows = conn.execute(
            "SELECT intake_id FROM pb_intake_records WHERE workspace_token='ws_race_intake'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        verifier.close()


def test_concurrent_racing_conflicting_intake_submissions(tmp_path: Path) -> None:
    """Two independent connections racing conflicting intake submissions: 1 wins, 1 fails."""
    path = tmp_path / "pb.sqlite"

    init_store = SqliteTransactionStore(path)
    init_store.create_owned_workspace(
        "owner_conflict", "ws_conflict_fresh", Workspace(notes="Fresh"), NOW
    )
    init_store.close()

    barrier = Barrier(2)

    c1_photos = ("photo_win_1.jpg", "photo_win_2.jpg")
    c1_provisions = frozenset(["prov_win_a", "prov_win_b"])
    c1_facts = {
        "statement": "Winner Statement",
        "photos": list(c1_photos),
        "provisions": sorted(c1_provisions),
    }
    c1_json = json.dumps(c1_facts)

    c2_photos = ("photo_lose_1.jpg", "photo_lose_2.jpg")
    c2_provisions = frozenset(["prov_lose_a", "prov_lose_b"])
    c2_facts = {
        "statement": "Loser Statement",
        "photos": list(c2_photos),
        "provisions": sorted(c2_provisions),
    }
    c2_json = json.dumps(c2_facts)

    def run_conflict_submit(
        case_id: str,
        intake_json: str,
        stmt: str,
        photos: tuple[str, ...],
        provisions: frozenset[str],
        prehold: str,
    ) -> tuple[bool, str | Exception]:
        store = SqliteTransactionStore(path)
        try:
            barrier.wait(timeout=5.0)
            rec = store.record_intake_with_workspace_snapshot(
                intake_id=case_id,
                workspace_token="ws_conflict_fresh",
                intake_json=intake_json,
                prehold_json=prehold,
                customer_statement=stmt,
                photo_refs=photos,
                provision_selections=provisions,
                now=LATER,
            )
            ret_id = rec.intake_id
            store.close()
            return True, ret_id
        except Exception as exc:
            store.close()
            return False, exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(
            run_conflict_submit,
            "case_conf_1",
            c1_json,
            "Winner Statement",
            c1_photos,
            c1_provisions,
            '{"prehold":1}',
        )
        f2 = executor.submit(
            run_conflict_submit,
            "case_conf_2",
            c2_json,
            "Loser Statement",
            c2_photos,
            c2_provisions,
            '{"prehold":2}',
        )
        ok1, res1 = f1.result()
        ok2, res2 = f2.result()

    # Exactly one must succeed, and one must fail with IntakeConflict
    assert (ok1 and not ok2) or (ok2 and not ok1)
    err = res2 if ok1 else res1
    assert isinstance(err, IntakeConflict)
    assert "intake_already_submitted_create_new_workspace" in str(err)

    if ok1:
        winning_case = str(res1)
        winning_json = c1_json
        winning_stmt = "Winner Statement"
        winning_photos = c1_photos
        winning_provisions = c1_provisions
        losing_stmt = "Loser Statement"
        losing_photos = c2_photos
        losing_provisions = c2_provisions
    else:
        winning_case = str(res2)
        winning_json = c2_json
        winning_stmt = "Loser Statement"
        winning_photos = c2_photos
        winning_provisions = c2_provisions
        losing_stmt = "Winner Statement"
        losing_photos = c1_photos
        losing_provisions = c1_provisions

    verifier = SqliteTransactionStore(path)
    try:
        intake_rec = verifier.intake_for_workspace("ws_conflict_fresh")
        assert intake_rec is not None
        assert intake_rec.intake_id == winning_case
        assert intake_rec.intake_json == winning_json

        ws = verifier.get_workspace("ws_conflict_fresh")
        assert ws is not None
        assert ws.workspace.position == winning_stmt
        assert ws.workspace.position != losing_stmt
        assert ws.workspace.photo_refs == winning_photos
        assert ws.workspace.provision_selections == winning_provisions
        for photo in losing_photos:
            assert photo not in ws.workspace.photo_refs
        for prov in losing_provisions:
            assert prov not in ws.workspace.provision_selections

        conn = verifier._require_open()
        rows = conn.execute(
            "SELECT intake_id FROM pb_intake_records WHERE workspace_token='ws_conflict_fresh'"
        ).fetchall()
        assert len(rows) == 1

        history = verifier.workspace_history("ws_conflict_fresh")
        assert len(history) == 2
        assert tuple(r.revision for r in history) == (1, 2)
    finally:
        verifier.close()
