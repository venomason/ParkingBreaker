"""Closed context cannot rewrite legacy facts, leak identifiers or survive rollback alone."""

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from parkingbreaker.journey_context import (
    COMPONENT,
    ContextFactRoute,
    ContextFunding,
    ContextOperation,
    ContextReason,
    DurationBasis,
    JourneyEventContext,
    JourneyReference,
    ReferenceState,
    opaque_reference,
)
from parkingbreaker.journey_events import (
    CostStatus,
    JourneyEvent,
    JourneyEventAccessError,
    JourneyEventConflict,
    JourneyEventInput,
    JourneyEventKind,
    JourneyEventOutcome,
    read_journey_events,
    record_journey_event,
)
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_journey_events import NOW, _HookedConnection


def context() -> JourneyEventContext:
    return JourneyEventContext(
        operation=ContextOperation.OCR_EXTRACT,
        reason=ContextReason.NOT_APPLICABLE,
        fact_route=ContextFactRoute.NOT_APPLICABLE,
        duration_basis=DurationBasis.SERVER_WALL_ELAPSED,
        operation_ref=opaque_reference("op", "private-workspace:private-operation"),
        attempt_ref=opaque_reference("att", "private-request:pi_do_not_export"),
    )


def event(metadata: JourneyEventContext | None = None) -> JourneyEventInput:
    return JourneyEventInput(
        kind=JourneyEventKind.TICKET_READ,
        outcome=JourneyEventOutcome.SUCCEEDED,
        occurred_at=NOW,
        recorded_at=NOW,
        duration_ms=10,
        cost_status=CostStatus.UNKNOWN,
        context=metadata,
    )


def seed(path: Path) -> SqliteTransactionStore:
    store = SqliteTransactionStore(path)
    store.create_owned_workspace("alice", "workspace", Workspace(notes="private story"), NOW)
    return store


def test_closed_context_roundtrip_and_explicit_unknowns() -> None:
    value = context()
    encoded = value.to_dict()
    assert JourneyEventContext.from_mapping(encoded) == value
    assert encoded["build_status"] == encoded["config_status"] == "unknown"
    assert encoded["causation_ref"] == {"status": "unknown", "id": None}
    assert encoded["funding"] == "unknown"
    assert encoded["fact_route"] == "not_applicable"
    raw = value.to_json()
    for private in (
        "private-workspace",
        "private-operation",
        "private-request",
        "pi_do_not_export",
    ):
        assert private not in raw
    assert opaque_reference("op", "same") != opaque_reference("att", "same")
    assert opaque_reference("op", "same") == opaque_reference("op", "same")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("capture_coverage", "complete"),
        ("operation", "arbitrary customer text"),
        ("reason", "some exception"),
        ("build_sha256", "credential-not-a-digest"),
        ("build_status", "known"),
        ("workspace_token", "private-workspace"),
        ("provider_id", "pi_do_not_export"),
    ],
)
def test_context_rejects_unknown_fields_and_unsupported_values(key: str, value: object) -> None:
    payload = context().to_dict()
    payload[key] = value
    with pytest.raises(
        ValueError, match=r"closed schema|unsupported|complete coverage|invalid|SHA-256|agree"
    ):
        JourneyEventContext.from_mapping(payload)


def test_reference_domains_and_duration_basis_cannot_be_invented() -> None:
    with pytest.raises(ValueError, match="opaque identity"):
        JourneyReference(ReferenceState.KNOWN, "pi_do_not_export")
    with pytest.raises(ValueError, match="no identity"):
        JourneyReference(ReferenceState.UNKNOWN, "evt_" + "a" * 32)
    with pytest.raises(ValueError, match="wrong domain"):
        replace(context(), operation_ref=JourneyReference(ReferenceState.KNOWN, "evt_" + "a" * 32))
    with pytest.raises(ValueError, match="duration and recorded basis"):
        event(replace(context(), duration_basis=DurationBasis.UNKNOWN))
    with pytest.raises(ValueError, match="duration and recorded basis"):
        replace(event(context()), duration_ms=None)
    assert JourneyReference(ReferenceState.NOT_APPLICABLE).to_dict() != JourneyReference().to_dict()


def test_context_commits_with_event_and_preserves_replay_owner_boundary(tmp_path: Path) -> None:
    with seed(tmp_path / "context.sqlite") as store:
        db = store._require_open()
        first = record_journey_event(
            db, workspace_token="workspace", occurrence_key="read", event=event(context())
        )
        assert first.context == context()
        assert JourneyEvent.from_mapping(first.to_dict()) == first
        assert (
            JourneyEventInput.from_mapping(
                {
                    "kind": "ticket_read",
                    "outcome": "succeeded",
                    "occurred_at": NOW,
                    "duration_ms": 10,
                    "cost_status": "unknown",
                    "context": context().to_dict(),
                }
            ).context
            == context()
        )
        assert read_journey_events(db, workspace_token="workspace", owner="alice").events == (
            first,
        )
        with pytest.raises(JourneyEventAccessError):
            read_journey_events(db, workspace_token="workspace", owner="bob")
        assert (
            record_journey_event(
                db, workspace_token="workspace", occurrence_key="read", event=event()
            )
            == first
        )
        with pytest.raises(JourneyEventConflict, match="journey_event_context_conflict"):
            record_journey_event(
                db,
                workspace_token="workspace",
                occurrence_key="read",
                event=event(replace(context(), funding=ContextFunding.PAID)),
            )
        assert db.execute("SELECT COUNT(*) FROM pb_journey_events").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM pb_journey_event_context").fetchone() == (1,)
        for action in (
            "UPDATE pb_journey_event_context SET context_json='{}'",
            "DELETE FROM pb_journey_event_context",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(action)
    with SqliteTransactionStore(tmp_path / "context.sqlite") as store:
        assert store.journey_events(workspace_token="workspace").events == (first,)


def test_legacy_replay_and_migration_do_not_invent_context(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    with seed(path) as store:
        db = store._require_open()
        original = record_journey_event(
            db, workspace_token="workspace", occurrence_key="old", event=event()
        )
        rows = db.execute("SELECT * FROM pb_journey_events").fetchall()
        assert original.context is None
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE pb_journey_event_context")
        db.execute("DELETE FROM pb_store_versions WHERE component=?", (COMPONENT,))
    with SqliteTransactionStore(path) as store:
        db = store._require_open()
        assert db.execute("SELECT * FROM pb_journey_events").fetchall() == rows
        replay = record_journey_event(
            db, workspace_token="workspace", occurrence_key="old", event=event(context())
        )
        assert replay == original
        assert replay.context is None
        assert db.execute("SELECT COUNT(*) FROM pb_journey_event_context").fetchone() == (0,)
        assert db.execute("SELECT * FROM pb_journey_events").fetchall() == rows


@pytest.mark.parametrize("boundary", ["context_insert", "commit"])
def test_context_and_event_rollback_together(tmp_path: Path, boundary: str) -> None:
    with seed(tmp_path / "rollback.sqlite") as store:
        hooked = _HookedConnection(store._require_open())
        if boundary == "context_insert":
            hooked.raise_on_statement = (
                "INSERT INTO pb_journey_event_context",
                RuntimeError("context fault"),
            )
        else:
            hooked.raise_before_commit = RuntimeError("context fault")
        with pytest.raises(RuntimeError, match="context fault"):
            record_journey_event(
                hooked, workspace_token="workspace", occurrence_key="new", event=event(context())
            )
        assert hooked.execute("SELECT COUNT(*) FROM pb_journey_events").fetchone() == (0,)
        assert hooked.execute("SELECT COUNT(*) FROM pb_journey_event_context").fetchone() == (0,)
        assert store.journey_capture(workspace_token="workspace") is not None


@pytest.mark.parametrize("raw", ['{"customer_story":"private story"}', "null", "[]"])
def test_malformed_stored_context_fails_closed_without_content(tmp_path: Path, raw: str) -> None:
    with seed(tmp_path / "malformed.sqlite") as store:
        db = store._require_open()
        original = record_journey_event(
            db, workspace_token="workspace", occurrence_key="old", event=event()
        )
        db.execute("INSERT INTO pb_journey_event_context VALUES (?, ?)", (original.event_id, raw))
        with pytest.raises(StorageCorruption, match="malformed journey event context") as caught:
            store.journey_events(workspace_token="workspace")
        assert "private story" not in str(caught.value)
        assert "customer_story" not in str(caught.value)


def test_causation_is_scoped_to_an_existing_earlier_fact(tmp_path: Path) -> None:
    from parkingbreaker.journey_events import prior_journey_event_reference_in_transaction

    with seed(tmp_path / "causation.sqlite") as store:
        db = store._require_open()
        store.create_owned_workspace("bob", "other", Workspace(notes=""), NOW)
        first = record_journey_event(
            db, workspace_token="workspace", occurrence_key="first", event=event(context())
        )
        with pytest.raises(StorageCorruption, match="active transaction"):
            prior_journey_event_reference_in_transaction(
                db, workspace_token="workspace", occurrence_key="first"
            )
        db.execute("BEGIN")
        try:
            known = prior_journey_event_reference_in_transaction(
                db, workspace_token="workspace", occurrence_key="first"
            )
            assert known == JourneyReference(ReferenceState.KNOWN, first.event_id)
            assert (
                prior_journey_event_reference_in_transaction(
                    db, workspace_token="other", occurrence_key="first"
                ).status
                is ReferenceState.UNKNOWN
            )
        finally:
            db.execute("ROLLBACK")
        foreign_context = replace(context(), causation_ref=known)
        with pytest.raises(ValueError, match="earlier recorded event in the same journey"):
            record_journey_event(
                db, workspace_token="other", occurrence_key="foreign", event=event(foreign_context)
            )
        successor = record_journey_event(
            db, workspace_token="workspace", occurrence_key="second", event=event(foreign_context)
        )
        assert successor.context is not None
        assert successor.context.causation_ref == known
        assert successor.seq > first.seq
        assert store.journey_events(workspace_token="other", owner="bob").events == ()


def test_context_cannot_contradict_operation_outcome_or_no_charge_route() -> None:
    with pytest.raises(ValueError, match="operation disagrees"):
        event(replace(context(), operation=ContextOperation.PACKET_APPROVAL))
    with pytest.raises(ValueError, match="reason disagrees"):
        event(replace(context(), reason=ContextReason.READING_REFUSED))
    with pytest.raises(ValueError, match="no-charge journey"):
        JourneyEventInput(
            kind=JourneyEventKind.HOLD_AUTHORIZED,
            outcome=JourneyEventOutcome.SUCCEEDED,
            occurred_at=NOW,
            context=JourneyEventContext(
                operation=ContextOperation.PAYMENT_AUTHORIZATION,
                reason=ContextReason.NOT_APPLICABLE,
                funding=ContextFunding.FREE_BETA,
            ),
        )
