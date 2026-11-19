from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

from mail_samples import RECIPIENT, make_request, seed_approval, seed_destination
from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import (
    Accepted,
    LetterEvidence,
    LobResponse,
    MetadataLookup,
    NotSent,
    Uncertain,
    resolve_lookup,
)
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.submission_coordinator import run_submission_step
from parkingbreaker.submission_records import SubmissionCursor, decode_outcome, outcome_json
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import RevisionConflict, SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_transaction_lifecycle import letter

NOW = datetime(2026, 10, 11, tzinfo=UTC)

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"

HOLD_LOCK_SCRIPT = """\
import sys, time
from pathlib import Path
from parkingbreaker.transaction_store import SqliteTransactionStore

with SqliteTransactionStore(Path(sys.argv[1])) as store:
    with store.submission_ownership():
        Path(sys.argv[2]).write_text("ready")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if Path(sys.argv[3]).exists():
                break
            time.sleep(0.01)
        else:
            sys.exit(75)
"""

CRASH_LOCK_SCRIPT = """\
import os, sys
from pathlib import Path
from parkingbreaker.transaction_store import SqliteTransactionStore

with SqliteTransactionStore(Path(sys.argv[1])) as store:
    with store.submission_ownership():
        Path(sys.argv[2]).write_text("ready")
        os._exit(71)
"""


def spawn_lock_holder(script: str, *arguments: object) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", script, *(str(argument) for argument in arguments)],
        env={**os.environ, "PYTHONPATH": str(SOURCE_ROOT)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def wait_for_marker(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        assert process.poll() is None, "lock holder exited before acquiring ownership"
        assert time.monotonic() < deadline, "lock holder did not acquire ownership in time"
        time.sleep(0.01)


def prepared(
    store: SqliteTransactionStore, expires_at: datetime = NOW + timedelta(days=5)
) -> CaseRecord:
    store.save_workspace("workspace", Workspace(), NOW)
    case = store.start_case("workspace", "auth", NOW, expires_at)
    for event in (
        cs.ResearchComplete(),
        cs.DraftReady(),
        cs.CustomerApproved(),
        cs.SubmitRequested(),
    ):
        case = letter(store, case, event)
    seed_destination(store, case, NOW)
    store.prepare(case.case_token, 1, make_request(), NOW)
    seed_approval(store, case.case_token)
    return case


def step(
    store: SqliteTransactionStore,
    client: FakeLobClient,
    case: CaseRecord,
    cursor: SubmissionCursor | None = None,
    now: datetime = NOW,
    effects: tuple[str, ...] = (),
) -> SubmissionCursor:
    return run_submission_step(
        store,
        client,
        case.case_token,
        case.attempt,
        expected_case_revision=case.revision,
        expected_cursor_revision=None if cursor is None else cursor.revision,
        operation_id=f"call-{case.attempt}-{0 if cursor is None else cursor.revision}",
        clock=lambda: now,
        effect_ids=effects,
    )


@pytest.mark.parametrize("status", [200, 422])
def test_first_send_and_atomic_payment_routing(tmp_path: Path, status: int) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store)
        client = FakeLobClient(
            [LobResponse(status, "lob-1", "bad address", evidence=LetterEvidence.PRODUCTION)]
        )
        cursor = step(store, client, case, effects=("letter-effect", "payment-effect"))
        saved = store.get_case(case.case_token)
        assert saved is not None
        assert saved.letter_state is (
            cs.CaseState.IN_TRANSIT if status == 200 else cs.CaseState.STOPPED_NO_CHARGE
        )
        assert saved.payment_state is (
            ps.PaymentState.CAPTURE_PENDING if status == 200 else ps.PaymentState.RELEASED
        )
        assert cursor.first_sent_at == NOW
        assert cursor.sends == 1
        assert cursor.owner is None
        assert len(store.transition_history(case.case_token)) == 6
        assert len(store.effects(case.case_token)) == 3
        before = store.effects(case.case_token)
        # A replay, including an old routed pending effect, cannot apply payment twice.
        assert step(store, client, case, cursor) == cursor
        assert store.effects(case.case_token) == before
        assert len(client.calls) == 1
        with sqlite3.connect(store.path) as db:
            outcome, completed = db.execute(
                "SELECT outcome_json, completed_at FROM pb_submission_operations"
            ).fetchone()
        assert decode_outcome(outcome) == cursor.outcome
        assert completed == NOW.isoformat()


def test_uncertain_retries_identical_request_key_and_reopening(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    client = FakeLobClient(
        [
            LobResponse(None),
            LobResponse(503),
            LobResponse(200, "lob-1", evidence=LetterEvidence.PRODUCTION),
        ]
    )
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        cursor = step(store, client, case)
        assert isinstance(cursor.outcome, Uncertain)
        with pytest.raises(ValueError, match="not due"):
            step(store, client, case, cursor)
        assert len(client.calls) == 1
    with SqliteTransactionStore(path) as store:
        assert store.submission_cursor(case.case_token, 1) == cursor
        cursor = step(store, client, case, cursor, cursor.next_at)
        assert cursor.sends == 2
        cursor = step(store, client, case, cursor, cursor.next_at, ("capture-route", "capture"))
        assert cursor.outcome == Accepted("lob-1")
        assert len(client.calls) == 3
        assert all(call == client.calls[0] for call in client.calls)
        assert cursor.first_sent_at == NOW


class LookupClient(FakeLobClient):
    result: str | MetadataLookup | None = None
    failed = False

    def find_by_metadata(self, submission_token: str) -> str | MetadataLookup | None:
        self.lookups.append(submission_token)
        if self.failed:
            raise OSError("lookup unavailable")
        return self.result


@pytest.mark.parametrize("elapsed", [23, 24, 48])
@pytest.mark.parametrize("lookup", ["found", "absent", "legacy", "incomplete", "failed"])
def test_cutoff_lookup_semantics(tmp_path: Path, elapsed: int, lookup: str) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store)
        client = LookupClient()
        cursor = step(store, client, case)
        client.result = {
            "found": MetadataLookup("lob-found", evidence=LetterEvidence.PRODUCTION),
            "absent": MetadataLookup(authoritative=True),
            "legacy": None,
            "incomplete": MetadataLookup(),
            "failed": None,
        }[lookup]
        client.failed = lookup == "failed"
        terminal = lookup in {"found", "absent"}
        cursor = step(
            store,
            client,
            case,
            cursor,
            NOW + timedelta(hours=elapsed),
            ("route", "payment") if terminal else (),
        )
        assert len(client.calls) == 1
        assert len(client.lookups) == 1
        assert cursor.lookup_only
        saved = store.get_case(case.case_token)
        assert saved is not None
        if lookup == "found":
            assert cursor.outcome == Accepted("lob-found")
            assert saved.payment_state is ps.PaymentState.CAPTURE_PENDING
        elif lookup == "absent":
            assert cursor.outcome == NotSent()
            assert saved.payment_state is ps.PaymentState.RELEASED
        else:
            assert isinstance(cursor.outcome, Uncertain)
            assert saved == case
            step(store, client, case, cursor, cursor.next_at)
            assert len(client.calls) == 1
            assert len(client.lookups) == 2
            assert len(store.effects(case.case_token)) == 1


@pytest.mark.parametrize("phase", ["before_call", "after_call"])
def test_abrupt_crash_keeps_uncertain_intent_and_recovers_same_key(
    tmp_path: Path, phase: str
) -> None:
    path = tmp_path / "cases.sqlite"
    marker = tmp_path / "sent"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        frozen = store.get(case.case_token, 1)
        assert frozen is not None
    script = """
import os, sys
from pathlib import Path
from datetime import datetime
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.submission_coordinator import run_submission_step
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
class Client(FakeLobClient):
    def create_letter(self, request, key):
        if sys.argv[4] == "before_call":
            os._exit(71)
        response = super().create_letter(request, key)
        Path(sys.argv[3]).write_text(key)
        return response
class Store(SqliteTransactionStore):
    def finish_submission(self, *args, **kwargs):
        os._exit(72)
with Store(Path(sys.argv[1])) as store:
    case = store.get_case(sys.argv[2])
    now = datetime.fromisoformat(sys.argv[5])
    proven = LobResponse(200, "lob-created", evidence=LetterEvidence.PRODUCTION)
    run_submission_step(store, Client([proven]), case.case_token, 1,
        expected_case_revision=case.revision, expected_cursor_revision=None,
        operation_id="crashing-call", clock=lambda: now)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(path),
            case.case_token,
            str(marker),
            phase,
            NOW.isoformat(),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == (71 if phase == "before_call" else 72), result.stderr
    with SqliteTransactionStore(path) as store:
        cursor = store.submission_cursor(case.case_token, 1)
        assert cursor is not None
        assert cursor.owner == "crashing-call"
        assert cursor.first_sent_at == NOW
        assert isinstance(cursor.outcome, Uncertain)
        assert store.get_case(case.case_token) == case
        client = FakeLobClient(
            [LobResponse(200, "lob-created", evidence=LetterEvidence.PRODUCTION)]
        )
        if phase == "after_call":
            assert marker.read_text() == frozen.key
            client.by_key[frozen.key] = "lob-created"
            client.evidence_by_token[frozen.request.submission_token] = LetterEvidence.PRODUCTION
        cursor = step(store, client, case, cursor, cursor.next_at, ("route", "capture"))
        assert cursor.outcome == Accepted("lob-created")
        assert client.calls == [(frozen.key, frozen.request)]


def test_concurrent_coordinators_cannot_overlap_calls(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    entered, release = Event(), Event()

    class BlockingClient(FakeLobClient):
        def create_letter(self, request: object, idempotency_key: str) -> LobResponse:
            entered.set()
            assert release.wait(5)
            return LobResponse(None)

    with SqliteTransactionStore(path) as store:
        case = prepared(store)

    def first() -> SubmissionCursor:
        with SqliteTransactionStore(path) as store:
            return step(store, BlockingClient(), case)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first)
        assert entered.wait(5)
        try:
            with SqliteTransactionStore(path) as peer:
                current = peer.submission_cursor(case.case_token, 1)
                assert current is not None
                assert current.owner is not None
                client = FakeLobClient()
                with pytest.raises(RevisionConflict, match="owns"):
                    step(peer, client, case, current, current.next_at)
                assert not client.calls
        finally:
            release.set()
        assert future.result().owner is None


def test_outcome_commit_failure_is_atomic(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store)
        store._require_open().execute("""CREATE TRIGGER fail_outcome
            BEFORE UPDATE ON pb_submission_cursors WHEN NEW.owner IS NULL
            BEGIN SELECT RAISE(ABORT, 'injected crash'); END""")
        client = FakeLobClient([LobResponse(200, "lob-1", evidence=LetterEvidence.PRODUCTION)])
        with pytest.raises(sqlite3.IntegrityError, match="injected crash"):
            step(store, client, case, effects=("route", "capture"))
        assert store.get_case(case.case_token) == case
        assert len(store.transition_history(case.case_token)) == 4
        assert len(store.effects(case.case_token)) == 1
        cursor = store.submission_cursor(case.case_token, 1)
        assert cursor is not None
        assert cursor.owner is not None
        with sqlite3.connect(store.path) as db:
            assert db.execute("SELECT outcome_json FROM pb_submission_operations").fetchone() == (
                None,
            )
        store._require_open().execute("DROP TRIGGER fail_outcome")
        assert step(
            store, client, case, cursor, cursor.next_at, ("route", "capture")
        ).outcome == Accepted("lob-1")


def test_prepared_bytes_cannot_authorize_or_change_attempt(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        store.save_workspace("workspace", Workspace(), NOW)
        case = store.start_case("workspace", "auth", NOW, NOW + timedelta(days=5))
        store.prepare(case.case_token, 1, make_request(), NOW)
        client = FakeLobClient()
        with pytest.raises(ValueError, match="SUBMITTING"):
            step(store, client, case)
        for event in (
            cs.ResearchComplete(),
            cs.DraftReady(),
            cs.CustomerApproved(),
            cs.SubmitRequested(),
        ):
            case = letter(store, case, event)
        store.prepare(case.case_token, 2, make_request(token="remail-token"), NOW)
        with pytest.raises(ValueError, match="matching"):
            run_submission_step(
                store,
                client,
                case.case_token,
                2,
                expected_case_revision=case.revision,
                expected_cursor_revision=None,
                operation_id="wrong-attempt",
                clock=lambda: NOW,
            )
        assert not client.calls
        assert store.submission_cursor(case.case_token, 1) is None


@pytest.mark.parametrize("status", [200, 422])
def test_only_existing_approved_remail_and_no_second_payment(tmp_path: Path, status: int) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store)
        client = FakeLobClient(
            [
                LobResponse(200, "lob-1", evidence=LetterEvidence.PRODUCTION),
                LobResponse(status, "lob-2", evidence=LetterEvidence.PRODUCTION),
            ]
        )
        step(store, client, case, effects=("route", "capture"))
        saved_case = store.get_case(case.case_token)
        assert saved_case is not None
        case = saved_case
        case = store.apply_payment_event(
            case.case_token, ps.CaptureSucceeded("charge"), case.revision, occurred_at=NOW
        )
        case = letter(store, case, cs.ReturnConfirmed("returned"))
        case = letter(store, case, cs.RemailRequested())
        assert case.attempt == 2
        # The free re-mail needs a persisted SYSTEM_VERIFIED destination that differs
        # from the returned letter's address.
        corrected = replace(RECIPIENT, line1="987 Corrected Avenue")
        seed_destination(store, case, NOW, corrected)
        store.prepare(
            case.case_token,
            2,
            replace(
                make_request(pdf=b"%PDF-1.4\ncorrected synthetic packet", token="remail-token"),
                recipient=corrected,
            ),
            NOW,
        )
        seed_approval(store, case.case_token, 2)
        before = store.effects(case.case_token)
        cursor = step(store, client, case)
        assert (cursor.outcome == Accepted("lob-2")) == (status == 200)
        assert client.calls[0][0] != client.calls[1][0]
        assert store.effects(case.case_token) == before
        saved = store.get_case(case.case_token)
        assert saved is not None
        assert saved.payment_state is ps.PaymentState.CAPTURED


@pytest.mark.parametrize("damage", ["unknown_version", "missing_table", "malformed_cursor"])
def test_schema_and_cursor_fail_closed(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        step(store, FakeLobClient(), case)
    with sqlite3.connect(path) as db:
        if damage == "unknown_version":
            db.execute("UPDATE pb_store_versions SET version=99 WHERE component='submission'")
        elif damage == "missing_table":
            db.execute("DROP TABLE pb_submission_operations")
        else:
            db.execute("UPDATE pb_submission_cursors SET outcome_json='{}'")
    with pytest.raises(StorageCorruption), SqliteTransactionStore(path) as store:
        store.submission_cursor(case.case_token, 1)


def test_additive_schema_migration_preserves_lifecycle_and_frozen_request(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        frozen = store.get(case.case_token, 1)
        history, effects = store.transition_history(case.case_token), store.effects(case.case_token)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE pb_submission_cursors")
        db.execute("DROP TABLE pb_submission_operations")
        db.execute("DELETE FROM pb_store_versions WHERE component='submission'")
    with SqliteTransactionStore(path) as store:
        assert store.get_case(case.case_token) == case
        assert store.get(case.case_token, 1) == frozen
        assert store.transition_history(case.case_token) == history
        assert store.effects(case.case_token) == effects
        assert store.submission_cursor(case.case_token, 1) is None
        step(store, FakeLobClient(), case)


@pytest.mark.parametrize(
    "text", ["{}", '{"kind":"accepted","letter_id":""}', '{"kind":"not_sent","extra":true}', "null"]
)
def test_outcome_codec_refuses_invalid_data(text: str) -> None:
    with pytest.raises(ValueError, match=r".+"):
        decode_outcome(text)


def test_lookup_classifier_and_codec() -> None:
    assert resolve_lookup(None) == Uncertain(
        "legacy lookup did not establish authoritative absence"
    )
    assert isinstance(resolve_lookup(MetadataLookup("")), Uncertain)
    assert decode_outcome(outcome_json(NotSent())) == NotSent()


def test_first_send_time_is_visible_before_client_call(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"

    class ObservingClient(FakeLobClient):
        def create_letter(self, request: object, idempotency_key: str) -> LobResponse:
            with sqlite3.connect(path) as db:
                first, sends, owner = db.execute(
                    "SELECT first_sent_at, sends, owner FROM pb_submission_cursors"
                ).fetchone()
                assert first == NOW.isoformat()
                assert sends == 1
                assert owner is not None
                assert db.execute("SELECT COUNT(*) FROM pb_submission_operations").fetchone() == (
                    1,
                )
            return LobResponse(None)

    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        step(store, ObservingClient(), case)


def test_worker_delayed_before_create_switches_to_lookup(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store)
        client = FakeLobClient()
        cursor = step(store, client, case)
        times = iter([cursor.next_at, NOW + timedelta(hours=24), NOW + timedelta(hours=24)])
        cursor = run_submission_step(
            store,
            client,
            case.case_token,
            1,
            expected_case_revision=case.revision,
            expected_cursor_revision=cursor.revision,
            operation_id="delayed",
            clock=lambda: next(times),
        )
        assert len(client.calls) == 1
        assert isinstance(cursor.outcome, Uncertain)
        assert cursor.lookup_only
        # The fake's complete in-memory inventory can establish authoritative absence.
        cursor = step(store, client, case, cursor, cursor.next_at, ("route", "release"))
        assert cursor.outcome == NotSent()
        assert len(client.calls) == 1


def test_orphaned_intent_after_expiry_never_creates_again(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        with store.submission_ownership():
            cursor, kind = store.claim_submission(
                case.case_token,
                1,
                case.revision,
                None,
                now=NOW,
                operation_id="orphan",
            )
            assert kind == "create"
    with SqliteTransactionStore(path) as store:
        client = LookupClient()
        client.result = MetadataLookup("lob-found", evidence=LetterEvidence.PRODUCTION)
        cursor = step(store, client, case, cursor, NOW + timedelta(hours=48), ("route", "capture"))
        assert cursor.outcome == Accepted("lob-found")
        assert not client.calls
        assert len(client.lookups) == 1


@pytest.mark.parametrize("conflict", ["case", "cursor", "id"])
def test_optimistic_checks_and_reused_operation_id_prevent_calls(
    tmp_path: Path, conflict: str
) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store)
        client = FakeLobClient()
        cursor = step(store, client, case)
        with pytest.raises((RevisionConflict, sqlite3.IntegrityError)):
            run_submission_step(
                store,
                client,
                case.case_token,
                1,
                expected_case_revision=case.revision - (1 if conflict == "case" else 0),
                expected_cursor_revision=cursor.revision - (1 if conflict == "cursor" else 0),
                operation_id="call-1-0" if conflict == "id" else "next",
                clock=lambda: cursor.next_at,
            )
        assert len(client.calls) == 1
        assert store.submission_cursor(case.case_token, 1) == cursor


@pytest.mark.parametrize("status", [408, 409, 425, 429, 500])
def test_nondefinitive_response_cannot_release_hold(tmp_path: Path, status: int) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store)
        cursor = step(store, FakeLobClient([LobResponse(status)]), case)
        assert isinstance(cursor.outcome, Uncertain)
        assert store.get_case(case.case_token) == case
        assert len(store.effects(case.case_token)) == 1


def test_create_exception_is_durably_uncertain(tmp_path: Path) -> None:
    class FailingClient(FakeLobClient):
        def create_letter(self, request: object, idempotency_key: str) -> LobResponse:
            raise OSError("sensitive provider text")

    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store)
        cursor = step(store, FailingClient(), case)
        assert cursor.outcome == Uncertain("provider call failed")
        assert cursor.owner is None
        assert store.get_case(case.case_token) == case


@pytest.mark.parametrize(
    ("column", "value"),
    [("sends", 8), ("revision", 8), ("first_sent_at", "2026-10-12T00:00:00+00:00")],
)
def test_cursor_history_mismatch_prevents_send(tmp_path: Path, column: str, value: object) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store)
        cursor = step(store, FakeLobClient(), case)
        store._require_open().execute(f"UPDATE pb_submission_cursors SET {column}=?", (value,))
        with pytest.raises(StorageCorruption):
            step(store, FakeLobClient(), case, cursor, cursor.next_at)


def test_cursor_history_read_uses_one_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        old = step(store, FakeLobClient(), case)
        connection = store._require_open()

        class InterleavedWriter:
            fired = False

            @property
            def in_transaction(self) -> bool:
                return connection.in_transaction

            def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
                cursor = connection.execute(sql, parameters)
                if sql.startswith("SELECT * FROM pb_submission_cursors") and not self.fired:
                    self.fired = True
                    with SqliteTransactionStore(path) as peer:
                        step(
                            peer,
                            FakeLobClient(
                                [LobResponse(200, "lob-1", evidence=LetterEvidence.PRODUCTION)]
                            ),
                            case,
                            old,
                            old.next_at,
                            ("route", "capture"),
                        )
                return cursor

            def close(self) -> None:
                connection.close()

        store._database = InterleavedWriter()  # type: ignore[assignment]
        assert store.submission_cursor(case.case_token, 1) == old
        current = store.submission_cursor(case.case_token, 1)
        assert current is not None
        assert current.outcome == Accepted("lob-1")


def test_same_store_reentrancy_is_refused(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        with (
            store.submission_ownership(),
            pytest.raises(RevisionConflict, match="already active"),
            store.submission_ownership(),
        ):
            pass
        with store.submission_ownership():
            pass


def test_normal_and_exception_release_unblock_peers(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    store = SqliteTransactionStore(path)
    try:
        with store.submission_ownership():
            pass
        with (
            pytest.raises(RuntimeError, match="injected"),
            store.submission_ownership(),
        ):
            raise RuntimeError("injected")
        with SqliteTransactionStore(path) as peer, peer.submission_ownership():
            pass
        with store.submission_ownership():
            pass
    finally:
        store.close()


def test_lock_stays_held_across_cursor_commits(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        database = store._require_open()
        database.execute("CREATE TABLE witness(value INTEGER)")
        with store.submission_ownership():
            database.execute("BEGIN")
            database.execute("INSERT INTO witness VALUES (1)")
            database.execute("COMMIT")
            with (
                SqliteTransactionStore(path) as peer,
                pytest.raises(RevisionConflict, match="owns"),
                peer.submission_ownership(),
            ):
                pass
        with SqliteTransactionStore(path) as peer, peer.submission_ownership():
            pass


def test_cross_process_owner_excludes_and_release_unblocks(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path):
        pass
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    process = spawn_lock_holder(HOLD_LOCK_SCRIPT, path, ready, release)
    try:
        wait_for_marker(ready, process)
        with (
            SqliteTransactionStore(path) as store,
            pytest.raises(RevisionConflict, match="owns"),
            store.submission_ownership(),
        ):
            pass
        release.write_text("go")
        assert process.wait(10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)
    with SqliteTransactionStore(path) as store, store.submission_ownership():
        pass


def test_crashed_cross_process_owner_releases_lock(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path):
        pass
    ready = tmp_path / "ready"
    process = spawn_lock_holder(CRASH_LOCK_SCRIPT, path, ready)
    try:
        wait_for_marker(ready, process)
        assert process.wait(10) == 71
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)
    assert ready.read_text() == "ready"
    with SqliteTransactionStore(path) as store, store.submission_ownership():
        pass


def test_relative_parent_and_symlink_parent_aliases_share_one_lock(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(data, target_is_directory=True)
    real = SqliteTransactionStore(data / "cases.sqlite")
    relative = SqliteTransactionStore(data / ".." / "data" / "cases.sqlite")
    through_symlink = SqliteTransactionStore(alias / "cases.sqlite")
    try:
        with real.submission_ownership():
            for peer in (relative, through_symlink):
                with pytest.raises(RevisionConflict, match="owns"), peer.submission_ownership():
                    pass
        with relative.submission_ownership():
            pass
    finally:
        for store in (real, relative, through_symlink):
            store.close()


def test_symlink_database_path_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    with SqliteTransactionStore(target):
        pass
    link = tmp_path / "link.sqlite"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        SqliteTransactionStore(link)


def test_hard_linked_database_path_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    with SqliteTransactionStore(target):
        pass
    alias = tmp_path / "alias.sqlite"
    os.link(target, alias)
    with pytest.raises(ValueError, match="hard link"):
        SqliteTransactionStore(alias)


def test_non_regular_database_path_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular file"):
        SqliteTransactionStore(path)


def test_database_replacement_before_ownership_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    store = SqliteTransactionStore(path)
    try:
        path.rename(tmp_path / "moved.sqlite")
        path.write_bytes(b"")
        with pytest.raises(RevisionConflict, match="replaced"), store.submission_ownership():
            pass
    finally:
        store.close()


def test_directory_replacement_before_ownership_fails_closed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    store = SqliteTransactionStore(data / "cases.sqlite")
    try:
        data.rename(tmp_path / "old")
        data.mkdir()
        with pytest.raises(RevisionConflict, match="replaced"), store.submission_ownership():
            pass
    finally:
        store.close()
