from __future__ import annotations

import json
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID

import pytest

from mail_samples import make_request
from parkingbreaker import case_state as cs
from parkingbreaker.case_state import CaseState, StopFamily
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import Accepted, LetterEvidence, LobResponse, Uncertain
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.oplog.counters import CounterKey, decode_counter_key
from parkingbreaker.oplog.digest import (
    CounterMilestone,
    JurisdictionExclusion,
    PhaseReached,
    ResearchStatus,
    make_digest,
)
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.transaction_records import WorkspaceRecord, decode_workspace, workspace_json
from parkingbreaker.transaction_store import CaseConflict, RevisionConflict, SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_oplog_digest import _kwargs
from test_submission_coordinator import prepared, step
from test_transaction_lifecycle import letter

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
EXPIRES = NOW + timedelta(days=5)
WORKSPACE = Workspace(
    notes="The meter was broken.",
    position="Please dismiss the citation.",
    photo_refs=("photo-1",),
    provision_selections=frozenset({"code-2", "code-1"}),
    letter_edits=("Original draft", "Customer revision"),
    answers={"NOTICE_ISSUE_DATE": "2026-10-01"},
)


def test_workspace_codec_retains_all_customer_data() -> None:
    encoded = workspace_json(WORKSPACE)
    assert decode_workspace(encoded) == WORKSPACE
    assert json.loads(encoded)["provision_selections"] == ["code-1", "code-2"]
    assert workspace_json(decode_workspace(encoded)) == encoded


@pytest.mark.parametrize(
    "text",
    [
        "null",
        "{}",
        "malformed",
        workspace_json(WORKSPACE).replace('"notes":', '"notes":0,"notes":'),
        workspace_json(WORKSPACE).replace('"notes":', '"unknown":true,"notes":'),
        workspace_json(WORKSPACE).replace('"photo_refs":["photo-1"]', '"photo_refs":[1]'),
        workspace_json(WORKSPACE).replace('"position":', '"position":NaN,"position":'),
        workspace_json(WORKSPACE).replace('"code-1","code-2"', '"code-1","code-1"'),
    ],
)
def test_workspace_codec_rejects_ambiguous_or_malformed_data(text: str) -> None:
    with pytest.raises(ValueError, match=r".+"):
        decode_workspace(text)


def test_workspace_and_mail_store_share_one_durable_database(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        workspace = store.save_workspace("workspace-1", WORKSPACE, NOW)
        prepared = store.prepare("mail-case", 1, make_request(), NOW)
        assert workspace.revision == 1
    with SqliteTransactionStore(path) as store:
        assert store.get_workspace("workspace-1") == workspace
        assert store.get("mail-case", 1) == prepared


def test_workspace_updates_keep_history_and_reject_stale_writes(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        original = store.save_workspace("workspace-1", WORKSPACE, NOW)
        changed = replace(WORKSPACE, notes="The customer corrected their notes.")
        second = store.save_workspace(
            "workspace-1", changed, NOW + timedelta(minutes=1), expected_revision=original.revision
        )
        assert second.revision == 2
        with pytest.raises(RevisionConflict):
            store.save_workspace("workspace-1", WORKSPACE, NOW, expected_revision=1)
        with pytest.raises(RevisionConflict):
            store.save_workspace("workspace-1", WORKSPACE, NOW)
        assert store.get_workspace("workspace-1") == second
        assert store.workspace_history("workspace-1") == (original, second)


def test_case_authorization_clock_is_immutable_under_workspace_activity(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        store.save_workspace("workspace-1", WORKSPACE, NOW)
        case = store.start_case("workspace-1", "test-auth-1", NOW, EXPIRES)
        assert UUID(case.case_token).version == 4
        assert case.letter_state is CaseState.RESEARCHING
        assert case.payment_state is PaymentState.AUTHORIZED
        assert case.attempt == case.revision == 1
        assert case.amount_cents == 2999
        store.save_workspace(
            "workspace-1", replace(WORKSPACE, notes="Updated"), NOW + timedelta(minutes=20), 1
        )
        assert store.start_case("workspace-1", "test-auth-1", NOW, EXPIRES) == case
        with pytest.raises(CaseConflict):
            store.start_case("workspace-1", "test-auth-1", NOW + timedelta(minutes=20), EXPIRES)
    with SqliteTransactionStore(path) as store:
        assert store.get_case(case.case_token) == case
        assert store.get_workspace("workspace-1") is not None


def test_new_authorization_creates_distinct_case_for_preserved_workspace(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        workspace = store.save_workspace("workspace-1", WORKSPACE, NOW)
        first = store.start_case("workspace-1", "test-auth-1", NOW, EXPIRES)
        second = store.start_case(
            "workspace-1", "test-auth-2", NOW + timedelta(minutes=31), EXPIRES
        )
        assert first.case_token != second.case_token
        assert first.authorized_at == NOW
        assert second.authorized_at == NOW + timedelta(minutes=31)
        assert store.get_case(first.case_token) == first
        assert store.get_workspace("workspace-1") == workspace


def test_existing_authorization_cannot_bind_to_another_workspace(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        store.save_workspace("workspace-1", WORKSPACE, NOW)
        store.save_workspace("workspace-2", Workspace(), NOW)
        store.start_case("workspace-1", "test-auth-1", NOW, EXPIRES)
        with pytest.raises(CaseConflict):
            store.start_case("workspace-2", "test-auth-1", NOW, EXPIRES)


def test_case_requires_known_workspace_and_valid_authorization(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        with pytest.raises(ValueError, match="workspace"):
            store.start_case("missing", "test-auth-1", NOW, EXPIRES)
        store.save_workspace("workspace-1", WORKSPACE, NOW)
        with pytest.raises(ValueError, match=r"expir|authorization"):
            store.start_case("workspace-1", "test-auth-1", NOW, NOW)
        with pytest.raises(ValueError, match="timezone-aware"):
            store.start_case("workspace-1", "test-auth-1", NOW.replace(tzinfo=None), EXPIRES)


def test_workspace_snapshot_does_not_alias_input_answers(tmp_path: Path) -> None:
    answers = {"NOTICE_ISSUE_DATE": "2026-10-01"}
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        saved = store.save_workspace("workspace-1", replace(WORKSPACE, answers=answers), NOW)
        answers["NOTICE_ISSUE_DATE"] = "2026-12-01"
        read = store.get_workspace("workspace-1")
        assert read is not None
        assert read.workspace.answers["NOTICE_ISSUE_DATE"] == "2026-10-01"
        assert saved.workspace.answers["NOTICE_ISSUE_DATE"] == "2026-10-01"


def test_unknown_transaction_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        store.save_workspace("workspace-1", WORKSPACE, NOW)
    with sqlite3.connect(path) as database:
        database.execute("UPDATE pb_store_versions SET version=999 WHERE component='transactions'")
    with pytest.raises(StorageCorruption):
        SqliteTransactionStore(path)
    with sqlite3.connect(path) as database:
        assert database.execute("SELECT COUNT(*) FROM pb_workspaces").fetchone() == (1,)


def test_corrupt_workspace_and_case_data_are_not_trusted(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        store.save_workspace("workspace-1", WORKSPACE, NOW)
        case = store.start_case("workspace-1", "test-auth-1", NOW, EXPIRES)
    with sqlite3.connect(path) as database:
        database.execute("UPDATE pb_cases SET letter_state='NOT_REAL'")
    with SqliteTransactionStore(path) as store, pytest.raises(StorageCorruption):
        store.get_case(case.case_token)
    with sqlite3.connect(path) as database:
        database.execute("UPDATE pb_workspaces SET payload_json='{}'")
    with SqliteTransactionStore(path) as store, pytest.raises(StorageCorruption):
        store.get_workspace("workspace-1")


@pytest.mark.parametrize("mismatch", [False, True])
def test_damaged_history_blocks_further_workspace_writes(tmp_path: Path, mismatch: bool) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        original = store.save_workspace("workspace-1", WORKSPACE, NOW)
    with sqlite3.connect(path) as database:
        if mismatch:
            database.execute(
                "UPDATE pb_workspace_history SET payload_json=?", (workspace_json(Workspace()),)
            )
        else:
            database.execute("DELETE FROM pb_workspace_history")
    with SqliteTransactionStore(path) as store:
        with pytest.raises(StorageCorruption, match="history"):
            store.save_workspace("workspace-1", Workspace(), NOW + timedelta(minutes=1), 1)
        assert store.get_workspace("workspace-1") == original


def test_concurrent_workspace_writers_have_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        original = store.save_workspace("workspace-1", WORKSPACE, NOW)
    barrier = Barrier(2)

    def save(notes: str) -> WorkspaceRecord | None:
        with SqliteTransactionStore(path) as store:
            barrier.wait(timeout=5)
            try:
                return store.save_workspace(
                    "workspace-1", replace(WORKSPACE, notes=notes), NOW + timedelta(minutes=1), 1
                )
            except RevisionConflict:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save, notes) for notes in ("Writer one", "Writer two")]
        winners = [
            result for future in futures if (result := future.result(timeout=10)) is not None
        ]
    assert len(winners) == 1
    with SqliteTransactionStore(path) as store:
        assert store.get_workspace("workspace-1") == winners[0]
        assert store.workspace_history("workspace-1") == (original, winners[0])


def test_history_reader_has_one_snapshot_during_concurrent_write(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        first = store.save_workspace("workspace-1", WORKSPACE, NOW)

    class InterleavedReader(SqliteTransactionStore):
        advanced = False

        def _history_snapshot(
            self, token: str, current: WorkspaceRecord | None
        ) -> tuple[WorkspaceRecord, ...]:
            if not self.advanced:
                self.advanced = True
                with SqliteTransactionStore(path) as peer:
                    peer.save_workspace(token, Workspace(notes="Concurrent change"), NOW, 1)
            return super()._history_snapshot(token, current)

    with InterleavedReader(path) as store:
        assert store.workspace_history("workspace-1") == (first,)
        current = store.get_workspace("workspace-1")
        assert current is not None
        assert current.revision == 2


def test_save_acknowledges_its_own_revision_after_later_writer_commits(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        connection = store._require_open()

        class CommitHook:
            fired = False

            @property
            def in_transaction(self) -> bool:
                return connection.in_transaction

            def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
                cursor = connection.execute(sql, parameters)
                if sql == "COMMIT" and not self.fired:
                    self.fired = True
                    with SqliteTransactionStore(path) as peer:
                        peer.save_workspace("workspace-1", Workspace(notes="Later writer"), NOW, 1)
                return cursor

            def close(self) -> None:
                connection.close()

        hook = CommitHook()
        store._database = hook  # type: ignore[assignment]
        saved = store.save_workspace("workspace-1", WORKSPACE, NOW)
        assert hook.fired
        assert saved.revision == 1
        assert saved.workspace == WORKSPACE
        current = store.get_workspace("workspace-1")
        assert current is not None
        assert current.revision == 2
        assert current.workspace.notes == "Later writer"


def test_corrupted_authorization_expiry_is_rejected_on_load(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        store.save_workspace("workspace-1", WORKSPACE, NOW)
        case = store.start_case("workspace-1", "test-auth-1", NOW, EXPIRES)
    with sqlite3.connect(path) as database:
        database.execute("UPDATE pb_cases SET authorization_expires_at=?", (NOW.isoformat(),))
    with SqliteTransactionStore(path) as store, pytest.raises(StorageCorruption, match="expiry"):
        store.get_case(case.case_token)


# These readers stand in for the analysis role. Production transaction methods never
# inspect this table, including during replay and failed delivery recovery.
def coverage_rows(path: Path) -> tuple[CounterKey, ...]:
    with sqlite3.connect(path) as database:
        return tuple(
            decode_counter_key(row[0])
            for row in database.execute(
                "SELECT payload_json FROM pb_coverage_counters ORDER BY rowid"
            )
        )


def test_pre_research_request_and_failed_verification_count_once_after_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        store.record_counter("unknown-city-request", CounterKey(), occurred_at=NOW)
        store.record_counter("unknown-city-request", CounterKey(), occurred_at=NOW)
        assert store.flush_counters(occurred_at=NOW)
    assert [row.milestone for row in coverage_rows(path)] == [CounterMilestone.REQUEST_RECEIVED]
    with SqliteTransactionStore(path) as store:
        digest = make_digest(
            **_kwargs(
                issuer_token="unknown",
                phase_reached=PhaseReached.RESEARCHING,
                outcome=StopFamily.NOT_YET_VERIFIED,
                research_status=ResearchStatus.REQUIRED_FACTS_UNVERIFIED,
            )
        )
        for _ in range(2):
            store.record_digest("unknown-city-request", digest, occurred_at=NOW)
            assert store.flush_counters(occurred_at=NOW)
    rows = coverage_rows(path)
    assert [row.milestone for row in rows] == [
        CounterMilestone.REQUEST_RECEIVED,
        CounterMilestone.REQUIRED_FACTS_UNVERIFIED,
    ]
    assert rows[0].issuer_token == "unknown"
    assert all(row.jurisdiction_exclusion is None for row in rows)


def test_supported_service_denominator_excludes_only_genuine_jurisdiction_exits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        cases = (
            ("pending", CounterKey()),
            (
                "unknown-city",
                CounterKey(
                    milestone=CounterMilestone.REQUIRED_FACTS_UNVERIFIED,
                    outcome=StopFamily.NOT_YET_VERIFIED,
                ),
            ),
            (
                "excluded-jurisdiction",
                CounterKey(
                    milestone=CounterMilestone.GENUINELY_UNSUPPORTED,
                    outcome=StopFamily.BUSINESS_EXCLUSION,
                    jurisdiction_exclusion=JurisdictionExclusion.BUSINESS_EXCLUSION,
                ),
            ),
            (
                "private-operator",
                CounterKey(
                    milestone=CounterMilestone.OTHER_EXIT,
                    outcome=StopFamily.BUSINESS_EXCLUSION,
                ),
            ),
            (
                "passed",
                CounterKey(milestone=CounterMilestone.TIER1_PASSED, outcome="SERVICEABLE"),
            ),
        )
        for request, key in cases:
            for _ in range(2):
                store.record_counter(request, key, occurred_at=NOW)
        assert store.flush_counters(occurred_at=NOW)
    counts = Counter(row.milestone for row in coverage_rows(path))
    assert counts == {
        CounterMilestone.REQUEST_RECEIVED: 5,
        CounterMilestone.GENUINELY_UNSUPPORTED: 1,
        CounterMilestone.REQUIRED_FACTS_UNVERIFIED: 1,
        CounterMilestone.TIER1_PASSED: 1,
        CounterMilestone.OTHER_EXIT: 1,
    }
    denominator = (
        counts[CounterMilestone.REQUEST_RECEIVED] - counts[CounterMilestone.GENUINELY_UNSUPPORTED]
    )
    assert denominator == 4
    assert counts[CounterMilestone.TIER1_PASSED] / denominator == 0.25


def test_lob_acceptance_counts_once_under_jobs_remail_and_delivery(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        store.record_counter(
            "workspace",
            CounterKey("CA_sf_gov", "CA", "parking", "initial_review"),
            occurred_at=NOW,
        )
        case = prepared(store)
        client = FakeLobClient(
            [LobResponse(None), LobResponse(200, "lob-1", evidence=LetterEvidence.PRODUCTION)]
        )
        cursor = step(store, client, case)
        assert store.flush_counters(occurred_at=NOW)
        assert CounterMilestone.APPEAL_COMPLETED not in {
            row.milestone for row in coverage_rows(path)
        }
        cursor = step(store, client, case, cursor, cursor.next_at, ("capture-route", "capture"))
        for _ in range(2):
            assert step(store, client, case, cursor) == cursor
        current = store.get_case(case.case_token)
        assert current is not None
        current = letter(store, current, cs.ReturnConfirmed("undeliverable"))
        current = letter(store, current, cs.RemailRequested())
        current = letter(store, current, cs.SubmitConfirmed("lob-remail"))
        letter(store, current, cs.DeliveryProofStored("proof"))
        assert store.flush_counters(occurred_at=NOW)
    rows = coverage_rows(path)
    assert Counter(row.milestone for row in rows) == {
        CounterMilestone.REQUEST_RECEIVED: 1,
        CounterMilestone.TIER1_PASSED: 1,
        CounterMilestone.APPEAL_COMPLETED: 1,
    }
    assert all(row.issuer_token == "CA_sf_gov" for row in rows)
    assert all(row.stage_token == "initial_review" for row in rows)


def test_counter_failure_does_not_block_acceptance_and_delivery_can_replay(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        with sqlite3.connect(path) as database:
            database.execute(
                "CREATE TRIGGER unavailable_counters BEFORE INSERT ON pb_coverage_counters "
                "BEGIN SELECT RAISE(FAIL, 'analytics unavailable'); END"
            )
        assert not store.flush_counters(occurred_at=NOW)
        client = FakeLobClient([LobResponse(200, "lob-1", evidence=LetterEvidence.PRODUCTION)])
        cursor = step(store, client, case, effects=("capture-route", "capture"))
        assert cursor.outcome == Accepted("lob-1")
        assert not store.flush_counters(occurred_at=NOW)
    assert coverage_rows(path) == ()
    with sqlite3.connect(path) as database:
        database.execute("DROP TRIGGER unavailable_counters")
    with SqliteTransactionStore(path) as store:
        assert store.flush_counters(occurred_at=NOW)
    assert len(coverage_rows(path)) == 3
    with sqlite3.connect(path) as database:
        # Simulate delivery succeeding before the dispatcher records its acknowledgement.
        database.execute(
            "UPDATE pb_pending_effects SET outcome=NULL, completed_at=NULL WHERE machine='counter'"
        )
    with SqliteTransactionStore(path) as store:
        assert store.flush_counters(occurred_at=NOW)
        assert store.flush_counters(occurred_at=NOW)
    assert len(coverage_rows(path)) == 3


def test_transaction_role_needs_only_analytics_insert_never_select_update_delete(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cases.sqlite"
    denied: list[int] = []

    def insert_only(
        action: int, table: str | None, column: str | None, db: str | None, source: str | None
    ) -> int:
        if table == "pb_coverage_counters" and action in {
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
        }:
            denied.append(action)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with SqliteTransactionStore(path) as store:
        store._require_open().set_authorizer(insert_only)
        case = prepared(store)
        client = FakeLobClient([LobResponse(200, "lob-1", evidence=LetterEvidence.PRODUCTION)])
        cursor = step(store, client, case, effects=("capture-route", "capture"))
        assert cursor.outcome == Accepted("lob-1")
        assert store.flush_counters(occurred_at=NOW)
        assert store.flush_counters(occurred_at=NOW)
    assert denied == []
    assert len(coverage_rows(path)) == 3


def test_analytics_contains_no_request_tokens_customer_data_or_exact_dates(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        store.record_counter("private-request-anchor", CounterKey(), occurred_at=NOW)
        store.save_workspace("private-request-anchor", WORKSPACE, NOW)
        store.start_case("private-request-anchor", "private-auth-anchor", NOW, EXPIRES)
        assert store.flush_counters(occurred_at=NOW)
    with sqlite3.connect(path) as database:
        text = json.dumps(database.execute("SELECT * FROM pb_coverage_counters").fetchall())
    for private in (
        "private-request-anchor",
        "private-auth-anchor",
        "meter",
        "citation",
        "2026-10",
    ):
        assert private not in text


def test_authorization_retries_do_not_create_additional_request_counts(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        store.save_workspace("workspace", Workspace(), NOW)
        first = store.start_case("workspace", "auth-1", NOW, EXPIRES)
        second = store.start_case("workspace", "auth-2", NOW, EXPIRES)
        assert first.case_token != second.case_token
        assert store.flush_counters(occurred_at=NOW)
    assert len(coverage_rows(path)) == 1


def test_external_counter_input_cannot_claim_completion(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        with pytest.raises(ValueError, match="confirmed Lob acceptance"):
            store.record_counter(
                "request",
                CounterKey(milestone=CounterMilestone.APPEAL_COMPLETED, outcome="SERVICEABLE"),
                occurred_at=NOW,
            )
        with pytest.raises(ValueError, match="confirmed Lob acceptance"):
            store.record_digest("request", make_digest(**_kwargs()), occurred_at=NOW)


def test_tier1_failure_lifecycle_never_emits_unsupported(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        store.save_workspace("request", WORKSPACE, NOW)
        case = store.start_case("request", "auth", NOW, EXPIRES)
        stopped = letter(
            store, case, cs.Stopped(cs.StopReason.NOT_YET_VERIFIED, hard_stop=cs.HardStop.S2)
        )
        assert stopped.letter_state is CaseState.STOPPED_NO_CHARGE
        assert store.flush_counters(occurred_at=NOW)
    assert Counter(row.milestone for row in coverage_rows(path)) == {
        CounterMilestone.REQUEST_RECEIVED: 1,
        CounterMilestone.REQUIRED_FACTS_UNVERIFIED: 1,
    }


def test_version_two_outbox_migration_preserves_pending_effects(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        effects = store.effects(case.case_token)
    assert case.amount_cents > 0
    assert effects
    # The deployed v2 schema predates the v5 no-charge CHECK, so restore the exact
    # historical paid pb_cases table (same columns, FK, unique and revision checks)
    # alongside the v2 outbox before resetting the transaction version to 2.
    database = sqlite3.connect(path, isolation_level=None)
    database.executescript("""PRAGMA foreign_keys=OFF; PRAGMA legacy_alter_table=ON;
        BEGIN IMMEDIATE;
        DROP TABLE pb_access_email_calls;
        DELETE FROM pb_store_versions WHERE component='access_email';
        DROP TABLE pb_notification_access_links;
        DELETE FROM pb_store_versions WHERE component='notifications';
        ALTER TABLE pb_pending_effects RENAME TO new_outbox;
        CREATE TABLE pb_pending_effects (
            effect_id TEXT PRIMARY KEY, case_token TEXT NOT NULL,
            revision INTEGER NOT NULL, machine TEXT NOT NULL,
            effect TEXT NOT NULL, created_at TEXT NOT NULL,
            claimed_at TEXT, outcome TEXT, completed_at TEXT,
            FOREIGN KEY (case_token, revision, machine)
                REFERENCES pb_case_history(case_token, revision, machine),
            CHECK ((outcome IS NULL) = (completed_at IS NULL)),
            CHECK (completed_at IS NULL OR claimed_at IS NOT NULL)
        );
        INSERT INTO pb_pending_effects SELECT effect_id, case_token, revision, machine,
            effect, created_at, claimed_at, outcome, completed_at FROM new_outbox
            WHERE machine != 'counter';
        DROP TABLE new_outbox;
        ALTER TABLE pb_cases RENAME TO pb_cases_v5;
        CREATE TABLE pb_cases (
            case_token TEXT PRIMARY KEY,
            workspace_token TEXT NOT NULL REFERENCES pb_workspaces(workspace_token),
            authorization_ref TEXT NOT NULL UNIQUE, authorized_at TEXT NOT NULL,
            authorization_expires_at TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
            letter_state TEXT NOT NULL, payment_state TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK (attempt IN (1, 2)),
            revision INTEGER NOT NULL CHECK (revision >= 1)
        );
        INSERT INTO pb_cases SELECT * FROM pb_cases_v5;
        DROP TABLE pb_cases_v5;
        UPDATE pb_store_versions SET version=2 WHERE component='transactions';
        COMMIT;
        PRAGMA foreign_keys=ON; PRAGMA legacy_alter_table=OFF;""")
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []
    assert database.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert database.execute(
        "SELECT version FROM pb_store_versions WHERE component='transactions'"
    ).fetchall() == [(2,)]
    schema = database.execute("SELECT sql FROM sqlite_master WHERE name='pb_cases'").fetchall()
    assert len(schema) == 1
    assert "CHECK (amount_cents > 0)" in schema[0][0]
    assert "CHECK (amount_cents >= 0)" not in schema[0][0]
    assert database.execute("SELECT amount_cents FROM pb_cases").fetchall() == [
        (case.amount_cents,)
    ]
    assert database.execute(
        "SELECT COUNT(*) FROM pb_pending_effects WHERE case_token IS NOT NULL"
    ).fetchall() == [(len(effects),)]
    database.close()
    with SqliteTransactionStore(path) as store:
        assert store.get_case(case.case_token) == case
        assert store.effects(case.case_token) == effects
        assert store._require_open().execute(
            "SELECT version FROM pb_store_versions WHERE component='transactions'"
        ).fetchall() == [(5,)]
        assert store._require_open().execute("PRAGMA foreign_key_check").fetchall() == []
        assert store._require_open().execute("SELECT amount_cents FROM pb_cases").fetchall() == [
            (case.amount_cents,)
        ]
        claimed = store.claim_effect(effects[0].effect_id, NOW)
        assert claimed.claimed_at == NOW
        store.complete_effect(claimed.effect_id, "dispatched", NOW)
        store.record_counter("new-intake", CounterKey(), occurred_at=NOW)
        assert store.flush_counters(occurred_at=NOW)
    assert len(coverage_rows(path)) == 1


def test_acceptance_and_completion_outbox_effect_are_atomic(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        with sqlite3.connect(path) as database:
            database.execute(
                "CREATE TRIGGER reject_completion BEFORE INSERT ON pb_pending_effects "
                "WHEN NEW.machine='counter' AND NEW.effect='APPEAL_COMPLETED' "
                "BEGIN SELECT RAISE(FAIL, 'disk write failed'); END"
            )
        client = FakeLobClient(
            [LobResponse(200, "lob-accepted", evidence=LetterEvidence.PRODUCTION)]
        )
        with pytest.raises(sqlite3.IntegrityError, match="disk write failed"):
            step(store, client, case, effects=("capture-route", "capture"))
        assert len(client.calls) == 1
        assert store.get_case(case.case_token) == case
        cursor = store.submission_cursor(case.case_token, 1)
        assert cursor is not None
        assert cursor.owner is not None
        assert isinstance(cursor.outcome, Uncertain)
        assert store.flush_counters(occurred_at=NOW)
    assert CounterMilestone.APPEAL_COMPLETED not in {row.milestone for row in coverage_rows(path)}
