"""T02 capture-start evidence stays atomic, scoped and truthful across migration."""

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from parkingbreaker.journey_events import (
    CAPTURE_COMPONENT,
    CaptureStartBasis,
    CostStatus,
    JourneyBuildIdentity,
    JourneyEventAccessError,
    JourneyEventInput,
    JourneyEventKind,
    JourneyEventOutcome,
    ensure_journey_identity_in_transaction,
    record_journey_event,
)
from parkingbreaker.journey_timeline import CaptureCoverage
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_journey_events import NOW, _HookedConnection


def test_workspace_identity_before_events_and_restart_replay(tmp_path: Path) -> None:
    path = tmp_path / "capture.sqlite"
    identity = JourneyBuildIdentity("a" * 64, "b" * 64)
    with SqliteTransactionStore(path, journey_identity=identity) as store:
        saved = store.create_owned_workspace("alice", "ws", Workspace(notes=""), NOW)
        capture = store.journey_capture(workspace_token="ws", owner="alice")
        assert capture is not None
        assert capture.start_basis is CaptureStartBasis.WORKSPACE_CREATED
        assert capture.started_at == NOW
        assert capture.identity == identity
        page = store.journey_events(workspace_token="ws", owner="alice")
        assert page.journey_id == capture.journey_id
        assert page.events == ()
        assert store.journey_timeline(workspace_token="ws").coverage is CaptureCoverage.LIMITED
        assert (
            store.create_owned_workspace(
                "alice", "different-token", Workspace(notes=""), NOW + timedelta(minutes=2)
            )
            == saved
        )
        assert store.journey_capture(workspace_token="different-token") is None
        store.save_workspace("ws", Workspace(notes="edited"), NOW, expected_revision=1)
        assert store.journey_capture(workspace_token="ws") == capture
        with pytest.raises(JourneyEventAccessError):
            store.journey_capture(workspace_token="ws", owner="bob")
        with pytest.raises(JourneyEventAccessError):
            store.journey_capture(workspace_token="missing", owner="alice")
    with SqliteTransactionStore(path, journey_identity=JourneyBuildIdentity("c" * 64)) as store:
        assert store.journey_capture(workspace_token="ws", owner="alice") == capture


def test_creation_rollback_removes_identity_and_capture(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "rollback.sqlite") as store:
        hooked = _HookedConnection(store._require_open())
        store._database = hooked  # type: ignore[assignment]
        hooked.raise_before_commit = RuntimeError("fault before commit")
        with pytest.raises(RuntimeError, match="fault before commit"):
            store.create_owned_workspace("alice", "ws", Workspace(notes=""), NOW)
        assert store.get_workspace("ws") is None
        assert store.workspace_owner("ws") is None
        assert store.journey_capture(workspace_token="ws") is None
        assert hooked.execute("SELECT COUNT(*) FROM pb_journey_capture").fetchone() == (0,)
        store.create_owned_workspace("alice", "ws", Workspace(notes=""), NOW)
        assert store.journey_capture(workspace_token="ws") is not None


def test_old_ledger_migrates_without_invented_provenance(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    with SqliteTransactionStore(path) as store:
        store.create_owned_workspace("alice", "ws", Workspace(notes=""), NOW)
        event = record_journey_event(
            store._require_open(),
            workspace_token="ws",
            occurrence_key="old:1",
            event=JourneyEventInput(
                kind=JourneyEventKind.TICKET_READ,
                outcome=JourneyEventOutcome.SUCCEEDED,
                occurred_at=NOW,
                cost_status=CostStatus.UNKNOWN,
            ),
        )
        old_events = store._require_open().execute("SELECT * FROM pb_journey_events").fetchall()
        old_links = store._require_open().execute("SELECT * FROM pb_journey_links").fetchall()
    # Exact pre-capture schema fixture, not deleting business evidence in application code.
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE pb_journey_capture")
        db.execute("DELETE FROM pb_store_versions WHERE component=?", (CAPTURE_COMPONENT,))
    with SqliteTransactionStore(path, journey_identity=JourneyBuildIdentity("a" * 64)) as store:
        capture = store.journey_capture(workspace_token="ws")
        assert capture is not None
        assert capture.journey_id == event.journey_id
        assert capture.start_basis is CaptureStartBasis.LEGACY_UNKNOWN
        assert capture.started_at is None
        assert capture.identity == JourneyBuildIdentity()
        assert (
            store._require_open().execute("SELECT * FROM pb_journey_events").fetchall()
            == old_events
        )
        assert (
            store._require_open().execute("SELECT * FROM pb_journey_links").fetchall() == old_links
        )
        assert store.journey_timeline(workspace_token="ws").coverage is CaptureCoverage.LIMITED
    with SqliteTransactionStore(path) as store:
        assert store.journey_capture(workspace_token="ws") == capture


def test_first_durable_event_fallback_is_not_workspace_capture(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "fallback.sqlite") as store:
        event = record_journey_event(
            store._require_open(),
            workspace_token="legacy",
            occurrence_key="fallback:1",
            event=JourneyEventInput(
                kind=JourneyEventKind.TICKET_READ,
                outcome=JourneyEventOutcome.SUCCEEDED,
                occurred_at=NOW,
                recorded_at=NOW + timedelta(days=1),
                cost_status=CostStatus.UNKNOWN,
            ),
        )
        capture = store.journey_capture(workspace_token="legacy")
        assert capture is not None
        assert capture.journey_id == event.journey_id
        assert capture.started_at == event.recorded_at
        assert capture.start_basis is CaptureStartBasis.FIRST_DURABLE_EVENT
        assert capture.identity == JourneyBuildIdentity()


@pytest.mark.parametrize("value", ["secret", "a" * 63, "A" * 64, 1, True])
def test_identity_accepts_only_nonsecret_digest_shape(value: object) -> None:
    with pytest.raises(ValueError, match="only SHA-256 digests or unknown"):
        JourneyBuildIdentity(value)  # type: ignore[arg-type]


def test_capture_is_immutable_and_requires_caller_transaction(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "immutable.sqlite") as store:
        db = store._require_open()
        with pytest.raises(StorageCorruption, match="active transaction"):
            ensure_journey_identity_in_transaction(
                db, workspace_token="ws", at=NOW, basis=CaptureStartBasis.WORKSPACE_CREATED
            )
        store.save_workspace("ws", Workspace(notes=""), NOW)
        for sql in (
            "UPDATE pb_journey_capture SET started_at=NULL",
            "DELETE FROM pb_journey_capture",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(sql)


def test_unversioned_capture_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.sqlite"
    with SqliteTransactionStore(path):
        pass
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM pb_store_versions WHERE component=?", (CAPTURE_COMPONENT,))
    with pytest.raises(StorageCorruption, match="unversioned journey capture"):
        SqliteTransactionStore(path)


def test_capture_migration_failure_rolls_back_schema_and_version(tmp_path: Path) -> None:
    path = tmp_path / "migration-failure.sqlite"
    with SqliteTransactionStore(path) as store:
        store.save_workspace("old", Workspace(notes=""), NOW)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE pb_journey_capture")
        db.execute("DELETE FROM pb_store_versions WHERE component=?", (CAPTURE_COMPONENT,))
    db = sqlite3.connect(path, isolation_level=None)
    try:
        from parkingbreaker.journey_events import initialize_journey_events

        def deny_capture_insert(action: int, first: str | None, *_rest: object) -> int:
            if action == sqlite3.SQLITE_INSERT and first == "pb_journey_capture":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(deny_capture_insert)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            initialize_journey_events(db)
        db.set_authorizer(None)
        assert not db.in_transaction
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='pb_journey_capture'").fetchone()
            is None
        )
        assert (
            db.execute(
                "SELECT 1 FROM pb_store_versions WHERE component=?", (CAPTURE_COMPONENT,)
            ).fetchone()
            is None
        )
        assert db.execute("SELECT COUNT(*) FROM pb_journey_links").fetchone() == (1,)
        initialize_journey_events(db)
    finally:
        db.close()
    with SqliteTransactionStore(path) as store:
        capture = store.journey_capture(workspace_token="old")
        assert capture is not None
        assert capture.start_basis is CaptureStartBasis.LEGACY_UNKNOWN


def test_legacy_workspace_first_pending_attempt_survives_replay_and_restart(tmp_path: Path) -> None:
    from parkingbreaker.ticket_intake_store import (
        ClaimStatus,
        IntakeOperation,
        claim_ticket_intake_operation,
        reserve_ticket_intake,
    )
    from parkingbreaker.transaction_records import workspace_json

    path = tmp_path / "legacy-attempt.sqlite"
    with SqliteTransactionStore(path) as store:
        db = store._require_open()
        # Pre-journey workspace fixture, created without modern capture provenance.
        db.execute(
            "INSERT INTO pb_workspaces VALUES (?, ?, ?, 1)",
            ("old", workspace_json(Workspace(notes="")), NOW.isoformat()),
        )
        store.set_workspace_owner("old", "alice", NOW)
        assert store.journey_capture(workspace_token="old") is None
        reserve_ticket_intake(db, "intake", "old", "alice", NOW)
        hooked = _HookedConnection(db)
        hooked.raise_before_commit = RuntimeError("before attempt commit")
        with pytest.raises(RuntimeError, match="before attempt commit"):
            claim_ticket_intake_operation(
                cast(sqlite3.Connection, hooked),
                "req",
                "intake",
                IntakeOperation.EXTRACT,
                "a" * 64,
                NOW,
            )
        assert store.journey_capture(workspace_token="old") is None
        assert db.execute("SELECT COUNT(*) FROM pb_ticket_intake_operations").fetchone() == (0,)
        claim = claim_ticket_intake_operation(
            db, "req", "intake", IntakeOperation.EXTRACT, "a" * 64, NOW
        )
        assert claim.status is ClaimStatus.PENDING
        capture = store.journey_capture(workspace_token="old", owner="alice")
        assert capture is not None
        assert capture.start_basis is CaptureStartBasis.FIRST_DURABLE_ATTEMPT
        assert capture.started_at == NOW
        assert store.journey_events(workspace_token="old").events == ()
        claim_ticket_intake_operation(
            db, "alias", "intake", IntakeOperation.EXTRACT, "a" * 64, NOW + timedelta(hours=1)
        )
        assert store.journey_capture(workspace_token="old") == capture
    with SqliteTransactionStore(path) as store:
        assert store.journey_capture(workspace_token="old", owner="alice") == capture
        assert store.journey_timeline(workspace_token="old").coverage is CaptureCoverage.LIMITED
