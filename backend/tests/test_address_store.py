"""T30 persistence, isolation, migration and every-SQL-boundary durability proofs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from typing import cast

import pytest

from mail_samples import make_request
from parkingbreaker.address_policy import (
    AddressProvenance,
    ConsentEvent,
    DestinationDecision,
    Disclosure,
    EvidenceKind,
    EvidenceRef,
    address_fingerprint,
)
from parkingbreaker.address_store import (
    CustomerAddressRevision,
    SavedDestinationDecision,
    initialize_address_store,
)
from parkingbreaker.agencies import MailingAddress
from parkingbreaker.case_state import DraftReady, ResearchComplete
from parkingbreaker.intake_store import IntakeConflict
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone(timedelta(hours=-7)))
LATER = NOW + timedelta(minutes=1)
ADDRESS = MailingAddress(
    "  Appeals \u2013 café  ", "", " 1 Oak St ", None, "Springfield", "IL", "62701"
)
CHANGED = replace(ADDRESS, line2="", attention=None)
DISCLOSURE = Disclosure("disclosure-v1", "a" * 64)
TABLES = ("pb_address_revisions", "pb_address_consents", "pb_address_decisions")


def consent(*, case_id: str = "case-1", owner: str = "owner-1", at: datetime = NOW) -> ConsentEvent:
    return ConsentEvent(case_id, owner, address_fingerprint(ADDRESS), "a" * 64, 1, True, at)


def decision(*, customer: bool = False) -> DestinationDecision:
    if customer:
        return DestinationDecision(
            "case-1",
            "owner-1",
            ADDRESS,
            address_fingerprint(ADDRESS),
            AddressProvenance.CUSTOMER_DIRECTED_UNVERIFIED,
            "refund-v1",
            LATER,
            failed_verification_code="NO_OFFICIAL_DESTINATION",
            customer_address_revision=1,
            disclosure=DISCLOSURE,
            consent=consent(),
        )
    return DestinationDecision(
        "case-1",
        "owner-1",
        ADDRESS,
        address_fingerprint(ADDRESS),
        AddressProvenance.SYSTEM_VERIFIED,
        "refund-v1",
        LATER,
        verification_evidence=(
            EvidenceRef("quote-1", "b" * 64, EvidenceKind.OFFICIAL_SOURCE_QUOTE),
            EvidenceRef("resolution-1", "c" * 64, EvidenceKind.RESOLUTION_CHECK),
        ),
    )


def seed_intakes(store: SqliteTransactionStore) -> None:
    for case_id, owner in (("case-1", "owner-1"), ("case-2", "owner-2"), ("case-3", "owner-1")):
        token = f"workspace-{case_id}"
        store.save_workspace(token, Workspace(), NOW)
        store.set_workspace_owner(token, owner, NOW)
        store.record_intake(case_id, token, "{}", "{}", NOW)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteTransactionStore]:
    with SqliteTransactionStore(tmp_path / "addresses.sqlite") as result:
        seed_intakes(result)
        yield result


def seed_customer(store: SqliteTransactionStore) -> None:
    store.save_customer_address("case-1", "owner-1", ADDRESS, NOW, None)
    store.record_address_consent("case-1", "owner-1", consent())


def close_destination_choice(store: SqliteTransactionStore, barrier: str) -> None:
    case = store.start_case("workspace-case-1", "auth", NOW, NOW + timedelta(days=5))
    store.update_intake("case-1", 1, selected_tier="CERTIFIED", case_token=case.case_token)
    case = store.apply_letter_event(
        case.case_token, ResearchComplete(), case.revision, occurred_at=NOW
    )
    if barrier in {"state", "lock_and_state"}:
        store.apply_letter_event(case.case_token, DraftReady(), case.revision, occurred_at=NOW)
    if barrier in {"lock", "lock_and_state"}:
        # A persisted lock is sufficient even before any draft record is saved.
        store._require_open().execute(
            "INSERT INTO pb_address_locks VALUES (?, ?, ?)",
            ("case-1", "owner-1", NOW.isoformat()),
        )


@pytest.mark.parametrize("barrier", ["lock", "state", "lock_and_state"])
def test_system_verified_first_write_allowed_after_destination_closed(
    store: SqliteTransactionStore, barrier: str
) -> None:
    close_destination_choice(store, barrier)
    saved = store.save_destination_decision("case-1", "owner-1", decision(), None)
    assert saved == SavedDestinationDecision(decision(), 1)
    assert store.destination_decision_history("case-1", "owner-1") == (saved,)
    assert store.customer_address_revisions("case-1", "owner-1") == ()
    assert store.address_consent_events("case-1", "owner-1") == ()


@pytest.mark.parametrize("customer", [False, True])
def test_has_destination_lock_is_scoped_and_read_only(
    store: SqliteTransactionStore, customer: bool
) -> None:
    seed_customer(store)
    store.save_destination_decision("case-1", "owner-1", decision(customer=customer), None)
    close_destination_choice(store, "none")
    assert store.has_destination_lock("case-1", "owner-1") is False
    if customer:
        before = snapshot(store._require_open())
        with pytest.raises(IntakeConflict, match=r"^destination_decision_stale$"):
            store.lock_destination_for_draft("case-1", "owner-1", LATER, "a" * 64)
        assert snapshot(store._require_open()) == before
    else:
        store.lock_destination_for_draft("case-1", "owner-1", LATER, "a" * 64)
    before_reads = snapshot(store._require_open())
    assert store.has_destination_lock("case-1", "owner-1") is (not customer)
    assert store.has_destination_lock("case-1", "owner-1") is (not customer)
    assert store.has_destination_lock("case-3", "owner-1") is False
    with pytest.raises(IntakeConflict, match=r"^address_scope_mismatch$"):
        store.has_destination_lock("case-1", "owner-2")
    with pytest.raises(IntakeConflict, match=r"^address_scope_mismatch$"):
        store.has_destination_lock("case-2", "owner-1")
    rows = store._require_open().execute("SELECT COUNT(*) FROM pb_address_locks").fetchone()
    assert rows[0] == (0 if customer else 1)
    assert snapshot(store._require_open()) == before_reads


@pytest.mark.parametrize("customer", [False, True])
@pytest.mark.parametrize("barrier", ["open", "lock", "state", "lock_and_state"])
@pytest.mark.parametrize("expected", [None, 1])
def test_system_verified_replacement_only_explicit_and_before_lock(
    store: SqliteTransactionStore, customer: bool, barrier: str, expected: int | None
) -> None:
    seed_customer(store)
    saved = store.save_destination_decision("case-1", "owner-1", decision(customer=customer), None)
    if barrier != "open":
        close_destination_choice(store, barrier)
    if barrier == "open" and expected == 1:
        # Before the lock an explicit, revision-checked write may supersede (history kept).
        replaced = store.save_destination_decision("case-1", "owner-1", decision(), expected)
        assert replaced == SavedDestinationDecision(decision(), 2)
        assert store.destination_decision_history("case-1", "owner-1") == (saved, replaced)
        return
    before = snapshot(store._require_open())
    with pytest.raises(IntakeConflict, match=r"^address_revision_stale$"):
        store.save_destination_decision("case-1", "owner-1", decision(), expected)
    assert snapshot(store._require_open()) == before
    assert store.latest_destination_decision("case-1", "owner-1") == saved


CORRECTED = replace(ADDRESS, line1="2 Elm St")


def corrected_decision(address: MailingAddress = CORRECTED) -> DestinationDecision:
    return replace(
        decision(),
        address=address,
        fingerprint=address_fingerprint(address),
        decided_at=LATER + timedelta(minutes=1),
    )


def case_in(store: SqliteTransactionStore, state: str, attempt: int, cursor: bool = False) -> None:
    """Place the linked case in a lifecycle state after a locked first decision."""
    close_destination_choice(store, "lock")
    database = store._require_open()
    row = database.execute("SELECT case_token FROM pb_cases").fetchone()
    token = row[0]
    database.execute(
        "UPDATE pb_cases SET letter_state=?, attempt=? WHERE case_token=?", (state, attempt, token)
    )
    if cursor:
        store.prepare(token, 1, make_request(), NOW)
        store.prepare(token, 2, make_request(token="remail-token"), NOW)
        database.execute(
            "INSERT INTO pb_submission_cursors VALUES (?, 2, ?, 1, ?, 0, 1, NULL, 1, ?)",
            (token, NOW.isoformat(), NOW.isoformat(), '{"kind":"uncertain","reason":"x"}'),
        )


@pytest.mark.parametrize(("state", "attempt"), [("RETURNED", 1), ("SUBMITTING", 2)])
def test_remail_correction_may_supersede_locked_decision_before_remail_is_sent(
    store: SqliteTransactionStore, state: str, attempt: int
) -> None:
    saved = store.save_destination_decision("case-1", "owner-1", decision(), None)
    case_in(store, state, attempt)
    replaced = store.save_destination_decision("case-1", "owner-1", corrected_decision(), 1)
    assert replaced == SavedDestinationDecision(corrected_decision(), 2)
    assert store.destination_decision_history("case-1", "owner-1") == (saved, replaced)
    # The correction is itself immutable history: it cannot be superseded by the old address.
    with pytest.raises(IntakeConflict, match=r"^address_revision_stale$"):
        store.save_destination_decision("case-1", "owner-1", decision(), 2)


@pytest.mark.parametrize(
    ("state", "attempt", "cursor"),
    [
        ("DRAFTING", 1, False),
        ("AWAITING_ADOPTION", 1, False),
        ("FROZEN", 1, False),
        ("SUBMITTING", 1, False),
        ("IN_TRANSIT", 1, False),
        ("DELIVERED", 1, False),
        ("CLOSED_RETURNED", 1, False),
        ("SUBMITTING", 2, True),
        ("IN_TRANSIT", 2, False),
    ],
)
def test_locked_decision_cannot_be_superseded_outside_the_remail_correction_window(
    store: SqliteTransactionStore, state: str, attempt: int, cursor: bool
) -> None:
    saved = store.save_destination_decision("case-1", "owner-1", decision(), None)
    case_in(store, state, attempt, cursor)
    before = snapshot(store._require_open())
    with pytest.raises(IntakeConflict, match=r"^address_revision_stale$"):
        store.save_destination_decision("case-1", "owner-1", corrected_decision(), 1)
    assert snapshot(store._require_open()) == before
    assert store.latest_destination_decision("case-1", "owner-1") == saved


def test_remail_correction_must_differ_and_stays_system_verified_only(
    store: SqliteTransactionStore,
) -> None:
    seed_customer(store)
    store.save_destination_decision("case-1", "owner-1", decision(), None)
    case_in(store, "RETURNED", 1)
    # The returned address itself is never a correction.
    with pytest.raises(IntakeConflict, match=r"^address_revision_stale$"):
        store.save_destination_decision("case-1", "owner-1", decision(), 1)
    # Customer-directed writes stay locked even in the re-mail window.
    with pytest.raises(IntakeConflict, match=r"^destination_locked_after_draft$"):
        store.save_destination_decision("case-1", "owner-1", decision(customer=True), 1)
    with pytest.raises(IntakeConflict, match=r"^destination_locked_after_draft$"):
        store.save_customer_address("case-1", "owner-1", CHANGED, LATER, 1)


@pytest.mark.parametrize("barrier", ["lock", "state", "lock_and_state"])
@pytest.mark.parametrize("write", ["address", "consent", "decision"])
def test_customer_writes_refused_after_destination_closed(
    store: SqliteTransactionStore, barrier: str, write: str
) -> None:
    seed_customer(store)
    close_destination_choice(store, barrier)
    before = snapshot(store._require_open())

    def write_customer_input() -> None:
        if write == "address":
            store.save_customer_address("case-1", "owner-1", CHANGED, LATER, 1)
        elif write == "consent":
            store.record_address_consent("case-1", "owner-1", consent(at=LATER))
        else:
            store.save_destination_decision("case-1", "owner-1", decision(customer=True), None)

    with pytest.raises(IntakeConflict, match=r"^destination_locked_after_draft$"):
        write_customer_input()
    assert snapshot(store._require_open()) == before


@pytest.mark.parametrize("customer", [False, True])
def test_round_trip_all_fields_and_restart(tmp_path: Path, customer: bool) -> None:
    path = tmp_path / "addresses.sqlite"
    with SqliteTransactionStore(path) as store:
        seed_intakes(store)
        seed_customer(store)
        saved = store.save_destination_decision(
            "case-1", "owner-1", decision(customer=customer), None
        )
    with SqliteTransactionStore(path) as store:
        revisions = store.customer_address_revisions("case-1", "owner-1")
        assert revisions == (CustomerAddressRevision("case-1", "owner-1", ADDRESS, 1, NOW),)
        assert revisions[0].created_at.isoformat() == NOW.isoformat()
        assert revisions[0].address.attention == ""
        assert revisions[0].address.line2 is None
        assert address_fingerprint(revisions[0].address) == address_fingerprint(ADDRESS)
        assert store.address_consent_events("case-1", "owner-1") == (consent(),)
        assert saved == SavedDestinationDecision(decision(customer=customer), 1)
        assert store.latest_destination_decision("case-1", "owner-1") == saved
        assert store.destination_decision_history("case-1", "owner-1") == (saved,)
        assert store.address_consent_events("case-2", "owner-2") == ()
        assert store.latest_destination_decision("case-2", "owner-2") is None


def test_history_retained_and_only_explicit_decision_supersedes(
    store: SqliteTransactionStore,
) -> None:
    seed_customer(store)
    first = store.save_destination_decision("case-1", "owner-1", decision(customer=True), None)
    second_address = store.save_customer_address("case-1", "owner-1", CHANGED, LATER, 1)
    assert second_address.revision == 2
    assert store.latest_destination_decision("case-1", "owner-1") == first
    assert store.address_consent_events("case-1", "owner-1") == (consent(),)
    with pytest.raises(IntakeConflict, match="address_consent_stale"):
        store.record_address_consent("case-1", "owner-1", consent(at=LATER))
    with pytest.raises(IntakeConflict, match="address_consent_stale"):
        store.save_destination_decision("case-1", "owner-1", decision(customer=True), 1)
    current_consent = replace(
        consent(at=LATER), address_revision=2, address_fingerprint=address_fingerprint(CHANGED)
    )
    store.record_address_consent("case-1", "owner-1", current_consent)
    current = replace(
        decision(customer=True),
        address=CHANGED,
        fingerprint=address_fingerprint(CHANGED),
        customer_address_revision=2,
        consent=current_consent,
    )
    second = store.save_destination_decision("case-1", "owner-1", current, 1)
    third = store.save_destination_decision("case-1", "owner-1", decision(), 2)
    assert store.destination_decision_history("case-1", "owner-1") == (first, second, third)
    assert store.latest_destination_decision("case-1", "owner-1") == third
    assert [record.address for record in store.customer_address_revisions("case-1", "owner-1")] == [
        ADDRESS,
        CHANGED,
    ]
    assert store.address_consent_events("case-1", "owner-1") == (consent(), current_consent)


@pytest.mark.parametrize("accepted", [False, 0, 1, None, "true"])
def test_no_inferred_or_default_consent(store: SqliteTransactionStore, accepted: object) -> None:
    store.save_customer_address("case-1", "owner-1", ADDRESS, NOW, None)
    forged = consent()
    object.__setattr__(forged, "accepted", accepted)
    with pytest.raises(ValueError, match="explicit acceptance"):
        store.record_address_consent("case-1", "owner-1", forged)
    assert store.address_consent_events("case-1", "owner-1") == ()


def test_decision_requires_preexisting_explicit_consent(store: SqliteTransactionStore) -> None:
    store.save_customer_address("case-1", "owner-1", ADDRESS, NOW, None)
    with pytest.raises(IntakeConflict, match="address_consent_not_recorded"):
        store.save_destination_decision("case-1", "owner-1", decision(customer=True), None)
    assert store.address_consent_events("case-1", "owner-1") == ()
    assert store.latest_destination_decision("case-1", "owner-1") is None


@pytest.mark.parametrize("field", ["address_fingerprint", "address_revision", "accepted_at"])
def test_consent_must_match_saved_address(store: SqliteTransactionStore, field: str) -> None:
    store.save_customer_address("case-1", "owner-1", ADDRESS, NOW, None)
    event = consent()
    changes: dict[str, object] = {
        "address_fingerprint": address_fingerprint(CHANGED),
        "address_revision": 2,
        "accepted_at": NOW - timedelta(seconds=1),
    }
    object.__setattr__(event, field, changes[field])
    with pytest.raises(IntakeConflict, match="address_consent_stale"):
        store.record_address_consent("case-1", "owner-1", event)
    assert store.address_consent_events("case-1", "owner-1") == ()


@pytest.mark.parametrize(
    ("case_id", "owner"),
    [("case-1", "owner-2"), ("case-2", "owner-1"), ("missing", "owner-1")],
)
def test_all_apis_refuse_wrong_scope(
    store: SqliteTransactionStore, case_id: str, owner: str
) -> None:
    seed_customer(store)
    store.save_destination_decision("case-1", "owner-1", decision(customer=True), None)
    with pytest.raises(IntakeConflict, match="address_scope_mismatch"):
        store.customer_address_revisions(case_id, owner)
    with pytest.raises(IntakeConflict, match="address_scope_mismatch"):
        store.address_consent_events(case_id, owner)
    with pytest.raises(IntakeConflict, match="address_scope_mismatch"):
        store.latest_destination_decision(case_id, owner)
    with pytest.raises(IntakeConflict, match="address_scope_mismatch"):
        store.destination_decision_history(case_id, owner)
    with pytest.raises(IntakeConflict, match="address_scope_mismatch"):
        store.save_customer_address(case_id, owner, CHANGED, LATER, 1)
    with pytest.raises(IntakeConflict, match="address_scope_mismatch"):
        store.record_address_consent(case_id, owner, consent())
    with pytest.raises(IntakeConflict, match="address_scope_mismatch"):
        store.save_destination_decision(case_id, owner, decision(), None)


@pytest.mark.parametrize(("case_id", "owner"), [("case-2", "owner-2"), ("case-3", "owner-1")])
def test_no_replay_into_another_valid_case(
    store: SqliteTransactionStore, case_id: str, owner: str
) -> None:
    seed_customer(store)
    store.save_customer_address(case_id, owner, ADDRESS, NOW, None)
    with pytest.raises(IntakeConflict, match="address_scope_mismatch"):
        store.record_address_consent(case_id, owner, consent())
    with pytest.raises(IntakeConflict, match="address_scope_mismatch"):
        store.save_destination_decision(case_id, owner, decision(customer=True), None)
    assert store.address_consent_events(case_id, owner) == ()
    assert store.latest_destination_decision(case_id, owner) is None


@pytest.mark.parametrize("expected", [None, 2, 12])
def test_revision_conflicts(store: SqliteTransactionStore, expected: int | None) -> None:
    seed_customer(store)
    first = store.save_destination_decision("case-1", "owner-1", decision(), None)
    with pytest.raises(IntakeConflict, match="address_revision_stale"):
        store.save_customer_address("case-1", "owner-1", CHANGED, LATER, expected)
    with pytest.raises(IntakeConflict, match="address_revision_stale"):
        store.save_destination_decision("case-1", "owner-1", decision(customer=True), expected)
    assert len(store.customer_address_revisions("case-1", "owner-1")) == 1
    assert store.destination_decision_history("case-1", "owner-1") == (first,)


@pytest.mark.parametrize("kind", ["address", "decision"])
@pytest.mark.parametrize("expected", [0, -1, True, "1"])
def test_invalid_revision_types_rejected(
    store: SqliteTransactionStore, kind: str, expected: object
) -> None:
    if kind == "address":
        with pytest.raises(ValueError, match="positive revision"):
            store.save_customer_address("case-1", "owner-1", ADDRESS, NOW, cast(int, expected))
    else:
        with pytest.raises(ValueError, match="positive revision"):
            store.save_destination_decision("case-1", "owner-1", decision(), cast(int, expected))


@pytest.mark.parametrize("kind", ["address", "consent", "decision", "nested_consent"])
def test_naive_timestamps_rejected_on_write(store: SqliteTransactionStore, kind: str) -> None:
    naive = NOW.replace(tzinfo=None)
    seed_customer(store)

    def write_naive() -> None:
        if kind == "address":
            store.save_customer_address("case-1", "owner-1", CHANGED, naive, 1)
        elif kind == "consent":
            event = consent()
            object.__setattr__(event, "accepted_at", naive)
            store.record_address_consent("case-1", "owner-1", event)
        else:
            record = decision(customer=kind == "nested_consent")
            if record.consent is not None:
                object.__setattr__(record.consent, "accepted_at", naive)
            else:
                object.__setattr__(record, "decided_at", naive)
            store.save_destination_decision("case-1", "owner-1", record, None)

    with pytest.raises(ValueError, match="timezone-aware"):
        write_naive()
    assert len(store.customer_address_revisions("case-1", "owner-1")) == 1
    assert store.address_consent_events("case-1", "owner-1") == (consent(),)
    assert store.latest_destination_decision("case-1", "owner-1") is None


def assert_read_corrupt(store: SqliteTransactionStore, table: str) -> None:
    readers = {
        TABLES[0]: store.customer_address_revisions,
        TABLES[1]: store.address_consent_events,
        TABLES[2]: store.latest_destination_decision,
    }
    with pytest.raises(StorageCorruption):
        readers[table]("case-1", "owner-1")


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("payload", ["{", "null", "[]", "{}", '{"a":1,"a":2}', "NaN"])
def test_malformed_json_raises_storage_corruption(
    store: SqliteTransactionStore, table: str, payload: str
) -> None:
    seed_customer(store)
    store.save_destination_decision("case-1", "owner-1", decision(customer=True), None)
    store._require_open().execute(f"UPDATE {table} SET payload_json=?", (payload,))
    assert_read_corrupt(store, table)


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("column", ["owner", "revision"])
def test_malformed_scope_or_sequence_raises_storage_corruption(
    store: SqliteTransactionStore, table: str, column: str
) -> None:
    seed_customer(store)
    store.save_destination_decision("case-1", "owner-1", decision(), None)
    value = "wrong-owner" if column == "owner" else 3
    store._require_open().execute(f"UPDATE {table} SET {column}=?", (value,))
    assert_read_corrupt(store, table)


@pytest.mark.parametrize(
    ("table", "field", "value"),
    [
        (TABLES[0], "created_at", "2026-10-20T12:00:00"),
        (TABLES[0], "created_at", "not-a-time"),
        (TABLES[0], "address", {"recipient": "incomplete-structure"}),
        (TABLES[1], "accepted", False),
        (TABLES[1], "accepted", 1),
        (TABLES[1], "accepted_at", "2026-10-20T12:00:00"),
        (TABLES[1], "address_revision", True),
        (TABLES[1], "address_revision", 2),
        (TABLES[1], "address_fingerprint", "f" * 64),
        (TABLES[1], "case_id", "case-3"),
        (TABLES[1], "owner", "owner-2"),
        (TABLES[2], "decided_at", "2026-10-20T12:00:00"),
        (TABLES[2], "case_id", "case-3"),
        (TABLES[2], "owner", "owner-2"),
        (TABLES[2], "provenance", "CONFIRMED"),
        (TABLES[2], "fingerprint", "f" * 64),
        (TABLES[2], "refund_policy_version", ""),
        (TABLES[2], "disclosure", None),
        (TABLES[2], "consent", None),
        (TABLES[2], "verification_evidence", ["not-an-evidence-ref"]),
    ],
)
def test_malformed_record_fields_raise_storage_corruption(
    store: SqliteTransactionStore, table: str, field: str, value: object
) -> None:
    seed_customer(store)
    store.save_destination_decision("case-1", "owner-1", decision(customer=True), None)
    database = store._require_open()
    payload = json.loads(database.execute(f"SELECT payload_json FROM {table}").fetchone()[0])
    payload[field] = value
    database.execute(
        f"UPDATE {table} SET payload_json=?",
        (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False),),
    )
    assert_read_corrupt(store, table)


def test_old_history_is_validated_too(store: SqliteTransactionStore) -> None:
    store.save_destination_decision("case-1", "owner-1", decision(), None)
    store.save_destination_decision("case-1", "owner-1", decision(), 1)
    store._require_open().execute(
        "UPDATE pb_address_decisions SET payload_json='{}' WHERE revision=1"
    )
    with pytest.raises(StorageCorruption):
        store.latest_destination_decision("case-1", "owner-1")
    with pytest.raises(StorageCorruption):
        store.save_destination_decision("case-1", "owner-1", decision(), 2)


def previous_schema(path: Path) -> None:
    with SqliteTransactionStore(path) as store:
        seed_intakes(store)
        started = store.start_case("workspace-case-1", "legacy-auth", NOW, NOW + timedelta(days=5))
        store.update_intake("case-1", 1, selected_tier="CERTIFIED", case_token=started.case_token)
        database = store._require_open()
        for table in (*TABLES, "pb_address_locks"):
            database.execute(f"DROP TABLE {table}")
        database.execute("DELETE FROM pb_store_versions WHERE component='address'")


def test_forward_migration_has_no_backfill_or_legacy_exclusion(tmp_path: Path) -> None:
    path = tmp_path / "previous.sqlite"
    previous_schema(path)
    with sqlite3.connect(path) as database:
        before = database.execute("SELECT * FROM pb_intake_records ORDER BY intake_id").fetchall()
        cases_before = database.execute("SELECT * FROM pb_cases").fetchall()
        assert len(cases_before) == 1
        versions = database.execute("SELECT * FROM pb_store_versions ORDER BY component").fetchall()
        assert ("intake", 2) in versions
        assert not any(row[0] == "address" for row in versions)
    for _ in range(2):  # Migration followed by ordinary reopen.
        with SqliteTransactionStore(path) as store:
            assert store.customer_address_revisions("case-1", "owner-1") == ()
            assert store.address_consent_events("case-1", "owner-1") == ()
            assert store.latest_destination_decision("case-1", "owner-1") is None
            database = store._require_open()
            assert (
                database.execute("SELECT * FROM pb_intake_records ORDER BY intake_id").fetchall()
                == before
            )
            assert database.execute("SELECT * FROM pb_cases").fetchall() == cases_before
            assert (
                database.execute(
                    "SELECT * FROM pb_store_versions WHERE component!='address' ORDER BY component"
                ).fetchall()
                == versions
            )
            assert database.execute(
                "SELECT version FROM pb_store_versions WHERE component='address'"
            ).fetchone() == (2,)
            for table in (*TABLES, "pb_address_locks"):
                assert database.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


@pytest.mark.parametrize("damage", ["version", "unversioned", "missing_table", "altered_table"])
def test_unknown_or_malformed_schema_refused(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "damaged.sqlite"
    with SqliteTransactionStore(path) as store:
        database = store._require_open()
        if damage == "version":
            database.execute("UPDATE pb_store_versions SET version=99 WHERE component='address'")
        elif damage == "unversioned":
            database.execute("DELETE FROM pb_store_versions WHERE component='address'")
        elif damage == "missing_table":
            database.execute("DROP TABLE pb_address_consents")
        else:
            database.execute("ALTER TABLE pb_address_consents ADD COLUMN surprise TEXT")
    with pytest.raises(StorageCorruption):
        SqliteTransactionStore(path)


class FaultConnection:
    """One-shot fault before/after a numbered SQL call, including BEGIN and COMMIT."""

    def __init__(
        self, target: sqlite3.Connection, boundary: int | None = None, *, after: bool = False
    ) -> None:
        self.target = target
        self.boundary = boundary
        self.after = after
        self.statements: list[str] = []

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        index = len(self.statements)
        self.statements.append(sql)
        fail = index == self.boundary
        if fail:
            self.boundary = None
        if fail and not self.after:
            raise RuntimeError("injected SQL fault")
        result = self.target.execute(sql, parameters)
        if fail:
            raise RuntimeError("injected SQL fault")
        return result

    @property
    def in_transaction(self) -> bool:
        return self.target.in_transaction

    def close(self) -> None:
        self.target.close()


Snapshot = tuple[tuple[tuple[object, ...], ...], ...]


def snapshot(database: sqlite3.Connection) -> Snapshot:
    return tuple(
        tuple(
            tuple(row)
            for row in database.execute(f"SELECT * FROM {table} ORDER BY case_id, owner, revision")
        )
        for table in TABLES
    )


ACTIONS = (
    "address_create",
    "address_update",
    "consent_create",
    "consent_append",
    "system_create",
    "system_update",
    "customer_create",
    "customer_update",
)


def prepare_action(store: SqliteTransactionStore, action: str) -> None:
    seed_intakes(store)
    if action.startswith(("consent", "customer")):
        store.save_customer_address("case-1", "owner-1", ADDRESS, NOW, None)
    if action.startswith("customer") or action == "consent_append":
        store.record_address_consent("case-1", "owner-1", consent())
    if action == "address_update":
        store.save_customer_address("case-1", "owner-1", ADDRESS, NOW, None)
    if action in {"system_update", "customer_update"}:
        store.save_destination_decision("case-1", "owner-1", decision(), None)


def perform_action(store: SqliteTransactionStore, action: str) -> None:
    if action.startswith("address"):
        store.save_customer_address(
            "case-1", "owner-1", CHANGED, LATER, 1 if action.endswith("update") else None
        )
    elif action.startswith("consent"):
        store.record_address_consent("case-1", "owner-1", consent(at=LATER))
    else:
        store.save_destination_decision(
            "case-1",
            "owner-1",
            decision(customer=action.startswith("customer")),
            1 if action.endswith("update") else None,
        )


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("after", [False, True])
def test_write_atomic_at_every_sql_boundary(tmp_path: Path, action: str, after: bool) -> None:
    with SqliteTransactionStore(tmp_path / "successful.sqlite") as successful:
        prepare_action(successful, action)
        old = snapshot(successful._require_open())
        hook = FaultConnection(successful._require_open())
        successful._database = hook  # type: ignore[assignment]
        perform_action(successful, action)
        statements = tuple(hook.statements)
        new = snapshot(successful._require_open())
    assert old != new
    assert statements[0] == "BEGIN IMMEDIATE"
    assert statements[-1] == "COMMIT"
    assert sum(sql.startswith("INSERT INTO pb_address_") for sql in statements) == 1
    for boundary, sql in enumerate(statements):
        path = tmp_path / f"fault-{boundary}.sqlite"
        with SqliteTransactionStore(path) as failing:
            prepare_action(failing, action)
            hook = FaultConnection(failing._require_open(), boundary, after=after)
            failing._database = hook  # type: ignore[assignment]
            with pytest.raises(RuntimeError, match="injected SQL fault"):
                perform_action(failing, action)
        with SqliteTransactionStore(path) as reopened:
            expected = new if after and sql == "COMMIT" else old
            assert snapshot(reopened._require_open()) == expected, (action, boundary, sql, after)
            # Exercise strict decoders after recovery as well as comparing raw bytes.
            reopened.customer_address_revisions("case-1", "owner-1")
            reopened.address_consent_events("case-1", "owner-1")
            reopened.destination_decision_history("case-1", "owner-1")


@pytest.mark.parametrize("after", [False, True])
def test_migration_atomic_at_every_sql_boundary(tmp_path: Path, after: bool) -> None:
    probe = tmp_path / "probe.sqlite"
    previous_schema(probe)
    with sqlite3.connect(probe, isolation_level=None) as database:
        hook = FaultConnection(database)
        initialize_address_store(cast(sqlite3.Connection, hook))
        statements = tuple(hook.statements)
    for boundary, sql in enumerate(statements):
        path = tmp_path / f"migration-{boundary}.sqlite"
        previous_schema(path)
        with sqlite3.connect(path, isolation_level=None) as database:
            hook = FaultConnection(database, boundary, after=after)
            with pytest.raises(RuntimeError, match="injected SQL fault"):
                initialize_address_store(cast(sqlite3.Connection, hook))
            committed = after and sql == "COMMIT"
            names = database.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'pb_address_%'"
            ).fetchall()
            assert len(names) == (4 if committed else 0)
            assert database.execute(
                "SELECT version FROM pb_store_versions WHERE component='address'"
            ).fetchone() == ((2,) if committed else None)
        with SqliteTransactionStore(path) as reopened:
            assert reopened.latest_destination_decision("case-1", "owner-1") is None


@pytest.mark.parametrize("kind", ["address", "decision"])
def test_two_connections_racing_same_revision_only_one_wins(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "race.sqlite"
    with SqliteTransactionStore(path) as store:
        seed_intakes(store)
        if kind == "address":
            store.save_customer_address("case-1", "owner-1", ADDRESS, NOW, None)
        else:
            store.save_destination_decision("case-1", "owner-1", decision(), None)
    barrier = Barrier(2)

    def write(index: int) -> bool:
        with SqliteTransactionStore(path) as store:
            barrier.wait(timeout=5)
            try:
                if kind == "address":
                    store.save_customer_address(
                        "case-1", "owner-1", replace(ADDRESS, line1=f"{index} Oak St"), LATER, 1
                    )
                else:
                    store.save_destination_decision(
                        "case-1",
                        "owner-1",
                        replace(decision(), refund_policy_version=f"v{index}"),
                        1,
                    )
                return True
            except IntakeConflict:
                return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(write, (1, 2))) == [False, True]
    with SqliteTransactionStore(path) as store:
        if kind == "address":
            assert [r.revision for r in store.customer_address_revisions("case-1", "owner-1")] == [
                1,
                2,
            ]
        else:
            assert [
                r.revision for r in store.destination_decision_history("case-1", "owner-1")
            ] == [
                1,
                2,
            ]
