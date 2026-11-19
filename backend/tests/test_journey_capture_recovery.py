"""Independent-review gaps: pending REVIEW, legacy replay and lost commit acknowledgement."""

import hashlib
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from parkingbreaker.journey_events import CaptureStartBasis
from parkingbreaker.ticket_intake_store import (
    ClaimStatus,
    IntakeOperation,
    claim_ticket_intake_operation,
    complete_ticket_intake_operation,
    reserve_ticket_intake,
)
from parkingbreaker.transaction_records import workspace_json
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_journey_events import NOW, _HookedConnection


def _legacy_workspace(store: SqliteTransactionStore) -> None:
    store._require_open().execute(
        "INSERT INTO pb_workspaces VALUES (?, ?, ?, 1)",
        ("legacy", workspace_json(Workspace(notes="")), NOW.isoformat()),
    )
    store.set_workspace_owner("legacy", "alice", NOW)
    reserve_ticket_intake(store._require_open(), "intake", "legacy", "alice", NOW)


def test_first_pending_review_records_start_without_success(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "review.sqlite") as store:
        _legacy_workspace(store)
        claim = claim_ticket_intake_operation(
            store._require_open(), "review", "intake", IntakeOperation.REVIEW, "a" * 64, NOW
        )
        assert claim.status is ClaimStatus.PENDING
        capture = store.journey_capture(workspace_token="legacy", owner="alice")
        assert capture is not None
        assert capture.start_basis is CaptureStartBasis.FIRST_DURABLE_ATTEMPT
        assert capture.started_at == NOW
        assert store.journey_events(workspace_token="legacy").events == ()
        replay = claim_ticket_intake_operation(
            store._require_open(),
            "review",
            "intake",
            IntakeOperation.REVIEW,
            "a" * 64,
            NOW + timedelta(hours=1),
        )
        assert not replay.should_call
        assert store.journey_capture(workspace_token="legacy") == capture


def test_pre_migration_pending_replay_does_not_backfill_attempt_start(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "pending.sqlite") as store:
        _legacy_workspace(store)
        db = store._require_open()
        capability = "c" * 43
        db.execute(
            "INSERT INTO pb_ticket_intake_operations "
            "(request_id, intake_id, workspace_token, owner, operation, binding_sha256, "
            "claim_token_sha256, status, result_json, usage_json, claimed_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, NULL)",
            (
                "old",
                "intake",
                "legacy",
                "alice",
                "extract",
                "a" * 64,
                hashlib.sha256(capability.encode()).hexdigest(),
                NOW.isoformat(),
            ),
        )
        replay = claim_ticket_intake_operation(
            db, "old", "intake", IntakeOperation.EXTRACT, "a" * 64, NOW + timedelta(hours=1)
        )
        assert replay.status is ClaimStatus.PENDING
        assert not replay.should_call
        assert store.journey_capture(workspace_token="legacy") is None
        complete_ticket_intake_operation(
            db,
            "old",
            ClaimStatus.UNKNOWN,
            None,
            None,
            NOW + timedelta(hours=2),
            claim_token=capability,
        )
        capture = store.journey_capture(workspace_token="legacy", owner="alice")
        assert capture is not None
        (event,) = store.journey_events(workspace_token="legacy").events
        assert capture.start_basis is CaptureStartBasis.FIRST_DURABLE_EVENT
        assert capture.started_at == event.recorded_at
        assert capture.started_at != replay.claimed_at
        assert event.outcome.value == "unknown"
        assert db.execute("SELECT COUNT(*) FROM pb_ticket_intake_operations").fetchone() == (1,)


class _LostCommitAcknowledgement(_HookedConnection):
    lose_acknowledgement = True

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        result = super().execute(sql, parameters)
        if sql.strip().upper() == "COMMIT" and self.lose_acknowledgement:
            self.lose_acknowledgement = False
            raise RuntimeError("commit acknowledgement lost")
        return result


def test_committed_creation_reconciles_after_lost_ack_without_duplicate(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "ack.sqlite") as store:
        hooked = _LostCommitAcknowledgement(store._require_open())
        store._database = hooked  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="commit acknowledgement lost"):
            store.create_owned_workspace("alice", "original", Workspace(notes=""), NOW)
        assert not hooked.in_transaction
        capture = store.journey_capture(workspace_token="original", owner="alice")
        assert capture is not None
        saved = store.get_workspace("original")
        assert saved is not None
        replay = store.create_owned_workspace(
            "alice", "new-proposal", Workspace(notes=""), NOW + timedelta(hours=1)
        )
        assert replay == saved
        assert store.journey_capture(workspace_token="original") == capture
        assert store.journey_capture(workspace_token="new-proposal") is None
        for table in (
            "pb_workspaces",
            "pb_workspace_history",
            "pb_intake_owners",
            "pb_journey_links",
            "pb_journey_capture",
        ):
            assert hooked.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (1,)
