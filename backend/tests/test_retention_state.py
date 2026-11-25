"""T36 durable explicit retention policy, closure and hold state.

These tests use a synthetic database with only the existing structural tables the component
needs. They cover independent connections and reopen persistence, exact command replay and
conflict refusal, racing writers, stale releases against a newer hold, immutable hold identity
scopes, omitted expected generation/version authority, unknown cases, malformed
schema/version/rows, atomic rollback, and that initialization changes no existing table or row.
No customer content is used.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from parkingbreaker import retention_state as rs
from parkingbreaker.retention_policy import (
    ClosureKind,
    DataScope,
    PolicyAdoption,
    PreservationHold,
    RetentionRefused,
    UnresolvedBlockers,
    evaluate_service_retention,
)

CASE = "case-1"
OTHER_CASE = "case-2"
AT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
OLD = datetime(2020, 1, 1, 12, 0, tzinfo=UTC)
EVALUATED = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
UTC_TZ = ZoneInfo("UTC")
NEW_YORK = ZoneInfo("America/New_York")


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def make_database(path: Path) -> sqlite3.Connection:
    connection = connect(path)
    connection.execute(
        "CREATE TABLE pb_store_versions (component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE pb_cases (case_token TEXT PRIMARY KEY, letter_state TEXT NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO pb_cases VALUES (?, ?)",
        [(CASE, "DRAFTING"), (OTHER_CASE, "CLOSED")],
    )
    connection.execute("CREATE TABLE pb_existing (value TEXT NOT NULL)")
    connection.executemany("INSERT INTO pb_existing VALUES (?)", [("alpha",), ("beta",)])
    connection.execute("INSERT INTO pb_store_versions VALUES ('transactions', 5)")
    return connection


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "parkingbreaker.sqlite"
    connection = make_database(path)
    try:
        rs.initialize_retention_state(connection)
    finally:
        connection.close()
    return path


@pytest.fixture
def database(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


def existing_object_rows(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT GLOB 'pb_retention_*' "
            "AND name NOT GLOB 'sqlite_autoindex_pb_retention_*' "
            "ORDER BY type, name"
        ).fetchall()
    )


def rows(connection: sqlite3.Connection, table: str) -> tuple[tuple[object, ...], ...]:
    return tuple(connection.execute(f'SELECT * FROM "{table}"').fetchall())


def row_count(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
    assert row is not None
    return int(row[0])


def record_proposed(
    connection: sqlite3.Connection,
    command_id: str = "policy-1",
    *,
    expected_version: int | None = None,
) -> rs.PolicyCommandResult:
    return rs.record_policy(
        connection,
        command_id=command_id,
        adoption=PolicyAdoption.PROPOSED,
        timezone=UTC_TZ,
        at=AT,
        expected_version=expected_version,
    )


def record_adopted(
    connection: sqlite3.Connection,
    command_id: str = "policy-adopted",
    *,
    expected_version: int | None = None,
) -> rs.PolicyCommandResult:
    return rs.record_policy(
        connection,
        command_id=command_id,
        adoption=PolicyAdoption.ADOPTED,
        timezone=UTC_TZ,
        at=AT,
        adoption_evidence_ref="evidence/2026/approval",
        expected_version=expected_version,
    )


def record_closed(
    connection: sqlite3.Connection,
    command_id: str = "closure-1",
    *,
    expected_generation: int | None = None,
    kind: ClosureKind = ClosureKind.CLOSED,
    anchor_at: datetime = AT,
) -> rs.ClosureCommandResult:
    return rs.record_closure(
        connection,
        command_id=command_id,
        case_token=CASE,
        kind=kind,
        anchor_at=anchor_at,
        source_ref="anchor/source-1",
        at=AT,
        expected_generation=expected_generation,
    )


def record_service_hold(
    connection: sqlite3.Connection,
    command_id: str = "hold-1",
    *,
    hold_id: str = "hold-a",
    scope: DataScope = DataScope.SERVICE_DATA,
    expected_generation: int | None = None,
    at: datetime = AT,
) -> rs.HoldCommandResult:
    return rs.record_hold(
        connection,
        command_id=command_id,
        case_token=CASE,
        hold_id=hold_id,
        scope=scope,
        source_ref="hold/source-1",
        at=at,
        expected_generation=expected_generation,
    )


def release(
    connection: sqlite3.Connection,
    command_id: str,
    generation: int,
    *,
    hold_id: str = "hold-a",
) -> rs.HoldCommandResult:
    return rs.release_hold(
        connection,
        command_id=command_id,
        case_token=CASE,
        hold_id=hold_id,
        at=AT,
        expected_generation=generation,
    )


def test_initialization_is_idempotent_and_changes_no_existing_table_or_row(
    tmp_path: Path,
) -> None:
    path = tmp_path / "parkingbreaker.sqlite"
    connection = make_database(path)
    try:
        before_schema = existing_object_rows(connection)
        before_cases = rows(connection, "pb_cases")
        before_existing = rows(connection, "pb_existing")
        before_versions = rows(connection, "pb_store_versions")

        rs.initialize_retention_state(connection)
        rs.initialize_retention_state(connection)

        assert existing_object_rows(connection) == before_schema
        assert rows(connection, "pb_cases") == before_cases
        assert rows(connection, "pb_existing") == before_existing
        versions = rows(connection, "pb_store_versions")
        assert ("transactions", 5) in versions
        assert ("retention_state", 1) in versions
        assert len(versions) == len(before_versions) + 1
        # Nothing is adopted, closed or held by initialization.
        assert row_count(connection, "pb_retention_policies") == 0
        assert row_count(connection, "pb_retention_case_closures") == 0
        assert row_count(connection, "pb_retention_holds") == 0
        assert rs.policy_snapshot(connection) is None
    finally:
        connection.close()


def test_policy_versions_are_immutable_and_exact_replay_returns_the_saved_outcome(
    db_path: Path,
) -> None:
    first_connection = connect(db_path)
    try:
        first = record_proposed(first_connection)
        assert first.outcome is rs.CommandOutcome.APPLIED
        assert first.version == 1
        assert first.revision is not None
        assert first.revision.adoption is PolicyAdoption.PROPOSED
        assert first.revision.adoption_evidence_ref is None

        adopted = record_adopted(first_connection, expected_version=1)
        assert adopted.outcome is rs.CommandOutcome.APPLIED
        assert adopted.version == 2
        assert adopted.revision is not None
        assert adopted.revision.adoption is PolicyAdoption.ADOPTED
        assert adopted.revision.adoption_evidence_ref == "evidence/2026/approval"

        with pytest.raises(sqlite3.IntegrityError):
            first_connection.execute(
                "UPDATE pb_retention_policies SET adoption='adopted' WHERE version=1"
            )
        with pytest.raises(sqlite3.IntegrityError):
            first_connection.execute("DELETE FROM pb_retention_policies")
        with pytest.raises(sqlite3.IntegrityError):
            first_connection.execute("UPDATE pb_retention_policy_commands SET outcome='stale'")
    finally:
        first_connection.close()

    reopened = connect(db_path)
    try:
        revisions = rs.policy_revisions(reopened)
        assert [revision.version for revision in revisions] == [1, 2]
        snapshot = rs.policy_snapshot(reopened)
        assert snapshot is not None
        assert snapshot.version == 2
        assert snapshot.adoption is PolicyAdoption.ADOPTED

        replay = record_adopted(reopened, expected_version=1)
        assert replay.outcome is rs.CommandOutcome.APPLIED
        assert replay.version == 2
        assert replay.revision is not None
        assert replay.revision.adoption_evidence_ref == "evidence/2026/approval"
        assert len(rs.policy_revisions(reopened)) == 2

        with pytest.raises(rs.CommandIdReused) as error:
            rs.record_policy(
                reopened,
                command_id="policy-adopted",
                adoption=PolicyAdoption.PROPOSED,
                timezone=UTC_TZ,
                at=AT,
                expected_version=1,
            )
        assert error.value.reason is rs.RetentionStateRefusal.COMMAND_ID_REUSED
        assert len(rs.policy_revisions(reopened)) == 2
    finally:
        reopened.close()


def test_policy_stale_expected_version_and_evidence_rules_are_explicit(
    database: sqlite3.Connection,
) -> None:
    record_proposed(database)
    stale = rs.record_policy(
        database,
        command_id="policy-stale",
        adoption=PolicyAdoption.PROPOSED,
        timezone=NEW_YORK,
        at=AT,
        expected_version=5,
    )
    assert stale.outcome is rs.CommandOutcome.STALE
    assert stale.version == 1
    assert stale.revision is None
    assert len(rs.policy_revisions(database)) == 1
    snapshot = rs.policy_snapshot(database)
    assert snapshot is not None
    assert snapshot.version == 1
    assert snapshot.adoption is PolicyAdoption.PROPOSED

    replay = rs.record_policy(
        database,
        command_id="policy-stale",
        adoption=PolicyAdoption.PROPOSED,
        timezone=NEW_YORK,
        at=AT,
        expected_version=5,
    )
    assert replay == stale

    with pytest.raises(rs.RetentionStateInputError) as error:
        rs.record_policy(
            database,
            command_id="policy-missing-evidence",
            adoption=PolicyAdoption.ADOPTED,
            timezone=UTC_TZ,
            at=AT,
        )
    assert error.value.reason is rs.RetentionStateRefusal.INVALID_EVIDENCE_REF

    with pytest.raises(rs.RetentionStateInputError) as error:
        rs.record_policy(
            database,
            command_id="policy-extra-evidence",
            adoption=PolicyAdoption.PROPOSED,
            timezone=UTC_TZ,
            at=AT,
            adoption_evidence_ref="evidence/1",
        )
    assert error.value.reason is rs.RetentionStateRefusal.INVALID_EVIDENCE_REF


def test_omitted_policy_version_is_stale_after_the_first_policy(
    db_path: Path,
) -> None:
    connection = connect(db_path)
    try:
        first = record_proposed(connection)
        assert first.outcome is rs.CommandOutcome.APPLIED
        assert first.version == 1
        before = rows(connection, "pb_retention_policies")

        # The very first policy may omit its expected version, but never a later one.
        implicit = rs.record_policy(
            connection,
            command_id="policy-implicit",
            adoption=PolicyAdoption.PROPOSED,
            timezone=NEW_YORK,
            at=AT,
        )
        assert implicit.outcome is rs.CommandOutcome.STALE
        assert implicit.version == 1
        assert implicit.revision is None
        assert rows(connection, "pb_retention_policies") == before
        assert [revision.version for revision in rs.policy_revisions(connection)] == [1]
        # The stale command was saved, so its exact replay returns the same outcome.
        assert (
            rs.record_policy(
                connection,
                command_id="policy-implicit",
                adoption=PolicyAdoption.PROPOSED,
                timezone=NEW_YORK,
                at=AT,
            )
            == implicit
        )

        explicit = record_adopted(connection, expected_version=1)
        assert explicit.outcome is rs.CommandOutcome.APPLIED
        assert explicit.version == 2

        # After version 2 an omitted expected version is still stale and changes nothing.
        after_v2 = rs.record_policy(
            connection,
            command_id="policy-implicit-after-v2",
            adoption=PolicyAdoption.PROPOSED,
            timezone=NEW_YORK,
            at=AT,
        )
        assert after_v2.outcome is rs.CommandOutcome.STALE
        assert after_v2.version == 2
        assert after_v2.revision is None
        snapshot = rs.policy_snapshot(connection)
        assert snapshot is not None
        assert snapshot.version == 2
        assert snapshot.adoption is PolicyAdoption.ADOPTED
        assert [revision.version for revision in rs.policy_revisions(connection)] == [1, 2]

        # The original first command still replays as its original applied outcome.
        assert record_proposed(connection) == first
    finally:
        connection.close()


def test_two_connections_racing_the_first_policy_record_one_version(db_path: Path) -> None:
    outcomes: list[rs.CommandOutcome] = []
    errors: list[Exception] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def contend(command_id: str) -> None:
        connection = connect(db_path)
        try:
            barrier.wait(timeout=10)
            result = record_proposed(connection, command_id)
        except Exception as error:
            with lock:
                errors.append(error)
        else:
            with lock:
                outcomes.append(result.outcome)
        finally:
            connection.close()

    threads = [threading.Thread(target=contend, args=(value,)) for value in ("p1", "p2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert sorted(outcome.value for outcome in outcomes) == ["applied", "stale"]
    connection = connect(db_path)
    try:
        revisions = rs.policy_revisions(connection)
        assert [revision.version for revision in revisions] == [1]
        assert row_count(connection, "pb_retention_policies") == 1
        assert row_count(connection, "pb_retention_policy_commands") == 2
    finally:
        connection.close()


def test_closure_generation_requires_an_explicit_match_and_persists_history(
    db_path: Path,
) -> None:
    connection = connect(db_path)
    try:
        first = record_closed(connection)
        assert first.outcome is rs.CommandOutcome.APPLIED
        assert first.generation == 1
        assert first.revision is not None
        assert first.revision.kind is ClosureKind.CLOSED

        silent = rs.record_closure(
            connection,
            command_id="closure-silent",
            case_token=CASE,
            kind=ClosureKind.ABANDONED,
            anchor_at=OLD,
            source_ref="anchor/other",
            at=AT,
        )
        assert silent.outcome is rs.CommandOutcome.STALE
        assert silent.generation == 1
        snapshot = rs.closure_snapshot(connection, CASE)
        assert snapshot is not None
        assert snapshot.kind is ClosureKind.CLOSED

        replaced = rs.record_closure(
            connection,
            command_id="closure-2",
            case_token=CASE,
            kind=ClosureKind.ABANDONED,
            anchor_at=OLD,
            source_ref="anchor/source-2",
            at=AT,
            expected_generation=1,
        )
        assert replaced.outcome is rs.CommandOutcome.APPLIED
        assert replaced.generation == 2
        assert [item.generation for item in rs.closure_revisions(connection, CASE)] == [1, 2]

        replay = record_closed(connection)
        assert replay.outcome is rs.CommandOutcome.APPLIED
        assert replay.generation == 1
        assert replay.revision is not None
        assert replay.revision.kind is ClosureKind.CLOSED

        with pytest.raises(rs.CommandIdReused) as error:
            rs.record_closure(
                connection,
                command_id="closure-1",
                case_token=CASE,
                kind=ClosureKind.ABANDONED,
                anchor_at=AT,
                source_ref="anchor/source-1",
                at=AT,
            )
        assert error.value.reason is rs.RetentionStateRefusal.COMMAND_ID_REUSED

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE pb_retention_case_closures SET closure_kind='abandoned'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM pb_retention_case_closures")
    finally:
        connection.close()

    reopened = connect(db_path)
    try:
        latest = rs.closure_snapshot(reopened, CASE)
        assert latest is not None
        assert latest.generation == 2
        assert latest.kind is ClosureKind.ABANDONED
        assert latest.source_ref == "anchor/source-2"
    finally:
        reopened.close()


def test_holds_retain_distinct_scopes_never_expire_and_need_generation_checked_releases(
    db_path: Path,
) -> None:
    connection = connect(db_path)
    try:
        service = record_service_hold(connection, "hold-service", at=OLD)
        assert service.outcome is rs.CommandOutcome.APPLIED
        assert service.generation == 1
        financial = record_service_hold(
            connection,
            "hold-financial",
            hold_id="hold-b",
            scope=DataScope.FINANCIAL_PROVIDER_DATA,
            at=OLD,
        )
        assert financial.outcome is rs.CommandOutcome.APPLIED

        stored = rs.holds_snapshot(connection, CASE)
        assert [(hold.hold_id, hold.scope, hold.active) for hold in stored] == [
            ("hold-a", DataScope.SERVICE_DATA, True),
            ("hold-b", DataScope.FINANCIAL_PROVIDER_DATA, True),
        ]
        # Years later the hold is still active: nothing expires it automatically.
        assert rs.hold_snapshot(connection, CASE, "hold-a") == service.revision

        with pytest.raises(rs.RetentionStateInputError) as error:
            release(connection, "release-zero", 0)
        assert error.value.reason is rs.RetentionStateRefusal.INVALID_EXPECTED_GENERATION

        stale = release(connection, "release-stale", 2)
        assert stale.outcome is rs.CommandOutcome.STALE
        still_active = rs.hold_snapshot(connection, CASE, "hold-a")
        assert still_active is not None
        assert still_active.active

        released = release(connection, "release-1", 1)
        assert released.outcome is rs.CommandOutcome.APPLIED
        assert released.generation == 2
        assert released.revision is not None
        assert not released.revision.active
        assert released.revision.released_at == AT

        reheld = record_service_hold(connection, "hold-2", expected_generation=2)
        assert reheld.outcome is rs.CommandOutcome.APPLIED
        assert reheld.generation == 3
        assert reheld.revision is not None
        assert reheld.revision.active
        assert reheld.revision.scope is DataScope.SERVICE_DATA

        # A different scope always needs its own identity; hold-a stays service-scoped.
        unknown = record_service_hold(
            connection,
            "hold-unknown",
            hold_id="hold-c",
            scope=DataScope.UNKNOWN_PROTECTED_DATA,
        )
        assert unknown.outcome is rs.CommandOutcome.APPLIED
        assert unknown.revision is not None
        assert unknown.revision.scope is DataScope.UNKNOWN_PROTECTED_DATA

        # A release that observed generation 2 cannot clear the newer generation 3 hold.
        stale_newer = release(connection, "release-stale-newer", 2)
        assert stale_newer.outcome is rs.CommandOutcome.STALE
        newer = rs.hold_snapshot(connection, CASE, "hold-a")
        assert newer is not None
        assert newer.active
        assert newer.generation == 3

        released_newer = release(connection, "release-3", 3)
        assert released_newer.outcome is rs.CommandOutcome.APPLIED
        assert released_newer.generation == 4
        assert release(connection, "release-again", 3).outcome is rs.CommandOutcome.STALE

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM pb_retention_holds")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE pb_retention_hold_commands SET outcome='stale'")
    finally:
        connection.close()

    reopened = connect(db_path)
    try:
        final = rs.hold_snapshot(reopened, CASE, "hold-a")
        assert final is not None
        assert final.generation == 4
        assert not final.active
    finally:
        reopened.close()


def test_hold_replay_and_conflicting_reuse_are_exact(database: sqlite3.Connection) -> None:
    first = record_service_hold(database)
    assert first.outcome is rs.CommandOutcome.APPLIED
    assert record_service_hold(database) == first

    with pytest.raises(rs.CommandIdReused) as error:
        record_service_hold(database, command_id="hold-1", scope=DataScope.FINANCIAL_PROVIDER_DATA)
    assert error.value.reason is rs.RetentionStateRefusal.COMMAND_ID_REUSED

    with pytest.raises(rs.CommandIdReused):
        record_service_hold(database, command_id="hold-1", at=datetime(2026, 1, 2, tzinfo=UTC))

    released = release(database, "release-1", 1)
    assert released == release(database, "release-1", 1)
    with pytest.raises(rs.CommandIdReused):
        release(database, "release-1", 2)


def test_hold_identity_scope_cannot_change_even_with_the_exact_generation(
    database: sqlite3.Connection,
) -> None:
    first = record_service_hold(database)
    assert first.outcome is rs.CommandOutcome.APPLIED
    before = rows(database, "pb_retention_holds")

    with pytest.raises(rs.HoldScopeConflict) as error:
        record_service_hold(
            database,
            "hold-scope-change",
            scope=DataScope.FINANCIAL_PROVIDER_DATA,
            expected_generation=1,
        )
    assert error.value.reason is rs.RetentionStateRefusal.HOLD_SCOPE_CONFLICT
    # Fixed refusal with no state mutation: neither the hold nor its command log moved.
    assert rows(database, "pb_retention_holds") == before
    assert row_count(database, "pb_retention_hold_commands") == 1
    assert rs.hold_snapshot(database, CASE, "hold-a") == first.revision

    # Replaying the original command still returns its original applied outcome.
    assert record_service_hold(database) == first


def test_hold_scope_stays_fixed_after_release_and_needs_a_distinct_identity(
    database: sqlite3.Connection,
) -> None:
    record_service_hold(database)
    released = release(database, "release-1", 1)
    assert released.outcome is rs.CommandOutcome.APPLIED
    before = rows(database, "pb_retention_holds")

    with pytest.raises(rs.HoldScopeConflict) as error:
        record_service_hold(
            database,
            "hold-scope-after-release",
            scope=DataScope.UNKNOWN_PROTECTED_DATA,
            expected_generation=2,
        )
    assert error.value.reason is rs.RetentionStateRefusal.HOLD_SCOPE_CONFLICT
    assert rows(database, "pb_retention_holds") == before
    still_released = rs.hold_snapshot(database, CASE, "hold-a")
    assert still_released is not None
    assert still_released.generation == 2
    assert not still_released.active
    assert still_released.scope is DataScope.SERVICE_DATA

    # The distinct scope is accepted only under a distinct hold identity.
    distinct = record_service_hold(
        database,
        "hold-distinct-scope",
        hold_id="hold-b",
        scope=DataScope.UNKNOWN_PROTECTED_DATA,
    )
    assert distinct.outcome is rs.CommandOutcome.APPLIED
    assert distinct.revision is not None
    assert distinct.revision.scope is DataScope.UNKNOWN_PROTECTED_DATA


def test_omitted_generation_only_creates_a_never_seen_hold(
    database: sqlite3.Connection,
) -> None:
    first = record_service_hold(database)
    assert first.outcome is rs.CommandOutcome.APPLIED
    before = rows(database, "pb_retention_holds")

    silent = record_service_hold(database, "hold-silent")
    assert silent.outcome is rs.CommandOutcome.STALE
    assert silent.generation == 1
    assert silent.revision is None
    assert rows(database, "pb_retention_holds") == before
    # The stale command is saved, so its exact replay returns the same outcome.
    assert record_service_hold(database, "hold-silent") == silent
    assert rs.hold_snapshot(database, CASE, "hold-a") == first.revision

    # The exact current generation is accepted, so an explicit reassertion still succeeds.
    reasserted = record_service_hold(database, "hold-explicit", expected_generation=1)
    assert reasserted.outcome is rs.CommandOutcome.APPLIED
    assert reasserted.generation == 2
    assert reasserted.revision is not None
    assert reasserted.revision.active
    assert reasserted.revision.scope is DataScope.SERVICE_DATA

    released = release(database, "release-2", 2)
    assert released.outcome is rs.CommandOutcome.APPLIED
    # After a release an omitted generation is still stale and removes nothing.
    after_release = record_service_hold(database, "hold-silent-2")
    assert after_release.outcome is rs.CommandOutcome.STALE
    assert after_release.generation == 3
    stored = rs.hold_snapshot(database, CASE, "hold-a")
    assert stored is not None
    assert stored.generation == 3
    assert not stored.active


def test_unknown_case_is_refused_and_never_binds_a_command(database: sqlite3.Connection) -> None:
    with pytest.raises(rs.UnknownCase) as error:
        rs.record_closure(
            database,
            command_id="unknown-closure",
            case_token="no-such-case",
            kind=ClosureKind.CLOSED,
            anchor_at=AT,
            source_ref="anchor/source-1",
            at=AT,
        )
    assert error.value.reason is rs.RetentionStateRefusal.UNKNOWN_CASE

    with pytest.raises(rs.UnknownCase):
        rs.record_hold(
            database,
            command_id="unknown-hold",
            case_token="no-such-case",
            hold_id="hold-a",
            scope=DataScope.SERVICE_DATA,
            source_ref="hold/source-1",
            at=AT,
        )

    for read in (
        lambda: rs.case_snapshot(database, "no-such-case"),
        lambda: rs.closure_snapshot(database, "no-such-case"),
        lambda: rs.holds_snapshot(database, "no-such-case"),
        lambda: rs.hold_snapshot(database, "no-such-case", "hold-a"),
    ):
        with pytest.raises(rs.UnknownCase):
            read()

    assert row_count(database, "pb_retention_closure_commands") == 0
    assert row_count(database, "pb_retention_hold_commands") == 0
    assert row_count(database, "pb_retention_case_closures") == 0
    assert row_count(database, "pb_retention_holds") == 0


def test_two_connections_racing_the_same_closure_command_produce_one_result(
    db_path: Path,
) -> None:
    outcomes: list[rs.CommandOutcome] = []
    errors: list[Exception] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def contend(command_id: str) -> None:
        connection = connect(db_path)
        try:
            barrier.wait(timeout=10)
            result = record_closed(connection, command_id, expected_generation=0)
        except Exception as error:
            with lock:
                errors.append(error)
        else:
            with lock:
                outcomes.append(result.outcome)
        finally:
            connection.close()

    threads = [threading.Thread(target=contend, args=(value,)) for value in ("c1", "c2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert sorted(outcome.value for outcome in outcomes) == ["applied", "stale"]
    connection = connect(db_path)
    try:
        latest = rs.closure_snapshot(connection, CASE)
        assert latest is not None
        assert latest.generation == 1
        assert row_count(connection, "pb_retention_case_closures") == 1
        assert row_count(connection, "pb_retention_closure_commands") == 2
    finally:
        connection.close()


def test_two_connections_racing_the_same_hold_and_release_produce_one_result(
    db_path: Path,
) -> None:
    hold_outcomes: list[rs.CommandOutcome] = []
    errors: list[Exception] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def contend_hold(command_id: str) -> None:
        connection = connect(db_path)
        try:
            barrier.wait(timeout=10)
            result = record_service_hold(connection, command_id, expected_generation=0)
        except Exception as error:
            with lock:
                errors.append(error)
        else:
            with lock:
                hold_outcomes.append(result.outcome)
        finally:
            connection.close()

    threads = [threading.Thread(target=contend_hold, args=(value,)) for value in ("h1", "h2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert sorted(outcome.value for outcome in hold_outcomes) == ["applied", "stale"]

    connection = connect(db_path)
    try:
        held = rs.hold_snapshot(connection, CASE, "hold-a")
        assert held is not None
        assert held.generation == 1
        assert held.active
    finally:
        connection.close()

    release_outcomes: list[rs.CommandOutcome] = []
    errors.clear()
    release_barrier = threading.Barrier(2)

    def contend_release(command_id: str) -> None:
        connection = connect(db_path)
        try:
            release_barrier.wait(timeout=10)
            result = release(connection, command_id, 1)
        except Exception as error:
            with lock:
                errors.append(error)
        else:
            with lock:
                release_outcomes.append(result.outcome)
        finally:
            connection.close()

    release_threads = [
        threading.Thread(target=contend_release, args=(value,)) for value in ("r1", "r2")
    ]
    for thread in release_threads:
        thread.start()
    for thread in release_threads:
        thread.join(timeout=30)

    assert not errors
    assert sorted(outcome.value for outcome in release_outcomes) == ["applied", "stale"]
    connection = connect(db_path)
    try:
        final = rs.hold_snapshot(connection, CASE, "hold-a")
        assert final is not None
        assert final.generation == 2
        assert not final.active
        assert row_count(connection, "pb_retention_holds") == 1
        assert row_count(connection, "pb_retention_hold_commands") == 4
    finally:
        connection.close()


def test_command_failure_rolls_back_its_state_change(
    database: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_after_write(connection: sqlite3.Connection, command_id: str) -> rs.HoldCommandResult:
        raise RuntimeError("simulated failure after the state write")

    monkeypatch.setattr(rs, "_hold_command_result", fail_after_write)
    with pytest.raises(RuntimeError):
        record_service_hold(database)
    assert rs.hold_snapshot(database, CASE, "hold-a") is None
    assert row_count(database, "pb_retention_holds") == 0
    assert row_count(database, "pb_retention_hold_commands") == 0

    monkeypatch.undo()
    assert record_service_hold(database).outcome is rs.CommandOutcome.APPLIED


def test_a_caller_active_transaction_is_refused_without_committing_caller_work(
    database: sqlite3.Connection,
) -> None:
    database.execute("BEGIN IMMEDIATE")
    try:
        database.execute("INSERT INTO pb_existing VALUES ('caller-work')")
        with pytest.raises(rs.RetentionTransactionRefused) as error:
            record_service_hold(database)
        assert error.value.reason is rs.RetentionStateRefusal.CALLER_TRANSACTION_ACTIVE
        assert ("caller-work",) in rows(database, "pb_existing")
    finally:
        database.execute("ROLLBACK")

    assert ("caller-work",) not in rows(database, "pb_existing")
    assert rs.hold_snapshot(database, CASE, "hold-a") is None
    assert row_count(database, "pb_retention_hold_commands") == 0


def test_invalid_inputs_fail_closed_with_fixed_reasons(database: sqlite3.Connection) -> None:
    with pytest.raises(rs.RetentionStateInputError) as error:
        rs.record_policy(
            database,
            command_id="",
            adoption=PolicyAdoption.PROPOSED,
            timezone=UTC_TZ,
            at=AT,
        )
    assert error.value.reason is rs.RetentionStateRefusal.INVALID_COMMAND_ID

    with pytest.raises(rs.RetentionStateInputError) as error:
        rs.record_policy(
            database,
            command_id="p",
            adoption=cast(PolicyAdoption, "adopted"),
            timezone=UTC_TZ,
            at=AT,
        )
    assert error.value.reason is rs.RetentionStateRefusal.INVALID_ADOPTION

    with pytest.raises(rs.RetentionStateInputError) as error:
        rs.record_policy(
            database,
            command_id="p",
            adoption=PolicyAdoption.PROPOSED,
            timezone=cast(ZoneInfo, "UTC"),
            at=AT,
        )
    assert error.value.reason is rs.RetentionStateRefusal.INVALID_TIMEZONE

    with pytest.raises(rs.RetentionStateInputError) as error:
        rs.record_policy(
            database,
            command_id="p",
            adoption=PolicyAdoption.PROPOSED,
            timezone=UTC_TZ,
            at=datetime(2026, 1, 1, 12),
        )
    assert error.value.reason is rs.RetentionStateRefusal.NAIVE_TIMESTAMP

    with pytest.raises(rs.RetentionStateInputError) as error:
        rs.record_policy(
            database,
            command_id="p",
            adoption=PolicyAdoption.PROPOSED,
            timezone=UTC_TZ,
            at=AT,
            retention_years=2,
        )
    assert error.value.reason is rs.RetentionStateRefusal.INVALID_RETENTION_YEARS

    with pytest.raises(rs.RetentionStateInputError) as error:
        rs.record_policy(
            database,
            command_id="p",
            adoption=PolicyAdoption.PROPOSED,
            timezone=UTC_TZ,
            at=AT,
            expected_version=-1,
        )
    assert error.value.reason is rs.RetentionStateRefusal.INVALID_EXPECTED_VERSION

    with pytest.raises(rs.RetentionStateInputError) as error:
        rs.record_closure(
            database,
            command_id="c",
            case_token=" ",
            kind=ClosureKind.CLOSED,
            anchor_at=AT,
            source_ref="anchor/source-1",
            at=AT,
        )
    assert error.value.reason is rs.RetentionStateRefusal.INVALID_CASE_TOKEN

    with pytest.raises(rs.RetentionStateInputError) as error:
        rs.record_closure(
            database,
            command_id="c",
            case_token=CASE,
            kind=cast(ClosureKind, "closed"),
            anchor_at=AT,
            source_ref="anchor/source-1",
            at=AT,
        )
    assert error.value.reason is rs.RetentionStateRefusal.INVALID_CLOSURE_KIND

    with pytest.raises(rs.RetentionStateInputError) as error:
        rs.record_closure(
            database,
            command_id="c",
            case_token=CASE,
            kind=ClosureKind.CLOSED,
            anchor_at=AT,
            source_ref="has a space",
            at=AT,
        )
    assert error.value.reason is rs.RetentionStateRefusal.INVALID_SOURCE_REF

    with pytest.raises(rs.RetentionStateInputError) as error:
        rs.record_closure(
            database,
            command_id="c",
            case_token=CASE,
            kind=ClosureKind.CLOSED,
            anchor_at=AT,
            source_ref="a" * 300,
            at=AT,
        )
    assert error.value.reason is rs.RetentionStateRefusal.INVALID_SOURCE_REF

    with pytest.raises(rs.RetentionStateInputError) as error:
        rs.record_hold(
            database,
            command_id="h",
            case_token=CASE,
            hold_id="",
            scope=DataScope.SERVICE_DATA,
            source_ref="hold/source-1",
            at=AT,
        )
    assert error.value.reason is rs.RetentionStateRefusal.INVALID_HOLD_ID

    with pytest.raises(rs.RetentionStateInputError) as error:
        rs.record_hold(
            database,
            command_id="h",
            case_token=CASE,
            hold_id="hold-a",
            scope=cast(DataScope, "other_scope"),
            source_ref="hold/source-1",
            at=AT,
        )
    assert error.value.reason is rs.RetentionStateRefusal.INVALID_SCOPE

    with pytest.raises(rs.RetentionStateInputError) as error:
        rs.record_hold(
            database,
            command_id="h",
            case_token=CASE,
            hold_id="hold-a",
            scope=DataScope.SERVICE_DATA,
            source_ref="hold/source-1",
            at=AT,
            expected_generation=True,
        )
    assert error.value.reason is rs.RetentionStateRefusal.INVALID_EXPECTED_GENERATION


def test_malformed_schema_version_and_rows_fail_closed(tmp_path: Path) -> None:
    # A missing component row is not an implicit initialization.
    path = tmp_path / "missing.sqlite"
    connection = make_database(path)
    try:
        with pytest.raises(rs.RetentionStateCorrupted) as error:
            rs.policy_snapshot(connection)
        assert error.value.reason is rs.RetentionStateRefusal.COMPONENT_MISSING
    finally:
        connection.close()

    # Unsupported version.
    path = tmp_path / "version.sqlite"
    connection = make_database(path)
    try:
        rs.initialize_retention_state(connection)
        connection.execute(
            "UPDATE pb_store_versions SET version=2 WHERE component='retention_state'"
        )
        with pytest.raises(rs.RetentionStateCorrupted) as error:
            rs.initialize_retention_state(connection)
        assert error.value.reason is rs.RetentionStateRefusal.VERSION_UNSUPPORTED
        with pytest.raises(rs.RetentionStateCorrupted) as error:
            rs.policy_snapshot(connection)
        assert error.value.reason is rs.RetentionStateRefusal.VERSION_UNSUPPORTED
    finally:
        connection.close()

    # A malformed trigger is refused; a merely missing trigger is recreated.
    path = tmp_path / "trigger.sqlite"
    connection = make_database(path)
    try:
        rs.initialize_retention_state(connection)
        connection.execute("DROP TRIGGER pb_retention_policies_no_update")
        rs.initialize_retention_state(connection)
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name='pb_retention_policies_no_update'"
            ).fetchone()
            is not None
        )
        connection.execute("DROP TRIGGER pb_retention_policies_no_update")
        connection.execute(
            "CREATE TRIGGER pb_retention_policies_no_update "
            "BEFORE UPDATE ON pb_retention_policies WHEN 0 BEGIN SELECT 1; END"
        )
        with pytest.raises(rs.RetentionStateCorrupted) as error:
            rs.initialize_retention_state(connection)
        assert error.value.reason is rs.RetentionStateRefusal.SCHEMA_MALFORMED
    finally:
        connection.close()

    # An unexpected retention object is refused.
    path = tmp_path / "extra.sqlite"
    connection = make_database(path)
    try:
        rs.initialize_retention_state(connection)
        connection.execute("CREATE TABLE pb_retention_extra (value TEXT)")
        with pytest.raises(rs.RetentionStateCorrupted) as error:
            rs.initialize_retention_state(connection)
        assert error.value.reason is rs.RetentionStateRefusal.SCHEMA_MALFORMED
    finally:
        connection.close()


def test_malformed_rows_fail_closed_on_read_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "rows.sqlite"
    connection = make_database(path)
    try:
        rs.initialize_retention_state(connection)
        connection.execute(
            "INSERT INTO pb_retention_policies "
            "(command_id, version, adoption, timezone, retention_years, "
            "adoption_evidence_ref, recorded_at) "
            "VALUES ('bad-policy', 1, 'proposed', 'UTC', 1, NULL, '2026-01-01T00:00:00')"
        )
        with pytest.raises(rs.RetentionStateCorrupted) as error:
            rs.policy_snapshot(connection)
        assert error.value.reason is rs.RetentionStateRefusal.ROW_MALFORMED
        with pytest.raises(rs.RetentionStateCorrupted) as error:
            rs.initialize_retention_state(connection)
        assert error.value.reason is rs.RetentionStateRefusal.ROW_MALFORMED
    finally:
        connection.close()

    path = tmp_path / "row-closures.sqlite"
    connection = make_database(path)
    try:
        rs.initialize_retention_state(connection)
        connection.execute(
            "INSERT INTO pb_retention_case_closures "
            "(case_token, generation, command_id, closure_kind, anchor_at, source_ref, "
            "recorded_at) VALUES (?, 1, 'bad-closure', 'closed', ?, ?, ?)",
            (CASE, AT.isoformat(), "a" * 300, AT.isoformat()),
        )
        with pytest.raises(rs.RetentionStateCorrupted) as error:
            rs.closure_snapshot(connection, CASE)
        assert error.value.reason is rs.RetentionStateRefusal.ROW_MALFORMED
    finally:
        connection.close()


def test_command_rows_contradicting_the_stricter_authority_rules_are_malformed(
    tmp_path: Path,
) -> None:
    # An applied policy beyond version 1 cannot have omitted its expected version.
    path = tmp_path / "policy-command.sqlite"
    connection = make_database(path)
    try:
        rs.initialize_retention_state(connection)
        connection.execute(
            "INSERT INTO pb_retention_policy_commands "
            "(command_id, adoption, timezone, retention_years, adoption_evidence_ref, "
            "expected_version, outcome, version, at) "
            "VALUES ('bad-policy-command', 'proposed', 'UTC', 1, NULL, NULL, 'applied', 2, ?)",
            (AT.isoformat(),),
        )
        with pytest.raises(rs.RetentionStateCorrupted) as error:
            rs.initialize_retention_state(connection)
        assert error.value.reason is rs.RetentionStateRefusal.ROW_MALFORMED
    finally:
        connection.close()

    # An applied hold beyond generation 1 cannot have omitted its expected generation.
    path = tmp_path / "hold-command.sqlite"
    connection = make_database(path)
    try:
        rs.initialize_retention_state(connection)
        connection.execute(
            "INSERT INTO pb_retention_hold_commands "
            "(command_id, case_token, hold_id, command, scope, source_ref, expected_generation, "
            "outcome, generation, active, post_scope, post_source_ref, post_held_at, "
            "post_released_at, at) VALUES ('bad-hold-command', ?, 'hold-a', 'hold', "
            "'service_data', 'hold/source-1', NULL, 'applied', 2, 1, 'service_data', "
            "'hold/source-1', ?, NULL, ?)",
            (CASE, AT.isoformat(), AT.isoformat()),
        )
        with pytest.raises(rs.RetentionStateCorrupted) as error:
            rs.initialize_retention_state(connection)
        assert error.value.reason is rs.RetentionStateRefusal.ROW_MALFORMED
    finally:
        connection.close()


def test_stored_inputs_are_frozen_and_runtime_blockers_are_not_inferred(
    database: sqlite3.Connection,
) -> None:
    record_adopted(database, expected_version=None)
    record_closed(database, anchor_at=datetime(2025, 1, 1, 12, 0, tzinfo=UTC))
    record_service_hold(database)

    snapshot = rs.case_snapshot(database, CASE)
    assert dataclasses.is_dataclass(snapshot)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.__setattr__("case_token", OTHER_CASE)
    assert snapshot.policy is not None
    assert snapshot.closure is not None

    inputs = rs.evaluation_inputs(database, CASE)
    assert inputs.policy is not None
    assert inputs.policy.adoption is PolicyAdoption.ADOPTED
    assert inputs.policy.timezone.key == "UTC"
    assert inputs.holds == (PreservationHold(scope=DataScope.SERVICE_DATA, released=False),)
    assert len(inputs.anchors) == 1
    assert inputs.anchors[0].kind is ClosureKind.CLOSED

    held = evaluate_service_retention(
        inputs.policy,
        scope=DataScope.SERVICE_DATA,
        evaluated_at=EVALUATED,
        anchors=inputs.anchors,
        holds=inputs.holds,
        blockers=UnresolvedBlockers(),
    )
    assert not held.eligible
    assert RetentionRefused.PRESERVATION_HOLD_ACTIVE in held.refusals

    # No blockers were persisted or inferred: omitting them still fails closed.
    missing_blockers = evaluate_service_retention(
        inputs.policy,
        scope=DataScope.SERVICE_DATA,
        evaluated_at=EVALUATED,
        anchors=inputs.anchors,
        holds=inputs.holds,
        blockers=None,
    )
    assert RetentionRefused.INPUT_INVALID in missing_blockers.refusals

    release(database, "release-1", 1)
    released_inputs = rs.evaluation_inputs(database, CASE)
    assert released_inputs.holds == (PreservationHold(scope=DataScope.SERVICE_DATA, released=True),)
    still_missing = evaluate_service_retention(
        released_inputs.policy,
        scope=DataScope.SERVICE_DATA,
        evaluated_at=EVALUATED,
        anchors=released_inputs.anchors,
        holds=released_inputs.holds,
        blockers=None,
    )
    assert RetentionRefused.INPUT_INVALID in still_missing.refusals

    money_open = evaluate_service_retention(
        released_inputs.policy,
        scope=DataScope.SERVICE_DATA,
        evaluated_at=EVALUATED,
        anchors=released_inputs.anchors,
        holds=released_inputs.holds,
        blockers=UnresolvedBlockers(money=True),
    )
    assert RetentionRefused.MONEY_UNRESOLVED in money_open.refusals

    eligible = evaluate_service_retention(
        released_inputs.policy,
        scope=DataScope.SERVICE_DATA,
        evaluated_at=EVALUATED,
        anchors=released_inputs.anchors,
        holds=released_inputs.holds,
        blockers=UnresolvedBlockers(),
    )
    assert eligible.eligible
    assert eligible.refusals == ()
