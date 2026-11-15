from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.payment_coordinator import run_payment_step
from parkingbreaker.payment_fakes import FakePaymentClient, full_outcome
from parkingbreaker.payment_records import PaymentCursor, PaymentOutcome, cursor_json, decode_cursor
from parkingbreaker.prices import CERTIFIED_CENTS
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import (
    EffectConflict,
    RevisionConflict,
    SqliteTransactionStore,
)
from parkingbreaker.workspace import Workspace
from test_transaction_lifecycle import NOW, letter, mailed, start


def step(
    store: SqliteTransactionStore,
    client: FakePaymentClient,
    case: CaseRecord,
    cursor: PaymentCursor | None = None,
    *,
    writeoff: bool = False,
) -> PaymentCursor:
    current = store.get_case(case.case_token)
    assert current is not None
    return run_payment_step(
        store,
        client,
        case.case_token,
        expected_case_revision=current.revision,
        expected_cursor_revision=None if cursor is None else cursor.revision,
        operation_id=f"payment-{0 if cursor is None else cursor.revision}",
        key="capture-original",
        clock=lambda: NOW if cursor is None else cursor.next_at,
        retry_effect_id=f"retry-{0 if cursor is None else cursor.revision}",
        writeoff_effect_ids=("link", "log") if writeoff else (),
    )


def test_accept_capture_replay_reopen_and_free_remail(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    client = FakePaymentClient()
    with SqliteTransactionStore(path) as store:
        case = mailed(store)
        before = store.transition_history(case.case_token)
        cursor = step(store, client, case)
        assert cursor.call_kind == "retrieve"
        assert store.transition_history(case.case_token) == before
        route = next(
            e
            for e in store.effects(case.case_token)
            if e.machine == "letter" and e.effect == cs.Effect.CAPTURE_PAYMENT
        )
        assert route.outcome == "routed"
        cursor = step(store, client, case, cursor)
        assert cursor.done
        assert client.captures == 1
        saved = store.get_case(case.case_token)
        assert saved is not None
        assert saved.payment_state is ps.PaymentState.CAPTURED
        assert saved.letter_state == case.letter_state
        assert step(store, client, case, cursor) == cursor
        assert len(client.calls) == 2
        history = store.transition_history(case.case_token)
        effects = store.effects(case.case_token)
    with SqliteTransactionStore(path) as store:
        assert store.payment_cursor(case.case_token) == cursor
        assert store.transition_history(case.case_token) == history
        assert store.effects(case.case_token) == effects
        saved = letter(store, saved, cs.ReturnConfirmed("address"))
        saved = letter(store, saved, cs.RemailRequested())
        saved = letter(store, saved, cs.SubmitConfirmed("lob-2"))
        assert step(store, client, saved, cursor) == cursor
        assert client.captures == 1
        assert (
            sum(
                e.machine == "payment" and e.effect == ps.PaymentEffect.CAPTURE_PAYMENT
                for e in store.effects(case.case_token)
            )
            == 1
        )


def test_stop_and_external_release_recovered(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = letter(store, start(store), cs.WindowExpired())
        client = FakePaymentClient()
        history = store.transition_history(case.case_token)
        cursor = step(store, client, case)
        assert case.payment_state is ps.PaymentState.RELEASED
        cursor = step(store, client, case, cursor)
        assert cursor.done
        assert client.releases == 1
        assert store.transition_history(case.case_token) == history
        assert not any(e.machine == "payment" for e in store.pending_effects(case.case_token))


def test_transient_retry_timing_and_success(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = mailed(store)
        client = FakePaymentClient()
        client.responses = [
            PaymentOutcome("transient", "auth"),
            PaymentOutcome("transient", "auth"),
        ]
        cursor = step(store, client, case)
        cursor = step(store, client, case, cursor)
        assert cursor.failed_attempts == 1
        assert cursor.next_at == NOW + timedelta(minutes=15)
        current = store.get_case(case.case_token)
        assert current is not None
        assert current.payment_state is ps.PaymentState.CAPTURE_RETRYING
        before = store.effects(case.case_token)
        with pytest.raises(ValueError, match="not due"):
            run_payment_step(
                store,
                client,
                case.case_token,
                expected_case_revision=current.revision,
                expected_cursor_revision=cursor.revision,
                operation_id="early",
                key=cursor.key,
                clock=lambda: NOW,
            )
        assert store.effects(case.case_token) == before
        cursor = step(store, client, case, cursor)  # due RetryCapture + retrieve
        assert store.transition_history(case.case_token)[-1].event_name == "RetryCapture"
        cursor = step(store, client, case, cursor)
        assert cursor.failed_attempts == 2
        assert cursor.next_at == NOW + timedelta(minutes=45)
        cursor = step(store, client, case, cursor)
        cursor = step(store, client, case, cursor)
        assert cursor.done
        assert client.captures == 1
        assert len({call[3] for call in client.calls if call[0] == "capture"}) == 3
        assert all(call[1:3] == ("auth", 2999) for call in client.calls)


@pytest.mark.parametrize("status", ["declined", "expired", "canceled"])
def test_definitive_writeoff_exact_effects_without_letter(tmp_path: Path, status: str) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = mailed(store)
        client = FakePaymentClient()
        client.responses = [PaymentOutcome(status, "auth")]
        cursor = step(store, client, case)
        cursor = step(store, client, case, cursor, writeoff=True)
        assert cursor.done
        current = store.get_case(case.case_token)
        assert current is not None
        assert current.payment_state is ps.PaymentState.WRITTEN_OFF
        assert current.letter_state == case.letter_state
        pending = [e for e in store.pending_effects(case.case_token) if e.machine == "payment"]
        assert [e.effect for e in pending] == [
            ps.PaymentEffect.SEND_PAYMENT_LINK,
            ps.PaymentEffect.LOG_WRITE_OFF,
        ]
        assert all(e.claimed_at is None for e in pending)
        before = store.effects(case.case_token)
        assert step(store, client, case, cursor) == cursor
        assert store.effects(case.case_token) == before
        assert len(client.calls) == 2  # PaymentClient exposes no Lob/notification call.


@pytest.mark.parametrize("release", [False, True])
@pytest.mark.parametrize("phase", ["before_call", "after_call", "before_commit"])
def test_crash_recovers_by_original_object_retrieval(
    tmp_path: Path, phase: str, release: bool
) -> None:
    path = tmp_path / "cases.sqlite"
    client = FakePaymentClient()
    with SqliteTransactionStore(path) as store:
        case = letter(store, start(store), cs.WindowExpired()) if release else mailed(store)
        cursor = step(store, client, case)
        current = store.get_case(case.case_token)
        assert current is not None

        # BaseException models a process that never reports its outcome.
        class Crashed(BaseException):
            pass

        class CrashClient(FakePaymentClient):
            def capture(
                self, authorization_ref: str, amount_cents: int, key: str, *, final_capture: bool
            ) -> PaymentOutcome:
                if phase == "before_call":
                    raise Crashed()
                result = client.capture(
                    authorization_ref, amount_cents, key, final_capture=final_capture
                )
                if phase == "after_call":
                    raise Crashed()
                return result

            def release(self, authorization_ref: str, key: str) -> PaymentOutcome:
                if phase == "before_call":
                    raise Crashed()
                result = client.release(authorization_ref, key)
                if phase == "after_call":
                    raise Crashed()
                return result

        connection = store._require_open()

        class FailCommit:
            @property
            def in_transaction(self) -> bool:
                return connection.in_transaction

            commits = 0

            def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
                if sql == "COMMIT":
                    self.commits += 1
                    # snapshot read then claim commit then outcome commit
                    if self.commits == 3:
                        raise RuntimeError("crash before outcome commit")
                return connection.execute(sql, parameters)

            def close(self) -> None:
                connection.close()

        if phase == "before_commit":
            store._database = FailCommit()  # type: ignore[assignment]
        with pytest.raises(RuntimeError if phase == "before_commit" else Crashed):
            step(store, CrashClient(), case, cursor)
    with SqliteTransactionStore(path) as store:
        interrupted = store.payment_cursor(case.case_token)
        assert interrupted is not None
        assert interrupted.owner is not None
        assert not interrupted.done
        recovered = step(store, client, case, interrupted)
        assert recovered.call_kind == "retrieve"
        if phase == "before_call":
            recovered = step(store, client, case, recovered)
        assert recovered.done
        assert client.captures == (0 if release else 1)
        assert client.releases == (1 if release else 0)


@pytest.mark.parametrize("lookup", ["uncertain", "exception", "mismatch"])
def test_retrieve_uncertainty_preserved_past_expiry(tmp_path: Path, lookup: str) -> None:
    class Client(FakePaymentClient):
        def retrieve(self, authorization_ref: str) -> PaymentOutcome:
            if lookup == "exception":
                raise OSError("secret provider error")
            return PaymentOutcome(
                "uncertain", "wrong" if lookup == "mismatch" else authorization_ref
            )

    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = mailed(store)
        client = Client()
        if lookup == "mismatch":
            with pytest.raises(ValueError, match="mismatch"):
                step(store, client, case)
            cursor = store.payment_cursor(case.case_token)
            assert cursor is not None
            assert cursor.owner is not None
        else:
            cursor = step(store, client, case)
        before = store.get_case(case.case_token)
        cursor = run_payment_step(
            store,
            client if lookup != "mismatch" else FakePaymentClient(),
            case.case_token,
            expected_case_revision=case.revision,
            expected_cursor_revision=cursor.revision,
            operation_id="after-expiry",
            key=cursor.key,
            clock=lambda: NOW + timedelta(days=8),
        )
        assert not cursor.done
        assert store.get_case(case.case_token) == before
        assert not any(
            e.effect in {ps.PaymentEffect.SEND_PAYMENT_LINK, ps.PaymentEffect.LOG_WRITE_OFF}
            for e in store.effects(case.case_token)
        )
        assert client.captures == 0


def test_live_lock_cannot_be_stolen_and_stale_writes_refused(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    entered, proceed = Event(), Event()

    class BlockingClient(FakePaymentClient):
        def retrieve(self, authorization_ref: str) -> PaymentOutcome:
            entered.set()
            assert proceed.wait(5)
            return super().retrieve(authorization_ref)

    with SqliteTransactionStore(path) as store:
        case = mailed(store)

    def dispatch() -> PaymentCursor:
        with SqliteTransactionStore(path) as store:
            return step(store, BlockingClient(), case)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(dispatch)
        assert entered.wait(5)
        try:
            with SqliteTransactionStore(path) as store:
                cursor = store.payment_cursor(case.case_token)
                assert cursor is not None
                with pytest.raises(RevisionConflict, match="owns"):
                    step(store, FakePaymentClient(), case, cursor)
        finally:
            proceed.set()
        cursor = future.result(timeout=5)
    with SqliteTransactionStore(path) as store:
        before = store.payment_cursor(case.case_token)
        client = FakePaymentClient()
        with pytest.raises(RevisionConflict, match="stale"):
            run_payment_step(
                store,
                client,
                case.case_token,
                expected_case_revision=1,
                expected_cursor_revision=cursor.revision,
                operation_id="stale",
                key=cursor.key,
                clock=lambda: NOW,
            )
        assert store.payment_cursor(case.case_token) == before
        assert client.calls == []


def test_exact_completion_and_public_claim_semantics(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = mailed(store)
        cursor = step(store, FakePaymentClient(), case)
        with store.submission_ownership():
            claimed = store.claim_payment(
                case.case_token,
                case.revision,
                cursor.revision,
                now=NOW,
                operation_id="manual",
                key=cursor.key,
                retry_effect_id="unused",
            )
            outcome = full_outcome(PaymentOutcome("succeeded", "auth", 2999, "charge-1"))
            saved = store.finish_payment(claimed, outcome, now=NOW)
            assert store.finish_payment(claimed, outcome, now=NOW + timedelta(days=1)) == saved
            with pytest.raises(EffectConflict, match="different"):
                store.finish_payment(claimed, replace(outcome, charge_ref="charge-2"), now=NOW)
        with pytest.raises(EffectConflict):
            store.claim_effect(cursor.effect_id, NOW)
        assert (
            store.complete_effect(
                cursor.effect_id,
                next(
                    e.outcome
                    for e in store.effects(case.case_token)
                    if e.effect_id == cursor.effect_id
                )
                or "",
                NOW,
            )
            is not None
        )


@pytest.mark.parametrize("damage", ["history", "amount", "ref", "cursor", "operation", "schema"])
def test_corruption_fails_closed(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = mailed(store)
        cursor = step(store, FakePaymentClient(), case)
    with sqlite3.connect(path) as db:
        if damage == "history":
            db.execute("DROP TRIGGER pb_case_history_no_delete")  # simulate file tampering
            db.execute("DELETE FROM pb_case_history WHERE revision=2")
        elif damage == "amount":
            db.execute("UPDATE pb_cases SET amount_cents=999")
        elif damage == "ref":
            db.execute("UPDATE pb_cases SET authorization_ref='other'")
        elif damage == "cursor":
            db.execute(
                "UPDATE pb_payment_cursor SET payload=?",
                (cursor_json(replace(cursor, failed_attempts=1)),),
            )
        elif damage == "operation":
            db.execute("DELETE FROM pb_payment_operations")
        else:
            db.execute("ALTER TABLE pb_payment_cursor ADD COLUMN extra TEXT")
    with pytest.raises(StorageCorruption), SqliteTransactionStore(path) as store:
        step(store, FakePaymentClient(), case, cursor)


def test_additive_schema_migration(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = mailed(store)
        before = store.effects(case.case_token)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE pb_payment_cursor")
        db.execute("DROP TABLE pb_payment_operations")
        db.execute("DELETE FROM pb_store_versions WHERE component='payment'")
    with SqliteTransactionStore(path) as store:
        assert store.get_case(case.case_token) == case
        assert store.effects(case.case_token) == before
        assert store.payment_cursor(case.case_token) is None
        cursor = step(store, FakePaymentClient(), case)
        assert decode_cursor(cursor_json(cursor)) == cursor


@pytest.mark.parametrize("phase", ["before_call", "after_call", "before_commit"])
def test_abrupt_process_exit_releases_lock_and_never_duplicates_charge(
    tmp_path: Path, phase: str
) -> None:
    import subprocess
    import sys

    path = tmp_path / "cases.sqlite"
    marker = tmp_path / "original-charge"
    with SqliteTransactionStore(path) as store:
        case = mailed(store)
        step(store, FakePaymentClient(), case)
    script = """
import os, sys
from pathlib import Path
from datetime import datetime
from parkingbreaker.payment_coordinator import run_payment_step
from parkingbreaker.payment_fakes import FakePaymentClient, full_outcome
from parkingbreaker.transaction_store import SqliteTransactionStore
class Client(FakePaymentClient):
    def capture(self, ref, amount, key, *, final_capture):
        if sys.argv[4] == 'before_call':
            os._exit(71)
        result = super().capture(ref, amount, key, final_capture=final_capture)
        Path(sys.argv[3]).write_text(result.charge_ref)
        if sys.argv[4] == 'after_call':
            os._exit(71)
        return result
with SqliteTransactionStore(Path(sys.argv[1])) as store:
    cursor = store.payment_cursor(sys.argv[2])
    connection = store._require_open()
    class ExitCommit:
        commits = 0
        @property
        def in_transaction(self):
            return connection.in_transaction
        def execute(self, sql, parameters=()):
            if sql == 'COMMIT':
                self.commits += 1
                if self.commits == 3:
                    os._exit(71)
            return connection.execute(sql, parameters)
        def close(self):
            connection.close()
    if sys.argv[4] == 'before_commit':
        store._database = ExitCommit()
    run_payment_step(store, Client(), sys.argv[2],
        expected_case_revision=cursor.case_revision,
        expected_cursor_revision=cursor.revision, operation_id='crashed-process',
        key=cursor.key, clock=lambda: cursor.next_at)
"""
    crashed = subprocess.run(
        [sys.executable, "-c", script, str(path), case.case_token, str(marker), phase],
        check=False,
        capture_output=True,
        timeout=10,
    )
    assert crashed.returncode == 71, crashed.stderr.decode()
    client = FakePaymentClient()
    if marker.exists():
        client.object = full_outcome(PaymentOutcome("succeeded", "auth", 2999, marker.read_text()))
    with SqliteTransactionStore(path) as store:
        interrupted = store.payment_cursor(case.case_token)
        assert interrupted is not None
        assert interrupted.owner == "crashed-process"
        recovered = step(store, client, case, interrupted)
        assert client.calls[0][0] == "retrieve"
        if phase == "before_call":
            assert not recovered.done
            recovered = step(store, client, case, recovered)
        assert recovered.done
        assert int(marker.exists()) + client.captures == 1


@pytest.mark.parametrize(
    "mistake", ["key", "operation", "retry", "preclaimed", "amount", "unmailed"]
)
def test_refused_dispatch_leaves_no_writes(tmp_path: Path, mistake: str) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = mailed(store) if mistake != "unmailed" else start(store)
        client = FakePaymentClient()
        if mistake == "unmailed":
            case = store.apply_payment_event(
                case.case_token,
                ps.CaptureRequested(),
                case.revision,
                occurred_at=NOW,
                effect_ids=("unmailed-capture",),
            )
        if mistake == "amount":
            store._require_open().execute("UPDATE pb_cases SET amount_cents=5000")
        if mistake == "preclaimed":
            effect = next(e for e in store.effects(case.case_token) if e.machine == "payment")
            store.claim_effect(effect.effect_id, NOW)
        cursor = None
        if mistake in {"key", "operation", "retry"}:
            cursor = step(store, client, case)
        if mistake == "retry":
            client.responses = [PaymentOutcome("transient", "auth")]
            cursor = step(store, client, case, cursor)
        current = store.get_case(case.case_token)
        assert current is not None
        effects, history = store.effects(case.case_token), store.transition_history(case.case_token)
        operations = store._require_open().execute("SELECT * FROM pb_payment_operations").fetchall()
        with pytest.raises((ValueError, EffectConflict, StorageCorruption)):
            run_payment_step(
                store,
                client,
                case.case_token,
                expected_case_revision=current.revision,
                expected_cursor_revision=None if cursor is None else cursor.revision,
                operation_id="payment-0" if mistake == "operation" else "new-operation",
                key="changed" if mistake == "key" else "capture-original",
                retry_effect_id="payment-0",
                clock=lambda: NOW if cursor is None else cursor.next_at,
            )
        assert store.get_case(case.case_token) == current
        assert store.effects(case.case_token) == effects
        assert store.transition_history(case.case_token) == history
        assert (
            store._require_open().execute("SELECT * FROM pb_payment_operations").fetchall()
            == operations
        )


def test_release_succeeded_and_expired_clock_remain_unresolved(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = letter(store, start(store), cs.WindowExpired())
        client = FakePaymentClient()
        client.object = full_outcome(PaymentOutcome("succeeded", "auth", 2999, "external-charge"))
        cursor = step(store, client, case)
        assert not cursor.done
        assert cursor.outcome == client.object
        assert cursor.next_at == NOW + timedelta(minutes=30)
        assert store.get_case(case.case_token) == case
        assert client.releases == 0
        assert any(
            e.machine == "payment" and e.completed_at is None
            for e in store.effects(case.case_token)
        )


def test_local_expiry_margin_reconciles_before_writeoff(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        store.save_workspace("workspace", Workspace(), NOW)
        case = store.start_case("workspace", "auth", NOW, NOW + timedelta(minutes=10))
        for event in (
            cs.ResearchComplete(),
            cs.DraftReady(),
            cs.CustomerApproved(),
            cs.SubmitRequested(),
            cs.SubmitConfirmed("lob-1"),
        ):
            case = letter(store, case, event)
        client = FakePaymentClient()
        client.responses = [PaymentOutcome("transient", "auth")]
        cursor = step(store, client, case)
        cursor = step(store, client, case, cursor)
        assert cursor.next_at == NOW + timedelta(hours=1)  # refusal is not expiry proof.
        assert not any(call[0] == "capture" for call in client.calls)
        assert not cursor.done
        cursor = step(store, client, case, cursor)
        assert cursor.call_kind == "retrieve"
        assert not any(
            e.effect == ps.PaymentEffect.SEND_PAYMENT_LINK for e in store.effects(case.case_token)
        )
        client.object = full_outcome(PaymentOutcome("expired", "auth"))
        # Prior successful retrieval could permit capture; return confirmed expiry there too.
        cursor = step(store, client, case, cursor, writeoff=True)
        assert cursor.done


def test_changed_case_revision_during_call_retains_intent_for_reconciliation(
    tmp_path: Path,
) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = mailed(store)
        client = FakePaymentClient()
        cursor = step(store, client, case)

        class ChangingClient(FakePaymentClient):
            def capture(
                self, authorization_ref: str, amount_cents: int, key: str, *, final_capture: bool
            ) -> PaymentOutcome:
                result = client.capture(
                    authorization_ref, amount_cents, key, final_capture=final_capture
                )
                store.apply_letter_event(
                    case.case_token, cs.DeliveryProofStored("proof"), case.revision, occurred_at=NOW
                )
                return result

        with pytest.raises(RevisionConflict, match="changed"):
            step(store, ChangingClient(), case, cursor)
        interrupted = store.payment_cursor(case.case_token)
        assert interrupted is not None
        assert interrupted.owner is not None
        assert interrupted.outcome.status == "uncertain"
        before = store.effects(case.case_token)
        assert next(e for e in before if e.effect_id == interrupted.effect_id).completed_at is None
        recovered = step(store, client, case, interrupted)
        assert recovered.done
        assert recovered.call_kind == "retrieve"
        assert client.captures == 1


def test_release_after_customer_decline_uses_existing_transition(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = letter(store, start(store), cs.Stopped(cs.StopReason.DECLINED, "customer declined"))
        client = FakePaymentClient()
        client.object = full_outcome(
            PaymentOutcome("canceled", "auth")
        )  # external cancellation already confirmed
        cursor = step(store, client, case)
        assert cursor.done
        assert client.releases == 0
        assert store.get_case(case.case_token) == case


def test_payment_key_cannot_be_reused_for_another_authorization(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        first = mailed(store)
        step(store, FakePaymentClient(), first)
        other = store.start_case("workspace", "other-auth", NOW, NOW + timedelta(days=5))
        other = store.apply_letter_event(
            other.case_token,
            cs.WindowExpired(),
            other.revision,
            occurred_at=NOW,
            effect_ids=("other-route", "other-release"),
        )
        effects = store.effects(other.case_token)
        with pytest.raises(ValueError, match="another case"):
            run_payment_step(
                store,
                FakePaymentClient("other-auth"),
                other.case_token,
                expected_case_revision=other.revision,
                expected_cursor_revision=None,
                operation_id="other-call",
                key="capture-original",
                clock=lambda: NOW,
            )
        assert store.effects(other.case_token) == effects
        assert store.payment_cursor(other.case_token) is None


@pytest.mark.parametrize("kind", ["capture", "release"])
def test_fake_caches_failed_responses_and_preserves_original_terminal_state(kind: str) -> None:
    client = FakePaymentClient()
    client.responses = [PaymentOutcome("transient", "auth")]

    def mutate(key: str) -> PaymentOutcome:
        if kind == "capture":
            return client.capture("auth", 2999, key, final_capture=True)
        return client.release("auth", key)

    failure = mutate("failed-500")
    assert mutate("failed-500") == failure
    assert client.retrieve("auth").status == "requires_capture"
    success = mutate("reconciled-new")
    assert success.status == ("succeeded" if kind == "capture" else "canceled")
    assert mutate("failed-500") == failure
    assert client.retrieve("auth") == success
    with pytest.raises(ValueError, match="state"):
        mutate("after-pruning")
    assert client.retrieve("auth") == success
    assert client.captures == int(kind == "capture")
    assert client.releases == int(kind == "release")


def test_cached_release_failure_recovers_with_distinct_durable_key(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    client = FakePaymentClient()
    client.responses = [PaymentOutcome("transient", "auth")]
    with SqliteTransactionStore(path) as store:
        case = letter(store, start(store), cs.WindowExpired())
        cursor = step(store, client, case)
        cursor = step(store, client, case, cursor)
        assert cursor.outcome.status == "transient"
        assert not cursor.done
        first_key = client.calls[-1][3]
    with SqliteTransactionStore(path) as store:
        cursor = step(store, client, case, cursor)
        assert client.calls[-1][0] == "retrieve"
        cursor = step(store, client, case, cursor)
        assert cursor.done
        assert client.calls[-1][3] != first_key
        assert cursor.mutation_key == client.calls[-1][3]
        assert client.releases == 1


@pytest.mark.parametrize("succeeded", [False, True])
def test_delayed_orphan_recovery_after_24h_uses_original_object(
    tmp_path: Path, succeeded: bool
) -> None:
    path = tmp_path / "cases.sqlite"
    client = FakePaymentClient()
    with SqliteTransactionStore(path) as store:
        case = mailed(store)
        cursor = step(store, client, case)
        with store.submission_ownership():
            pending = store.claim_payment(
                case.case_token,
                case.revision,
                cursor.revision,
                now=NOW,
                operation_id="orphan",
                key=cursor.key,
                retry_effect_id="unused",
            )
            assert pending.mutation_key is not None
            if succeeded:
                client.capture("auth", 2999, pending.mutation_key, final_capture=True)
    with SqliteTransactionStore(path) as store:
        cursor = run_payment_step(
            store,
            client,
            case.case_token,
            expected_case_revision=case.revision,
            expected_cursor_revision=pending.revision,
            operation_id="delayed-retrieve",
            key=pending.key,
            clock=lambda: NOW + timedelta(days=2),
        )
        assert client.calls[-1][0] == "retrieve"
        assert cursor.done == succeeded
        if not succeeded:
            cursor = step(store, client, case, cursor)
            assert cursor.call_kind == "capture"
            assert cursor.done
            assert cursor.mutation_key != pending.mutation_key
        assert client.captures == 1
        assert store.get_case(case.case_token).authorization_ref == "auth"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "outcome",
    [
        replace(full_outcome(PaymentOutcome("requires_capture", "auth")), currency="eur"),
        replace(full_outcome(PaymentOutcome("requires_capture", "auth")), amount_capturable=2800),
        replace(full_outcome(PaymentOutcome("requires_capture", "auth")), amount_received=100),
        replace(
            full_outcome(PaymentOutcome("requires_capture", "auth")), capture_method="automatic"
        ),
        replace(full_outcome(PaymentOutcome("requires_capture", "auth")), amount_cents=2800),
    ],
)
def test_full_object_retrieval_mismatch_never_mutates(
    tmp_path: Path, outcome: PaymentOutcome
) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = mailed(store)
        client = FakePaymentClient()
        client.object = outcome
        with pytest.raises(ValueError, match="invalid"):
            step(store, client, case)
        pending = store.payment_cursor(case.case_token)
        assert pending is not None
        assert pending.owner is not None
        assert client.captures == 0
        client.object = PaymentOutcome("uncertain", "auth")
        cursor = step(store, client, case, pending)
        mutation_key_before = cursor.mutation_key
        cursor = step(store, client, case, cursor)
        assert cursor.call_kind == "retrieve"
        assert cursor.mutation_key == mutation_key_before
        assert all(call[0] == "retrieve" for call in client.calls)


def test_legacy_pending_intent_retained_and_reconciled_before_new_key(tmp_path: Path) -> None:
    import json

    path = tmp_path / "cases.sqlite"
    client = FakePaymentClient()
    with SqliteTransactionStore(path) as store:
        case = mailed(store)
        cursor = step(store, client, case)
        with store.submission_ownership():
            store.claim_payment(
                case.case_token,
                case.revision,
                cursor.revision,
                now=NOW,
                operation_id="legacy-pending",
                key=cursor.key,
                retry_effect_id="unused",
            )

    # Model the exact old canonical codec, without rewriting it on reopening.
    def legacy(text: str) -> str:
        data = json.loads(text)
        data.pop("mutation_key", None)
        data.pop("final_capture", None)
        data.pop("contract_version", None)
        for field in ("currency", "amount_capturable", "amount_received", "capture_method"):
            data["outcome"].pop(field, None)
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT operation_id, intent, result FROM pb_payment_operations"
        ).fetchall()
        for op, intent, result in rows:
            db.execute(
                "UPDATE pb_payment_operations SET intent=?, result=? WHERE operation_id=?",
                (legacy(intent), None if result is None else legacy(result), op),
            )
        text = db.execute("SELECT payload FROM pb_payment_cursor").fetchone()[0]
        db.execute("UPDATE pb_payment_cursor SET payload=?", (legacy(text),))
        preserved = db.execute("SELECT * FROM pb_payment_operations").fetchall()
    with SqliteTransactionStore(path) as store:
        pending = store.payment_cursor(case.case_token)
        assert pending is not None
        assert pending.mutation_key is None
        recovered = step(store, client, case, pending)
        assert client.calls[-1][0] == "retrieve"
        recovered = step(store, client, case, recovered)
        assert recovered.done
        assert client.calls[-1][3] != "capture-original"
        assert (
            store._require_open().execute("SELECT * FROM pb_payment_operations LIMIT 2").fetchall()
            == preserved
        )
        assert client.captures == 1


@pytest.mark.parametrize(("amount", "final"), [(2800, True), (2999, False)])
def test_fake_refuses_partial_capture_and_multicapture(amount: int, final: bool) -> None:
    client = FakePaymentClient()
    with pytest.raises(ValueError, match="full final"):
        client.capture("auth", amount, "key", final_capture=final)
    assert client.calls == []
    assert client.captures == 0


def test_capture_delayed_across_expiry_margin_is_suppressed(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = mailed(store)
        client = FakePaymentClient()
        cursor = step(store, client, case)
        before_margin = case.authorization_expires_at - ps.EXPIRY_MARGIN - timedelta(seconds=1)
        at_margin = case.authorization_expires_at - ps.EXPIRY_MARGIN
        times = iter((before_margin, at_margin, at_margin))
        cursor = run_payment_step(
            store,
            client,
            case.case_token,
            expected_case_revision=case.revision,
            expected_cursor_revision=cursor.revision,
            operation_id="delayed-call",
            key=cursor.key,
            clock=lambda: next(times),
        )
        assert cursor.call_kind == "capture"
        assert cursor.outcome.status == "uncertain"
        assert not cursor.done
        assert client.captures == 0
        assert len(client.calls) == 1
        cursor = step(store, client, case, cursor)
        assert cursor.call_kind == "retrieve"
        assert client.captures == 0


# Exceptional no-charge compensation uses the same dispatcher and retained operations.
def compensation_client() -> FakePaymentClient:
    client = FakePaymentClient("pi_Original")
    client.object = replace(
        full_outcome(PaymentOutcome("succeeded", "pi_Original", charge_ref="ch_Original")),
        evidence_version=1,
        original_charge_ref="ch_Original",
        classification="verified_object",
    )
    return client


def stopped_compensation(store: SqliteTransactionStore) -> CaseRecord:
    store.save_workspace("workspace", Workspace(), NOW)
    case = store.start_case("workspace", "pi_Original", NOW, NOW + timedelta(days=5))
    return letter(store, case, cs.WindowExpired())


def compensation_step(
    store: SqliteTransactionStore,
    client: FakePaymentClient,
    case: CaseRecord,
    *,
    reconcile_capture: bool = False,
) -> PaymentCursor:
    cursor = store.payment_cursor(case.case_token)
    current = store.get_case(case.case_token)
    assert current is not None
    return run_payment_step(
        store,
        client,
        case.case_token,
        expected_case_revision=current.revision,
        expected_cursor_revision=None if cursor is None else cursor.revision,
        operation_id=f"comp-{0 if cursor is None else cursor.revision}",
        key="compensation-namespace",
        clock=lambda: NOW if cursor is None else cursor.next_at,
        compensation_effect_ids=("full-refund", "compensation-log") if reconcile_capture else (),
    )


def compensation_step_at(
    store: SqliteTransactionStore,
    client: FakePaymentClient,
    case: CaseRecord,
    at: datetime,
) -> PaymentCursor:
    cursor = store.payment_cursor(case.case_token)
    current = store.get_case(case.case_token)
    assert current is not None
    return run_payment_step(
        store,
        client,
        case.case_token,
        expected_case_revision=current.revision,
        expected_cursor_revision=None if cursor is None else cursor.revision,
        operation_id=f"timed-comp-{0 if cursor is None else cursor.revision}",
        key="compensation-namespace",
        clock=lambda: at,
    )


def test_full_compensation_reopen_exact_effects_and_no_other_mutations(tmp_path: Path) -> None:
    from parkingbreaker.case_status import CaseStatus

    path = tmp_path / "cases.sqlite"
    client = compensation_client()
    with SqliteTransactionStore(path) as store:
        case = stopped_compensation(store)
        cursor = compensation_step(store, client, case, reconcile_capture=True)
        saved = store.get_case(case.case_token)
        assert saved is not None
        assert saved.case_status is CaseStatus.IN_PROGRESS
        assert saved.payment_state is ps.PaymentState.CAPTURED
        assert (
            store.transition_history(case.case_token)[-1].event_name == "UnexpectedCaptureConfirmed"
        )
        cursor = compensation_step(store, client, case)  # complete original refund lookup
        assert cursor.outcome.refundable
        cursor = compensation_step(store, client, case)  # request is not completion
        assert not cursor.done
        assert store.get_case(case.case_token) == saved
        cursor = compensation_step(store, client, case)  # confirmed full evidence
        assert cursor.done
        saved = store.get_case(case.case_token)
        assert saved is not None
        assert saved.case_status is CaseStatus.DONE
        assert saved.payment_state is ps.PaymentState.REFUNDED
        assert store.transition_history(case.case_token)[-1].event_name == "RefundIssued"
        payment_effects = [e for e in store.effects(case.case_token) if e.machine == "payment"]
        assert [e.effect for e in payment_effects] == [
            ps.PaymentEffect.RELEASE_HOLD,
            ps.PaymentEffect.REFUND_FULL,
            ps.PaymentEffect.LOG_COMPENSATION,
        ]
        assert [e.effect for e in payment_effects if e.completed_at is None] == [
            ps.PaymentEffect.LOG_COMPENSATION
        ]
        assert client.refund_calls == 1
        assert client.captures == client.releases == 0
        assert [c[0] for c in client.calls] == [
            "retrieve",
            "retrieve_refunds",
            "refund",
            "retrieve_refunds",
        ]
        history, effects = store.transition_history(case.case_token), store.effects(case.case_token)
        operations = store._require_open().execute("SELECT * FROM pb_payment_operations").fetchall()
    with SqliteTransactionStore(path) as store:
        assert store.payment_cursor(case.case_token) == cursor
        assert store.get_case(case.case_token) == saved
        assert store.transition_history(case.case_token) == history
        assert store.effects(case.case_token) == effects
        assert (
            store._require_open().execute("SELECT * FROM pb_payment_operations").fetchall()
            == operations
        )
        assert compensation_step(store, client, case) == cursor
        assert client.refund_calls == 1


@pytest.mark.parametrize(
    ("statuses", "amounts"),
    [
        (["succeeded"], [2999]),
        (["succeeded", "succeeded"], [1900, 1000]),
        (["pending"], [2999]),
        (["failed"], [2999]),
        (["succeeded"], [1900]),
        (["succeeded", "pending"], [1900, 1000]),
    ],
)
def test_existing_refunds_settle_only_exact_succeeded_total(
    tmp_path: Path, statuses: list[str], amounts: list[int]
) -> None:
    from parkingbreaker.case_status import CaseStatus
    from parkingbreaker.payment_records import RefundEvidence

    client = compensation_client()
    client.refunds = tuple(
        RefundEvidence(f"re_{i}", "pi_Original", "ch_Original", amount, "usd", status)
        for i, (amount, status) in enumerate(zip(amounts, statuses, strict=True))
    )
    full = sum(a for a, s in zip(amounts, statuses, strict=True) if s == "succeeded") == 2999
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = stopped_compensation(store)
        compensation_step(store, client, case, reconcile_capture=True)
        cursor = compensation_step(store, client, case)
        assert cursor.done is full
        saved = store.get_case(case.case_token)
        assert saved is not None
        assert saved.case_status is (CaseStatus.DONE if full else CaseStatus.IN_PROGRESS)
        assert client.refund_calls == 0
        if not full and "failed" not in statuses:
            compensation_step(store, client, case)
            assert client.refund_calls == 0


def test_unknown_request_empty_lookup_after_days_never_rotates_key(tmp_path: Path) -> None:
    client = compensation_client()
    client.refund_responses = [PaymentOutcome("uncertain", "pi_Original")]
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = stopped_compensation(store)
        compensation_step(store, client, case, reconcile_capture=True)
        compensation_step(store, client, case)
        cursor = compensation_step(store, client, case)
        key = cursor.mutation_key
        cutoff = NOW + timedelta(hours=23)
        lookup = compensation_step_at(store, client, case, cursor.next_at)
        assert lookup.call_kind == "retrieve"
        cursor = compensation_step_at(store, client, case, cutoff + timedelta(hours=1))
        assert cursor.call_kind == "retrieve"
        assert not cursor.done
        assert cursor.mutation_key == key
        assert sum(c[0] == "refund" for c in client.calls) == 1
        assert all(c[3] == key for c in client.calls if c[0] == "refund")
        assert client.refund_calls == 0  # cached uncertainty; no replacement create


def test_confirmed_failed_request_requires_matching_reconciliation_then_new_key(
    tmp_path: Path,
) -> None:
    from parkingbreaker.payment_records import RefundEvidence

    client = compensation_client()
    failed = RefundEvidence("re_Failed", "pi_Original", "ch_Original", 2999, "usd", "failed")
    client.refund_responses = [replace(client.object, refunds=(failed,))]
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = stopped_compensation(store)
        compensation_step(store, client, case, reconcile_capture=True)
        compensation_step(store, client, case)
        cursor = compensation_step(store, client, case)
        first_key = cursor.mutation_key
        cursor = compensation_step(store, client, case)  # empty is not matching failure
        cursor = compensation_step(store, client, case)
        assert cursor.call_kind == "retrieve"
        client.refunds = (failed,)
        cursor = compensation_step(store, client, case)
        cursor = compensation_step(store, client, case)
        assert cursor.call_kind == "refund"
        assert cursor.mutation_key != first_key
        assert compensation_step(store, client, case).done


@pytest.mark.parametrize("change", ["charge", "currency", "amount", "duplicate", "lookupfail"])
def test_bad_refund_evidence_never_settles_or_mutates(tmp_path: Path, change: str) -> None:
    from parkingbreaker.payment_records import RefundEvidence

    client = compensation_client()
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = stopped_compensation(store)
        compensation_step(store, client, case, reconcile_capture=True)
        refund = RefundEvidence("re_1", "pi_Original", "ch_Original", 2999, "usd", "succeeded")
        if change == "charge":
            refund = replace(refund, charge_ref="ch_Other")
        if change == "currency":
            refund = replace(refund, currency="eur")
        if change == "amount":
            refund = replace(refund, amount_cents=4000)
        client.refunds = (refund, refund) if change == "duplicate" else (refund,)
        if change == "lookupfail":
            client.object = PaymentOutcome("uncertain", "pi_Original")
            cursor = compensation_step(store, client, case)
            assert not cursor.done
        else:
            with pytest.raises(ValueError, match="refund evidence"):
                compensation_step(store, client, case)
        assert client.refund_calls == 0
        saved = store.get_case(case.case_token)
        assert saved is not None
        assert saved.payment_state is ps.PaymentState.CAPTURED


@pytest.mark.parametrize("phase", ["before_call", "after_call", "before_commit"])
def test_real_refund_crash_reopen_no_duplicate(tmp_path: Path, phase: str) -> None:
    import subprocess
    import sys

    from parkingbreaker.payment_records import RefundEvidence

    path, marker = tmp_path / "cases.sqlite", tmp_path / "refund"
    with SqliteTransactionStore(path) as store:
        case = stopped_compensation(store)
        compensation_step(store, compensation_client(), case, reconcile_capture=True)
        compensation_step(store, compensation_client(), case)
    script = """
import os, sys
from pathlib import Path
from parkingbreaker.payment_coordinator import run_payment_step
from parkingbreaker.payment_fakes import FakePaymentClient, full_outcome
from parkingbreaker.payment_records import PaymentOutcome
from parkingbreaker.transaction_store import SqliteTransactionStore
from dataclasses import replace
class Client(FakePaymentClient):
    def refund(self, ref, charge, amount, key, *, captured_cents: int):
        if sys.argv[4] == 'before_call':
            os._exit(71)
        result = super().refund(ref, charge, amount, key, captured_cents=captured_cents)
        Path(sys.argv[3]).write_text(result.refunds[0].refund_ref)
        if sys.argv[4] == 'after_call':
            os._exit(71)
        return result
client = Client('pi_Original')
client.object = replace(
    full_outcome(PaymentOutcome('succeeded', 'pi_Original', charge_ref='ch_Original')),
    evidence_version=1, original_charge_ref='ch_Original', classification='verified_object')
with SqliteTransactionStore(Path(sys.argv[1])) as store:
    cursor = store.payment_cursor(sys.argv[2])
    conn = store._require_open()
    class ExitCommit:
        finishing = False
        @property
        def in_transaction(self):
            return conn.in_transaction
        def execute(self, sql, parameters=()):
            if sql.startswith('UPDATE pb_payment_operations SET result='):
                self.finishing = True
            if sql == 'COMMIT' and self.finishing:
                os._exit(71)
            return conn.execute(sql, parameters)
        def close(self):
            conn.close()
    if sys.argv[4] == 'before_commit':
        store._database = ExitCommit()
    run_payment_step(store, client, sys.argv[2],
        expected_case_revision=cursor.case_revision, expected_cursor_revision=cursor.revision,
        operation_id='crashed-refund', key=cursor.key, clock=lambda: cursor.next_at)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), case.case_token, str(marker), phase],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 71, result.stderr.decode()
    client = compensation_client()
    if marker.exists():
        client.refunds = (
            RefundEvidence(
                marker.read_text(), "pi_Original", "ch_Original", 2999, "usd", "succeeded"
            ),
        )
    with SqliteTransactionStore(path) as store:
        interrupted = store.payment_cursor(case.case_token)
        assert interrupted is not None
        assert interrupted.owner == "crashed-refund"
        history, effects = store.transition_history(case.case_token), store.effects(case.case_token)
    with SqliteTransactionStore(path) as store:
        assert store.payment_cursor(case.case_token) == interrupted
        assert store.transition_history(case.case_token) == history
        assert store.effects(case.case_token) == effects
        recovered = compensation_step(store, client, case)
        assert client.calls[0][0] == "retrieve_refunds"
        assert recovered.done is marker.exists()
        if not recovered.done:
            replay = compensation_step(store, client, case)
            assert replay.call_kind == "refund"
            assert replay.mutation_key == interrupted.mutation_key
            assert compensation_step(store, client, case).done
        assert client.refund_calls == int(not marker.exists())
        refund_calls = [c for c in client.calls if c[0] == "refund"]
        assert len(refund_calls) == int(not marker.exists())
        if refund_calls:
            assert refund_calls[0][3] == interrupted.mutation_key


def test_after_call_replay_uses_shared_provider_cache_with_stale_inventory(tmp_path: Path) -> None:
    client = compensation_client()
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = stopped_compensation(store)
        compensation_step(store, client, case, reconcile_capture=True)
        cursor = compensation_step(store, client, case)
        with store.submission_ownership():
            case_now = store.get_case(case.case_token)
            assert case_now is not None
            claimed = store.claim_payment(
                case.case_token,
                case_now.revision,
                cursor.revision,
                now=cursor.next_at,
                operation_id="crash-after-provider-call",
                key=cursor.key,
                retry_effect_id="unused-retry",
            )
        original_key = claimed.mutation_key
        assert original_key is not None
        first_response = client.refund(
            "pi_Original", "ch_Original", 2999, original_key, captured_cents=CERTIFIED_CENTS
        )
        assert first_response.full_refund
        actual_inventory = client.refunds
        client.refunds = ()  # simulate a temporarily stale complete inventory lookup

    with SqliteTransactionStore(path) as store:
        lookup = compensation_step(store, client, case)
        assert lookup.call_kind == "retrieve"
        assert lookup.outcome.refunds == ()
        replay = compensation_step(store, client, case)
        assert replay.call_kind == "refund"
        assert replay.mutation_key == original_key
        assert client.refund_calls == 1
        assert [c[3] for c in client.calls if c[0] == "refund"] == [original_key, original_key]
        client.refunds = actual_inventory
        settled = compensation_step(store, client, case)
        assert settled.done
        assert client.refund_calls == 1


def test_refund_replay_cutoff_is_anchored_and_not_slid(tmp_path: Path) -> None:
    client = compensation_client()
    client.refund_responses = [PaymentOutcome("uncertain", "pi_Original")]
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = stopped_compensation(store)
        compensation_step(store, client, case, reconcile_capture=True)
        compensation_step(store, client, case)
        first = compensation_step(store, client, case)
        assert first.call_kind == "refund"
        original_key = first.mutation_key
        first_intent = next(
            decode_cursor(row[0])
            for row in store._require_open().execute(
                "SELECT intent FROM pb_payment_operations ORDER BY rowid"
            )
            if decode_cursor(row[0]).call_kind == "refund"
        )
        cutoff = first_intent.next_at + timedelta(hours=23)
        lookup = compensation_step_at(store, client, case, NOW + timedelta(hours=1))
        assert lookup.call_kind == "retrieve"
        replay = compensation_step_at(store, client, case, NOW + timedelta(hours=1))
        assert replay.call_kind == "refund"
        assert replay.mutation_key == original_key
        late_lookup = compensation_step_at(store, client, case, NOW + timedelta(hours=22))
        assert late_lookup.call_kind == "retrieve"
        late_replay = compensation_step_at(store, client, case, NOW + timedelta(hours=22))
        assert late_replay.call_kind == "refund"
        assert late_replay.mutation_key == original_key
        assert (
            decode_cursor(
                store._require_open()
                .execute("SELECT intent FROM pb_payment_operations ORDER BY rowid DESC LIMIT 1")
                .fetchone()[0]
            ).next_at
            < cutoff
        )
        cursor = compensation_step_at(store, client, case, cutoff)
        assert cursor.call_kind == "retrieve"
        assert compensation_step_at(store, client, case, cutoff).call_kind == "retrieve"
        refund_calls = [c for c in client.calls if c[0] == "refund"]
        assert len(refund_calls) == 3
        assert all(c[3] == original_key for c in refund_calls)


def test_refund_replay_claim_clock_cannot_cross_provider_call_cutoff(tmp_path: Path) -> None:
    client = compensation_client()
    client.refund_responses = [PaymentOutcome("uncertain", "pi_Original")]
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = stopped_compensation(store)
        compensation_step(store, client, case, reconcile_capture=True)
        compensation_step(store, client, case)
        first = compensation_step(store, client, case)
        original_key = first.mutation_key
        intent = next(
            decode_cursor(row[0])
            for row in store._require_open().execute(
                "SELECT intent FROM pb_payment_operations ORDER BY rowid"
            )
            if decode_cursor(row[0]).call_kind == "refund"
        )
        cutoff = intent.next_at + timedelta(hours=23)
        before = cutoff - timedelta(seconds=1)
        lookup = run_payment_step(
            store,
            client,
            case.case_token,
            expected_case_revision=first.case_revision,
            expected_cursor_revision=first.revision,
            operation_id="pre-cutoff-lookup",
            key=first.key,
            clock=lambda: before,
        )
        assert lookup.call_kind == "retrieve"
        refund_calls_before = sum(c[0] == "refund" for c in client.calls)
        times = iter((before, cutoff - timedelta(milliseconds=500), cutoff, cutoff))
        paused_replay = run_payment_step(
            store,
            client,
            case.case_token,
            expected_case_revision=lookup.case_revision,
            expected_cursor_revision=lookup.revision,
            operation_id="paused-before-refund-replay",
            key=lookup.key,
            clock=lambda: next(times),
        )
        assert paused_replay.call_kind == "refund"
        assert paused_replay.mutation_key == original_key
        assert sum(c[0] == "refund" for c in client.calls) == refund_calls_before
        assert not paused_replay.done


def test_refund_at_replay_cutoff_stays_retrieve_only(tmp_path: Path) -> None:
    client = compensation_client()
    client.refund_responses = [PaymentOutcome("uncertain", "pi_Original")]
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = stopped_compensation(store)
        compensation_step(store, client, case, reconcile_capture=True)
        compensation_step(store, client, case)
        first = compensation_step(store, client, case)
        intent = next(
            decode_cursor(row[0])
            for row in store._require_open().execute(
                "SELECT intent FROM pb_payment_operations ORDER BY rowid"
            )
            if decode_cursor(row[0]).call_kind == "refund"
        )
        cutoff = intent.next_at + timedelta(hours=23)
        lookup = run_payment_step(
            store,
            client,
            case.case_token,
            expected_case_revision=first.case_revision,
            expected_cursor_revision=first.revision,
            operation_id="cutoff-lookup",
            key=first.key,
            clock=lambda: cutoff,
        )
        assert lookup.call_kind == "retrieve"
        refund_calls_before = sum(c[0] == "refund" for c in client.calls)
        after_cutoff = compensation_step(store, client, case)
        assert after_cutoff.call_kind == "retrieve"
        assert sum(c[0] == "refund" for c in client.calls) == refund_calls_before


def test_refund_process_ownership_and_stale_revision(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    entered, proceed = Event(), Event()

    class Blocking(FakePaymentClient):
        def refund(
            self,
            authorization_ref: str,
            charge_ref: str,
            amount_cents: int,
            key: str,
            *,
            captured_cents: int,
        ) -> PaymentOutcome:
            entered.set()
            assert proceed.wait(5)
            return super().refund(
                authorization_ref, charge_ref, amount_cents, key, captured_cents=captured_cents
            )

    client = Blocking("pi_Original")
    client.object = compensation_client().object
    with SqliteTransactionStore(path) as store:
        case = stopped_compensation(store)
        compensation_step(store, client, case, reconcile_capture=True)
        compensation_step(store, client, case)

    def dispatch() -> PaymentCursor:
        with SqliteTransactionStore(path) as store:
            return compensation_step(store, client, case)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(dispatch)
        assert entered.wait(5)
        try:
            with (
                SqliteTransactionStore(path) as store,
                pytest.raises(RevisionConflict, match="owns"),
            ):
                compensation_step(store, compensation_client(), case)
        finally:
            proceed.set()
        cursor = future.result(timeout=5)
    with SqliteTransactionStore(path) as store:
        before = store._require_open().execute("SELECT * FROM pb_payment_operations").fetchall()
        with pytest.raises(RevisionConflict, match="stale"):
            run_payment_step(
                store,
                client,
                case.case_token,
                expected_case_revision=case.revision,
                expected_cursor_revision=cursor.revision,
                operation_id="stale-refund",
                key=cursor.key,
                clock=lambda: cursor.next_at,
            )
        assert (
            store._require_open().execute("SELECT * FROM pb_payment_operations").fetchall()
            == before
        )
        assert client.refund_calls == 1


def test_preslice_succeeded_release_records_preserved_then_compensated(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    client = compensation_client()
    with SqliteTransactionStore(path) as store:
        case = stopped_compensation(store)
        result = compensation_step(store, client, case, reconcile_capture=True)
        # Synthetic pre-note-20 canonical version2 record: succeeded release was unresolved.
        legacy = replace(
            result,
            compensation_started=False,
            case_revision=case.revision,
            next_at=NOW + timedelta(minutes=30),
        )
        db = store._require_open()
        db.execute("DELETE FROM pb_pending_effects WHERE revision>?", (case.revision,))
        db.execute("DROP TRIGGER pb_case_history_no_delete")  # rebuild a pre-trigger record
        db.execute("DELETE FROM pb_case_history WHERE revision>?", (case.revision,))
        db.execute("UPDATE pb_cases SET payment_state='RELEASED', revision=?", (case.revision,))
        db.execute(
            "UPDATE pb_pending_effects SET completed_at=NULL, outcome=NULL WHERE machine='payment'"
        )
        db.execute("UPDATE pb_payment_operations SET result=?", (cursor_json(legacy),))
        db.execute("UPDATE pb_payment_cursor SET payload=?", (cursor_json(legacy),))
        before = db.execute("SELECT * FROM pb_payment_operations").fetchall()
        effects = store.effects(case.case_token)
    with SqliteTransactionStore(path) as store:
        assert store.payment_cursor(case.case_token) == legacy
        assert store.effects(case.case_token) == effects
        assert (
            store._require_open().execute("SELECT * FROM pb_payment_operations").fetchall()
            == before
        )
        cursor = compensation_step(store, client, case, reconcile_capture=True)
        assert cursor.compensation_started
        assert (
            store._require_open().execute("SELECT * FROM pb_payment_operations").fetchall()[:1]
            == before
        )
        compensation_step(store, client, case)
        compensation_step(store, client, case)
        assert compensation_step(store, client, case).done


def test_refund_completion_replay_and_different_outcome_no_writes(tmp_path: Path) -> None:
    client = compensation_client()
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = stopped_compensation(store)
        compensation_step(store, client, case, reconcile_capture=True)
        compensation_step(store, client, case)
        result = compensation_step(store, client, case)
        intent_text = (
            store._require_open()
            .execute(
                "SELECT intent FROM pb_payment_operations WHERE operation_id=?",
                (f"comp-{result.revision - 2}",),
            )
            .fetchone()[0]
        )
        intent = decode_cursor(intent_text)
        before = store._require_open().execute("SELECT * FROM pb_payment_operations").fetchall()
        with store.submission_ownership():
            assert store.finish_payment(intent, result.outcome, now=result.next_at) == result
            with pytest.raises(EffectConflict, match="different outcome"):
                store.finish_payment(
                    intent, PaymentOutcome("uncertain", "pi_Original"), now=result.next_at
                )
        assert (
            store._require_open().execute("SELECT * FROM pb_payment_operations").fetchall()
            == before
        )


def test_public_events_cannot_invent_compensation_or_settle_partial_stop(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = stopped_compensation(store)
        before = store.transition_history(case.case_token)
        with pytest.raises(ValueError, match="dispatcher evidence"):
            store.apply_payment_event(
                case.case_token,
                ps.UnexpectedCaptureConfirmed("pi_Original", "ch_Original"),
                case.revision,
                occurred_at=NOW,
                effect_ids=("invented", "log"),
            )
        assert store.transition_history(case.case_token) == before
        compensation_step(store, compensation_client(), case, reconcile_capture=True)
        saved = store.get_case(case.case_token)
        assert saved is not None
        for amount in (1900, 2999):
            with pytest.raises(ValueError, match="reconciled full compensation"):
                store.apply_payment_event(
                    case.case_token,
                    ps.RefundIssued("re_Invented", amount, 2999),
                    saved.revision,
                    occurred_at=NOW,
                )
        assert store.get_case(case.case_token) == saved


def test_fake_refund_cached_uncertainty_and_original_charge_cap() -> None:
    client = compensation_client()
    failed = PaymentOutcome("uncertain", "pi_Original")
    client.refund_responses = [failed]
    assert (
        client.refund(
            "pi_Original", "ch_Original", 2999, "cached500", captured_cents=CERTIFIED_CENTS
        )
        == failed
    )
    assert (
        client.refund(
            "pi_Original", "ch_Original", 2999, "cached500", captured_cents=CERTIFIED_CENTS
        )
        == failed
    )
    assert client.refund_calls == 0
    success = client.refund(
        "pi_Original", "ch_Original", 2999, "new", captured_cents=CERTIFIED_CENTS
    )
    assert success.full_refund
    assert client.refund_calls == 1
    assert (
        client.refund("pi_Original", "ch_Original", 2999, "new", captured_cents=CERTIFIED_CENTS)
        == success
    )
    assert (
        client.refund(
            "pi_Original", "ch_Original", 2999, "different", captured_cents=CERTIFIED_CENTS
        ).status
        == "uncertain"
    )
    assert client.refund_calls == 1
    with pytest.raises(ValueError, match="original full"):
        client.refund("pi_Original", "ch_Other", 2999, "new", captured_cents=CERTIFIED_CENTS)
