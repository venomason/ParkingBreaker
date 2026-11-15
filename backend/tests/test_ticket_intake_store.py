"""Focused checks for the durable vision-intake reservation/claim/confirmation store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.ticket_intake_store import (
    ClaimStatus,
    ConfirmationRead,
    ConfirmationState,
    DiscrepancyStatus,
    IntakeOperation,
    IntakeReservation,
    OperationClaim,
    TicketIntakeConflict,
    TicketIntakeStore,
    claim_ticket_intake_operation,
    complete_ticket_intake_operation,
    initialize_ticket_intake,
    latest_ticket_intake_confirmation,
    read_ticket_intake_confirmation,
    record_ticket_intake_confirmation,
    reserve_ticket_intake,
    review_confirmation_binding,
    ticket_intake_claims,
    ticket_intake_operation_claim,
    ticket_intake_reservation,
)
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
BINDING = "a" * 64
OTHER_BINDING = "b" * 64
IMAGES = "c" * 64
FIELDS = "d" * 64
OTHER_IMAGES = "e" * 64
OTHER_FIELDS = "f" * 64


def _seed(path: Path, token: str = "ws_1", owner: str = "alice") -> SqliteTransactionStore:
    store = SqliteTransactionStore(path)
    store.save_workspace(token, Workspace(notes="synthetic"), NOW)
    store.set_workspace_owner(token, owner, NOW)
    return store


def _reserved(path: Path) -> tuple[SqliteTransactionStore, IntakeReservation]:
    store = _seed(path)
    initialize_ticket_intake(store._require_open())
    reservation = reserve_ticket_intake(store._require_open(), "intake_1", "ws_1", "alice", NOW)
    return store, reservation


def _completed_review(
    store: SqliteTransactionStore,
    intake_id: str,
    request_id: str,
    *,
    images: str = IMAGES,
    fields: str = FIELDS,
) -> OperationClaim:
    binding = review_confirmation_binding(images, fields)
    claim = claim_ticket_intake_operation(
        store._require_open(),
        request_id,
        intake_id,
        IntakeOperation.REVIEW,
        binding,
        NOW,
    )
    assert claim.should_call
    assert claim.claim_token is not None
    return complete_ticket_intake_operation(
        store._require_open(),
        request_id,
        ClaimStatus.SUCCESS,
        '{"review":"synthetic"}',
        '{"tokens":7}',
        NOW,
        claim_token=claim.claim_token,
    )


def test_two_connections_and_restart_allocate_one_stable_reservation(tmp_path: Path) -> None:
    path = tmp_path / "stable.sqlite"
    first = _seed(path)
    initialize_ticket_intake(first._require_open())
    barrier = Barrier(2)

    def reserve(index: int) -> IntakeReservation:
        with SqliteTransactionStore(path) as store:
            initialize_ticket_intake(store._require_open())
            barrier.wait()
            return reserve_ticket_intake(
                store._require_open(),
                f"proposed_{index}",
                "ws_1",
                "alice",
                NOW + timedelta(seconds=index),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        reservations = list(pool.map(reserve, (1, 2)))
    assert {item.intake_id for item in reservations} == {reservations[0].intake_id}
    assert ticket_intake_reservation(first._require_open(), "ws_1") == reservations[0]
    assert first._require_open().execute(
        "SELECT COUNT(*) FROM pb_ticket_intake_reservations"
    ).fetchone() == (1,)
    first.close()
    with SqliteTransactionStore(path) as reopened:
        initialize_ticket_intake(reopened._require_open())
        assert ticket_intake_reservation(reopened._require_open(), "ws_1") == reservations[0]


def test_wrong_owner_unknown_workspace_and_cross_workspace_id_are_refused(
    tmp_path: Path,
) -> None:
    store = _seed(tmp_path / "owners.sqlite")
    database = store._require_open()
    initialize_ticket_intake(database)
    with pytest.raises(TicketIntakeConflict, match="workspace_owned_by_another_customer"):
        reserve_ticket_intake(database, "intake_1", "ws_1", "mallory", NOW)
    with pytest.raises(TicketIntakeConflict, match="workspace_not_found"):
        reserve_ticket_intake(database, "intake_1", "ws_unknown", "alice", NOW)
    first = reserve_ticket_intake(database, "intake_1", "ws_1", "alice", NOW)
    store.save_workspace("ws_2", Workspace(notes="synthetic two"), NOW)
    store.set_workspace_owner("ws_2", "alice", NOW)
    with pytest.raises(TicketIntakeConflict, match="intake_id_already_reserved"):
        reserve_ticket_intake(database, "intake_1", "ws_2", "alice", NOW)
    # The same proposed id is not reused for a different workspace.
    second = reserve_ticket_intake(database, "intake_2", "ws_2", "alice", NOW)
    assert second.intake_id == "intake_2"
    assert first.intake_id == "intake_1"
    assert ticket_intake_reservation(database, "ws_2") == second


def test_claim_replay_conflicting_request_and_binding_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "claims.sqlite"
    store, reservation = _reserved(path)
    database = store._require_open()
    first = claim_ticket_intake_operation(
        database, "req_1", reservation.intake_id, IntakeOperation.EXTRACT, BINDING, NOW
    )
    assert first.should_call
    assert first.claim_token is not None
    assert first.status is ClaimStatus.PENDING
    assert first.completed_at is None
    replay = claim_ticket_intake_operation(
        database, "req_1", reservation.intake_id, IntakeOperation.EXTRACT, BINDING, LATER
    )
    assert replay.request_id == first.request_id
    assert replay.status is ClaimStatus.PENDING
    assert replay.claimed_at == first.claimed_at
    assert not replay.should_call
    assert replay.claim_token is None
    same_attempt_new_request = claim_ticket_intake_operation(
        database, "req_2", reservation.intake_id, IntakeOperation.EXTRACT, BINDING, LATER
    )
    assert same_attempt_new_request.request_id == "req_1"
    assert not same_attempt_new_request.should_call
    with pytest.raises(TicketIntakeConflict, match="request_id_reused_for_another_claim"):
        claim_ticket_intake_operation(
            database, "req_1", reservation.intake_id, IntakeOperation.EXTRACT, OTHER_BINDING, LATER
        )
    with pytest.raises(TicketIntakeConflict, match="intake_not_reserved"):
        claim_ticket_intake_operation(
            database, "req_3", "intake_missing", IntakeOperation.EXTRACT, BINDING, LATER
        )
    store.close()
    with SqliteTransactionStore(path) as reopened:
        initialize_ticket_intake(reopened._require_open())
        database = reopened._require_open()
        assert ticket_intake_operation_claim(database, "req_1") == replay
        pending = claim_ticket_intake_operation(
            database, "req_1", reservation.intake_id, IntakeOperation.EXTRACT, BINDING, LATER
        )
        assert pending.status is ClaimStatus.PENDING
        assert not pending.should_call
        assert len(ticket_intake_claims(database, reservation.intake_id)) == 1


def test_concurrent_claim_has_exactly_one_should_call_owner(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.sqlite"
    store, reservation = _reserved(path)
    barrier = Barrier(2)

    def claim(index: int) -> OperationClaim:
        with SqliteTransactionStore(path) as worker_store:
            barrier.wait()
            return claim_ticket_intake_operation(
                worker_store._require_open(),
                f"req_{index}",
                reservation.intake_id,
                IntakeOperation.EXTRACT,
                BINDING,
                NOW + timedelta(seconds=index),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, (1, 2)))
    assert sum(item.should_call for item in claims) == 1
    assert sum(item.claim_token is not None for item in claims) == 1
    assert len({item.request_id for item in claims}) == 1
    count = (
        store._require_open()
        .execute(
            "SELECT COUNT(*) FROM pb_ticket_intake_operations "
            "WHERE intake_id=? AND operation='extract' AND binding_sha256=?",
            (reservation.intake_id, BINDING),
        )
        .fetchone()
    )
    assert count == (1,)


def test_pending_claim_stays_pending_after_crash_and_never_expires(tmp_path: Path) -> None:
    path = tmp_path / "pending.sqlite"
    store, reservation = _reserved(path)
    claim = claim_ticket_intake_operation(
        store._require_open(),
        "req_pending",
        reservation.intake_id,
        IntakeOperation.EXTRACT,
        BINDING,
        NOW,
    )
    assert claim.should_call
    store.close()
    with SqliteTransactionStore(path) as reopened:
        initialize_ticket_intake(reopened._require_open())
        database = reopened._require_open()
        stored = ticket_intake_operation_claim(database, "req_pending")
        assert stored is not None
        assert stored.status is ClaimStatus.PENDING
        assert stored.completed_at is None
        far_later = NOW + timedelta(days=3650)
        replayed = claim_ticket_intake_operation(
            database,
            "new_request",
            reservation.intake_id,
            IntakeOperation.EXTRACT,
            BINDING,
            far_later,
        )
        assert replayed.request_id == "req_pending"
        assert replayed.status is ClaimStatus.PENDING
        assert not replayed.should_call
        assert ticket_intake_operation_claim(database, "req_pending") == replayed


def test_completion_replays_exactly_and_refuses_contradiction(tmp_path: Path) -> None:
    store, reservation = _reserved(tmp_path / "complete.sqlite")
    database = store._require_open()
    claim = claim_ticket_intake_operation(
        database, "req_1", reservation.intake_id, IntakeOperation.EXTRACT, BINDING, NOW
    )
    assert claim.claim_token is not None
    done = complete_ticket_intake_operation(
        database,
        "req_1",
        ClaimStatus.SUCCESS,
        '{"fields":{"plate":"synthetic"}}',
        '{"total_tokens":42}',
        LATER,
        claim_token=claim.claim_token,
    )
    assert done.status is ClaimStatus.SUCCESS
    assert done.completed_at == LATER
    assert done.result_json == '{"fields":{"plate":"synthetic"}}'
    assert done.usage_json == '{"total_tokens":42}'
    replay = complete_ticket_intake_operation(
        database,
        "req_1",
        ClaimStatus.SUCCESS,
        '{"fields":{"plate":"synthetic"}}',
        '{"total_tokens":42}',
        LATER + timedelta(hours=1),
        claim_token=claim.claim_token,
    )
    assert replay == done
    with pytest.raises(TicketIntakeConflict, match="claim_already_completed_differently"):
        complete_ticket_intake_operation(
            database,
            "req_1",
            ClaimStatus.REFUSED,
            '{"reason":"synthetic"}',
            None,
            LATER + timedelta(hours=1),
        )
    with pytest.raises(TicketIntakeConflict, match="claim_token_mismatch"):
        complete_ticket_intake_operation(
            database,
            "req_1",
            ClaimStatus.SUCCESS,
            '{"fields":{"plate":"synthetic"}}',
            '{"total_tokens":42}',
            LATER,
            claim_token="not-the-token",
        )
    with pytest.raises(ValueError, match="unknown completion has no result"):
        complete_ticket_intake_operation(database, "req_2", ClaimStatus.UNKNOWN, "{}", None, LATER)
    unknown_claim = claim_ticket_intake_operation(
        database, "req_unknown", reservation.intake_id, IntakeOperation.EXTRACT, OTHER_BINDING, NOW
    )
    assert unknown_claim.claim_token is not None
    unknown = complete_ticket_intake_operation(
        database,
        "req_unknown",
        ClaimStatus.UNKNOWN,
        None,
        None,
        LATER,
        claim_token=unknown_claim.claim_token,
    )
    assert unknown.status is ClaimStatus.UNKNOWN
    assert unknown.result_json is None
    assert unknown.usage_json is None
    retried = claim_ticket_intake_operation(
        database,
        "req_unknown_retry",
        reservation.intake_id,
        IntakeOperation.EXTRACT,
        OTHER_BINDING,
        LATER,
    )
    assert retried.request_id == "req_unknown"
    assert retried.status is ClaimStatus.UNKNOWN
    assert not retried.should_call


def test_confirmation_is_bound_and_reports_each_stale_dimension(tmp_path: Path) -> None:
    store, reservation = _reserved(tmp_path / "confirm.sqlite")
    database = store._require_open()
    review = _completed_review(store, reservation.intake_id, "review_1")
    other_review = _completed_review(store, reservation.intake_id, "review_2", images=OTHER_IMAGES)
    confirmation = record_ticket_intake_confirmation(
        database,
        "command_1",
        reservation.intake_id,
        IMAGES,
        FIELDS,
        review.request_id,
        1,
        1,
        DiscrepancyStatus.RESOLVED,
        LATER,
    )
    assert confirmation.verified is False
    assert confirmation.discrepancy_status is DiscrepancyStatus.RESOLVED
    assert latest_ticket_intake_confirmation(database, reservation.intake_id) == confirmation
    current = read_ticket_intake_confirmation(
        database,
        reservation.intake_id,
        images_sha256=IMAGES,
        fields_sha256=FIELDS,
        revision=1,
        generation=1,
        review_request_id=review.request_id,
    )
    assert current.state is ConfirmationState.CURRENT
    assert current.current
    assert current.confirmation == confirmation

    def read(
        *,
        images_sha256: str = IMAGES,
        fields_sha256: str = FIELDS,
        revision: int = 1,
        generation: int = 1,
        review_request_id: str = review.request_id,
    ) -> ConfirmationRead:
        return read_ticket_intake_confirmation(
            database,
            reservation.intake_id,
            images_sha256=images_sha256,
            fields_sha256=fields_sha256,
            revision=revision,
            generation=generation,
            review_request_id=review_request_id,
        )

    for stale, reason in (
        (read(images_sha256=OTHER_IMAGES), "images_changed"),
        (read(fields_sha256=OTHER_FIELDS), "fields_changed"),
        (read(review_request_id=other_review.request_id), "review_changed"),
        (read(revision=2), "revision_changed"),
        (read(generation=2), "generation_changed"),
    ):
        assert stale.state is ConfirmationState.STALE
        assert stale.reason == reason
        assert stale.stale

    replay = record_ticket_intake_confirmation(
        database,
        "command_1",
        reservation.intake_id,
        IMAGES,
        FIELDS,
        review.request_id,
        1,
        1,
        DiscrepancyStatus.RESOLVED,
        LATER + timedelta(hours=1),
    )
    assert replay == confirmation
    with pytest.raises(TicketIntakeConflict, match="confirmation_command_reused"):
        record_ticket_intake_confirmation(
            database,
            "command_1",
            reservation.intake_id,
            OTHER_IMAGES,
            FIELDS,
            review.request_id,
            1,
            1,
            DiscrepancyStatus.RESOLVED,
            LATER + timedelta(hours=1),
        )
    replacement = record_ticket_intake_confirmation(
        database,
        "command_2",
        reservation.intake_id,
        OTHER_IMAGES,
        FIELDS,
        other_review.request_id,
        1,
        2,
        DiscrepancyStatus.NONE,
        LATER + timedelta(hours=2),
    )
    replaced = read_ticket_intake_confirmation(
        database,
        reservation.intake_id,
        images_sha256=OTHER_IMAGES,
        fields_sha256=FIELDS,
        revision=1,
        generation=2,
        review_request_id=other_review.request_id,
    )
    assert replaced.state is ConfirmationState.CURRENT
    assert replaced.confirmation == replacement
    old = read_ticket_intake_confirmation(
        database,
        reservation.intake_id,
        images_sha256=IMAGES,
        fields_sha256=FIELDS,
        revision=1,
        generation=1,
        review_request_id=review.request_id,
    )
    assert old.state is ConfirmationState.STALE


def test_confirmation_requires_completed_review_and_has_no_reason_field(tmp_path: Path) -> None:
    store, reservation = _reserved(tmp_path / "confirm-requires.sqlite")
    database = store._require_open()
    with pytest.raises(TicketIntakeConflict, match="review_not_found"):
        record_ticket_intake_confirmation(
            database,
            "command_1",
            reservation.intake_id,
            IMAGES,
            FIELDS,
            "missing_review",
            1,
            1,
            DiscrepancyStatus.NONE,
            NOW,
        )
    pending_review = claim_ticket_intake_operation(
        database, "review_pending", reservation.intake_id, IntakeOperation.REVIEW, BINDING, NOW
    )
    assert pending_review.claim_token is not None
    with pytest.raises(TicketIntakeConflict, match="review_result_not_available"):
        record_ticket_intake_confirmation(
            database,
            "command_2",
            reservation.intake_id,
            IMAGES,
            FIELDS,
            pending_review.request_id,
            1,
            1,
            DiscrepancyStatus.NONE,
            NOW,
        )
    facade = TicketIntakeStore(store)
    with pytest.raises(TypeError):
        facade.record_confirmation(
            "command_3",
            reservation.intake_id,
            IMAGES,
            FIELDS,
            pending_review.request_id,
            1,
            1,
            DiscrepancyStatus.NONE,
            NOW,
            reason="customer text must not be accepted",  # type: ignore[call-arg]
        )


def test_schema_version_malformed_rows_and_unrelated_tables(tmp_path: Path) -> None:
    path = tmp_path / "schema.sqlite"
    store = _seed(path)
    database = store._require_open()
    before_tables = {
        row[0]: row[1]
        for row in database.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'pb_ticket_intake_%'"
        )
    }
    before_versions = {
        row[0]: row[1]
        for row in database.execute(
            "SELECT component, version FROM pb_store_versions WHERE component != 'ticket_intake'"
        )
    }
    initialize_ticket_intake(database)
    initialize_ticket_intake(database)
    after_tables = {
        row[0]: row[1]
        for row in database.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'pb_ticket_intake_%'"
        )
    }
    after_versions = {
        row[0]: row[1]
        for row in database.execute(
            "SELECT component, version FROM pb_store_versions WHERE component != 'ticket_intake'"
        )
    }
    assert after_tables == before_tables
    assert after_versions == before_versions
    assert database.execute(
        "SELECT version FROM pb_store_versions WHERE component='ticket_intake'"
    ).fetchone() == (1,)
    database.execute("UPDATE pb_store_versions SET version=2 WHERE component='ticket_intake'")
    database.commit()
    with pytest.raises(StorageCorruption, match="malformed ticket intake schema"):
        initialize_ticket_intake(database)
    database.execute("UPDATE pb_store_versions SET version=1 WHERE component='ticket_intake'")
    database.execute("DROP TABLE pb_ticket_intake_confirmations")
    database.commit()
    with pytest.raises(StorageCorruption, match="malformed ticket intake schema"):
        initialize_ticket_intake(database)
    store.close()

    malformed = tmp_path / "malformed.sqlite"
    database = sqlite3.connect(malformed)
    database.execute(
        "CREATE TABLE pb_store_versions (component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
    )
    database.execute(
        "CREATE TABLE pb_intake_owners (workspace_token TEXT PRIMARY KEY, owner TEXT NOT NULL)"
    )
    database.execute("CREATE TABLE pb_ticket_intake_reservations (wrong TEXT)")
    database.execute("INSERT INTO pb_store_versions VALUES ('ticket_intake', 1)")
    database.commit()
    with pytest.raises(StorageCorruption, match="malformed ticket intake schema"):
        initialize_ticket_intake(database)
    database.close()


def test_stored_claim_and_confirmation_rows_are_validated(tmp_path: Path) -> None:
    store, reservation = _reserved(tmp_path / "rows.sqlite")
    database = store._require_open()
    review = _completed_review(store, reservation.intake_id, "review_1")
    record_ticket_intake_confirmation(
        database,
        "command_1",
        reservation.intake_id,
        IMAGES,
        FIELDS,
        review.request_id,
        1,
        1,
        DiscrepancyStatus.NONE,
        LATER,
    )
    database.execute("DROP TRIGGER pb_ticket_intake_operations_complete_once")
    database.execute("DROP TRIGGER pb_ticket_intake_confirmations_no_update")
    database.execute("PRAGMA ignore_check_constraints=ON")
    database.execute(
        "UPDATE pb_ticket_intake_operations SET binding_sha256='not-a-digest' WHERE request_id=?",
        (review.request_id,),
    )
    database.commit()
    with pytest.raises(StorageCorruption, match="malformed ticket intake claim"):
        ticket_intake_operation_claim(database, review.request_id)
    database.execute(
        "UPDATE pb_ticket_intake_confirmations SET verified=1 WHERE command_id='command_1'"
    )
    database.commit()
    with pytest.raises(StorageCorruption, match="malformed ticket intake confirmation"):
        latest_ticket_intake_confirmation(database, reservation.intake_id)
    database.execute(
        "UPDATE pb_ticket_intake_confirmations SET verified=0, discrepancy_status='invented' "
        "WHERE command_id='command_1'"
    )
    database.commit()
    with pytest.raises(StorageCorruption, match="malformed ticket intake confirmation"):
        latest_ticket_intake_confirmation(database, reservation.intake_id)


def test_review_confirmation_binding_is_domain_separated() -> None:
    expected = hashlib.sha256(
        json.dumps(["ticket-review-binding-v1", IMAGES, FIELDS], separators=(",", ":")).encode(
            "ascii"
        )
    ).hexdigest()
    assert review_confirmation_binding(IMAGES, FIELDS) == expected
    assert review_confirmation_binding(IMAGES.upper(), FIELDS.upper()) == expected
    assert review_confirmation_binding(OTHER_IMAGES, FIELDS) != expected
    assert review_confirmation_binding(IMAGES, OTHER_FIELDS) != expected


def test_pending_completion_requires_creator_claim_token(tmp_path: Path) -> None:
    store, reservation = _reserved(tmp_path / "token.sqlite")
    database = store._require_open()
    claim = claim_ticket_intake_operation(
        database, "req_token", reservation.intake_id, IntakeOperation.EXTRACT, BINDING, NOW
    )
    assert claim.claim_token is not None
    with pytest.raises(TicketIntakeConflict, match="claim_token_required"):
        complete_ticket_intake_operation(
            database, "req_token", ClaimStatus.SUCCESS, '{"synthetic":true}', None, LATER
        )
    with pytest.raises(TicketIntakeConflict, match="claim_token_mismatch"):
        complete_ticket_intake_operation(
            database,
            "req_token",
            ClaimStatus.SUCCESS,
            '{"synthetic":true}',
            None,
            LATER,
            claim_token="wrong-token",
        )
    done = complete_ticket_intake_operation(
        database,
        "req_token",
        ClaimStatus.SUCCESS,
        '{"synthetic":true}',
        None,
        LATER,
        claim_token=claim.claim_token,
    )
    assert done.status is ClaimStatus.SUCCESS
    assert done.claim_token is None
    replay = complete_ticket_intake_operation(
        database,
        "req_token",
        ClaimStatus.SUCCESS,
        '{"synthetic":true}',
        None,
        LATER + timedelta(hours=1),
        claim_token=claim.claim_token,
    )
    assert replay == done
    tokenless_exact = complete_ticket_intake_operation(
        database, "req_token", ClaimStatus.SUCCESS, '{"synthetic":true}', None, LATER
    )
    assert tokenless_exact == done
    with pytest.raises(TicketIntakeConflict, match="claim_already_completed_differently"):
        complete_ticket_intake_operation(
            database, "req_token", ClaimStatus.SUCCESS, '{"synthetic":false}', None, LATER
        )
    assert ticket_intake_operation_claim(database, "req_token") == done


def test_new_request_id_aliases_to_original_operation(tmp_path: Path) -> None:
    path = tmp_path / "alias.sqlite"
    store, reservation = _reserved(path)
    database = store._require_open()
    original = claim_ticket_intake_operation(
        database, "request-a", reservation.intake_id, IntakeOperation.EXTRACT, BINDING, NOW
    )
    assert original.claim_token is not None
    alias = claim_ticket_intake_operation(
        database, "request-b", reservation.intake_id, IntakeOperation.EXTRACT, BINDING, LATER
    )
    assert alias.request_id == "request-a"
    assert not alias.should_call
    assert alias.claim_token is None
    assert ticket_intake_operation_claim(database, "request-b") == alias
    with pytest.raises(TicketIntakeConflict, match="request_id_reused_for_another_claim"):
        claim_ticket_intake_operation(
            database,
            "request-b",
            reservation.intake_id,
            IntakeOperation.EXTRACT,
            OTHER_BINDING,
            LATER,
        )
    with pytest.raises(TicketIntakeConflict, match="request_id_reused_for_another_claim"):
        claim_ticket_intake_operation(
            database, "request-b", reservation.intake_id, IntakeOperation.REVIEW, BINDING, LATER
        )
    store.save_workspace("ws_2", Workspace(notes="synthetic two"), NOW)
    store.set_workspace_owner("ws_2", "alice", NOW)
    second = reserve_ticket_intake(database, "intake_2", "ws_2", "alice", NOW)
    with pytest.raises(TicketIntakeConflict, match="request_id_reused_for_another_claim"):
        claim_ticket_intake_operation(
            database, "request-b", second.intake_id, IntakeOperation.EXTRACT, BINDING, LATER
        )
    store.close()
    with SqliteTransactionStore(path) as reopened:
        initialize_ticket_intake(reopened._require_open())
        database = reopened._require_open()
        replayed = claim_ticket_intake_operation(
            database, "request-b", reservation.intake_id, IntakeOperation.EXTRACT, BINDING, LATER
        )
        assert replayed.request_id == "request-a"
        assert replayed.claim_token is None
        with pytest.raises(TicketIntakeConflict, match="request_id_reused_for_another_claim"):
            claim_ticket_intake_operation(
                database,
                "request-b",
                reservation.intake_id,
                IntakeOperation.EXTRACT,
                OTHER_BINDING,
                LATER,
            )
        with pytest.raises(TicketIntakeConflict, match="claim_token_required"):
            complete_ticket_intake_operation(
                database, "request-b", ClaimStatus.SUCCESS, '{"synthetic":true}', None, LATER
            )


def test_concurrent_new_request_ids_converge_on_one_operation(tmp_path: Path) -> None:
    path = tmp_path / "alias-race.sqlite"
    store, reservation = _reserved(path)
    barrier = Barrier(2)

    def claim(index: int) -> OperationClaim:
        with SqliteTransactionStore(path) as worker_store:
            barrier.wait()
            return claim_ticket_intake_operation(
                worker_store._require_open(),
                f"request-race-{index}",
                reservation.intake_id,
                IntakeOperation.EXTRACT,
                BINDING,
                NOW + timedelta(seconds=index),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, (1, 2)))
    assert len({item.request_id for item in claims}) == 1
    assert sum(item.claim_token is not None for item in claims) == 1
    assert sum(item.should_call for item in claims) == 1
    canonical = claims[0].request_id
    aliases = {
        row[0]
        for row in store._require_open().execute(
            "SELECT request_id FROM pb_ticket_intake_operation_aliases"
        )
    }
    assert aliases == {"request-race-1", "request-race-2"} - {canonical}
    store.close()
    with SqliteTransactionStore(path) as reopened:
        initialize_ticket_intake(reopened._require_open())
        again = claim_ticket_intake_operation(
            reopened._require_open(),
            canonical,
            reservation.intake_id,
            IntakeOperation.EXTRACT,
            BINDING,
            LATER,
        )
        assert again.request_id == canonical
        assert again.claim_token is None


def test_confirmation_refuses_stale_and_conflicting_same_generation(tmp_path: Path) -> None:
    store, reservation = _reserved(tmp_path / "generation.sqlite")
    database = store._require_open()
    review = _completed_review(store, reservation.intake_id, "review-current", images=IMAGES)
    record_ticket_intake_confirmation(
        database,
        "command-current",
        reservation.intake_id,
        IMAGES,
        FIELDS,
        review.request_id,
        2,
        2,
        DiscrepancyStatus.RESOLVED,
        LATER,
    )
    changed_review = _completed_review(
        store, reservation.intake_id, "review-changed", images=OTHER_IMAGES
    )
    with pytest.raises(TicketIntakeConflict, match="confirmation_revision_stale"):
        record_ticket_intake_confirmation(
            database,
            "command-stale",
            reservation.intake_id,
            OTHER_IMAGES,
            FIELDS,
            changed_review.request_id,
            1,
            1,
            DiscrepancyStatus.RESOLVED,
            LATER,
        )
    with pytest.raises(TicketIntakeConflict, match="confirmation_generation_conflict"):
        record_ticket_intake_confirmation(
            database,
            "command-overwrite",
            reservation.intake_id,
            OTHER_IMAGES,
            FIELDS,
            changed_review.request_id,
            2,
            2,
            DiscrepancyStatus.RESOLVED,
            LATER,
        )
    latest = latest_ticket_intake_confirmation(database, reservation.intake_id)
    assert latest is not None
    assert latest.command_id == "command-current"
    advanced = record_ticket_intake_confirmation(
        database,
        "command-advanced",
        reservation.intake_id,
        OTHER_IMAGES,
        FIELDS,
        changed_review.request_id,
        2,
        3,
        DiscrepancyStatus.NONE,
        LATER + timedelta(hours=1),
    )
    assert advanced.generation == 3
    assert advanced.revision == 2
    assert latest_ticket_intake_confirmation(database, reservation.intake_id) == advanced
    newer = record_ticket_intake_confirmation(
        database,
        "command-newer",
        reservation.intake_id,
        OTHER_IMAGES,
        FIELDS,
        changed_review.request_id,
        3,
        4,
        DiscrepancyStatus.NONE,
        LATER + timedelta(hours=2),
    )
    assert (newer.revision, newer.generation) == (3, 4)
    assert latest_ticket_intake_confirmation(database, reservation.intake_id) == newer
    with pytest.raises(TicketIntakeConflict, match="confirmation_revision_stale"):
        record_ticket_intake_confirmation(
            database,
            "command-generation-only",
            reservation.intake_id,
            OTHER_IMAGES,
            FIELDS,
            changed_review.request_id,
            2,
            5,
            DiscrepancyStatus.NONE,
            LATER + timedelta(hours=3),
        )
    with pytest.raises(TicketIntakeConflict, match="confirmation_generation_conflict"):
        record_ticket_intake_confirmation(
            database,
            "command-same-generation",
            reservation.intake_id,
            OTHER_IMAGES,
            FIELDS,
            changed_review.request_id,
            3,
            4,
            DiscrepancyStatus.NONE,
            LATER + timedelta(hours=3),
        )
    assert latest_ticket_intake_confirmation(database, reservation.intake_id) == newer


def test_confirmation_requires_review_bound_to_its_images_and_fields(tmp_path: Path) -> None:
    store, reservation = _reserved(tmp_path / "review-binding.sqlite")
    database = store._require_open()
    review = _completed_review(
        store, reservation.intake_id, "review-bound", images=IMAGES, fields=FIELDS
    )
    for command, images, fields in (
        ("command-fields", IMAGES, OTHER_FIELDS),
        ("command-images", OTHER_IMAGES, FIELDS),
    ):
        with pytest.raises(TicketIntakeConflict, match="review_binding_mismatch"):
            record_ticket_intake_confirmation(
                database,
                command,
                reservation.intake_id,
                images,
                fields,
                review.request_id,
                1,
                1,
                DiscrepancyStatus.NONE,
                LATER,
            )
    confirmation = record_ticket_intake_confirmation(
        database,
        "command-exact",
        reservation.intake_id,
        IMAGES,
        FIELDS,
        review.request_id,
        1,
        1,
        DiscrepancyStatus.RESOLVED,
        LATER,
    )
    read = read_ticket_intake_confirmation(
        database,
        reservation.intake_id,
        images_sha256=IMAGES,
        fields_sha256=FIELDS,
        revision=1,
        generation=1,
        review_request_id=review.request_id,
    )
    assert read.state is ConfirmationState.CURRENT
    assert read.confirmation == confirmation
    database.execute("DROP TRIGGER pb_ticket_intake_operations_complete_once")
    database.execute(
        "UPDATE pb_ticket_intake_operations SET binding_sha256=? WHERE request_id=?",
        (review_confirmation_binding(OTHER_IMAGES, FIELDS), review.request_id),
    )
    database.commit()
    with pytest.raises(StorageCorruption, match="ticket intake confirmation review mismatch"):
        latest_ticket_intake_confirmation(database, reservation.intake_id)
