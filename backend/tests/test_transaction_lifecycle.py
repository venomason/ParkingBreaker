from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.case_status import CaseStatus, payment_event_for
from parkingbreaker.state_table import IllegalTransition
from parkingbreaker.transaction_records import CaseRecord, lifecycle_event_json
from parkingbreaker.transaction_store import (
    EffectConflict,
    RevisionConflict,
    SqliteTransactionStore,
)
from parkingbreaker.workspace import Workspace

NOW = datetime(2026, 10, 11, tzinfo=UTC)


def start(store: SqliteTransactionStore) -> CaseRecord:
    store.save_workspace("workspace", Workspace(), NOW)
    return store.start_case("workspace", "auth", NOW, NOW + timedelta(days=5))


def letter(store: SqliteTransactionStore, case: CaseRecord, event: cs.Event) -> CaseRecord:
    t = cs.transition(case.letter_state, event, case.attempt)
    expected: list[tuple[str, cs.Effect | ps.PaymentEffect]] = [
        ("letter", effect) for effect in t.effects
    ]
    payment = case.payment_state
    for effect in t.effects:
        mapped = payment_event_for(effect)
        if mapped is not None:
            pt = ps.payment_transition(payment, mapped)
            expected.extend(("payment", e) for e in pt.effects)
            payment = pt.target
    before = store.effects(case.case_token)
    result = store.apply_letter_event(
        case.case_token,
        event,
        case.revision,
        occurred_at=NOW,
        effect_ids=tuple(f"effect-{case.revision}-{i}" for i in range(len(expected))),
    )
    assert result.letter_state is t.target
    assert result.payment_state is payment
    assert result.attempt == cs.next_attempt(case.attempt, t)
    emitted = store.effects(case.case_token)[len(before) :]
    assert [(e.machine, e.effect) for e in emitted] == expected
    assert all(e.created_at == NOW and e.revision == result.revision for e in emitted)
    history = store.transition_history(case.case_token)
    row = next(r for r in history if r.revision == result.revision and r.machine == "letter")
    assert (row.source, row.target, row.event_name, row.occurred_at) == (
        t.source,
        t.target,
        type(event).__name__,
        NOW,
    )
    return result


def payment(store: SqliteTransactionStore, case: CaseRecord, event: ps.PaymentEvent) -> CaseRecord:
    t = ps.payment_transition(case.payment_state, event)
    before = store.effects(case.case_token)
    result = store.apply_payment_event(
        case.case_token,
        event,
        case.revision,
        occurred_at=NOW,
        effect_ids=tuple(f"effect-{case.revision}-{i}" for i in range(len(t.effects))),
    )
    assert result.letter_state is case.letter_state
    assert result.payment_state is t.target
    assert [(e.machine, e.effect) for e in store.effects(case.case_token)[len(before) :]] == [
        ("payment", effect) for effect in t.effects
    ]
    return result


def mailed(store: SqliteTransactionStore) -> CaseRecord:
    case = start(store)
    for event in (
        cs.ResearchComplete(),
        cs.DraftReady(),
        cs.CustomerApproved(),
        cs.SubmitRequested(),
        cs.SubmitConfirmed("lob-1"),
    ):
        case = letter(store, case, event)
    assert case.payment_state is ps.PaymentState.CAPTURE_PENDING
    return case


def test_happy_path_and_reopening_preserve_every_record(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = mailed(store)
        case = payment(store, case, ps.CaptureSucceeded("charge-1"))
        case = letter(store, case, cs.DeliveryProofStored("proof-1"))
        assert case.letter_state is cs.CaseState.DELIVERED
        assert case.payment_state is ps.PaymentState.CAPTURED
        assert case.case_status is CaseStatus.DONE
        history = store.transition_history(case.case_token)
        assert json.loads(history[-2].event_json) == {"charge_ref": "charge-1"}
        assert json.loads(history[-1].event_json) == {"proof_ref": "proof-1"}
        effects = store.effects(case.case_token)
        store.claim_effect(effects[0].effect_id, NOW)
        store.complete_effect(effects[0].effect_id, "submitted", NOW)
        effects = store.effects(case.case_token)
        pending = store.pending_effects(case.case_token)
    with SqliteTransactionStore(path) as store:
        assert store.get_case(case.case_token) == case
        assert store.transition_history(case.case_token) == history
        assert store.effects(case.case_token) == effects
        assert store.pending_effects(case.case_token) == pending
        assert store.start_case("workspace", "auth", NOW, NOW + timedelta(days=5)) == case


def test_return_allows_one_free_remail(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = payment(store, mailed(store), ps.CaptureSucceeded("charge-1"))
        for event in (
            cs.ReturnConfirmed("address"),
            cs.RemailRequested(),
            cs.SubmitConfirmed("lob-2"),
            cs.ReturnConfirmed("address"),
        ):
            case = letter(store, case, event)
        assert case.attempt == 2
        before = store.effects(case.case_token)
        with pytest.raises(IllegalTransition):
            letter(store, case, cs.RemailRequested())
        assert store.effects(case.case_token) == before
        assert (
            sum(
                e.effect == ps.PaymentEffect.CAPTURE_PAYMENT and e.machine == "payment"
                for e in before
            )
            == 1
        )
        case = letter(store, case, cs.ReturnClosed())
        assert case.case_status is CaseStatus.DONE


def test_stop_releases_hold_atomically_and_preserves_reason(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = letter(store, start(store), cs.Stopped(cs.StopReason.DECLINED, "customer declined"))
        assert case.letter_state is cs.CaseState.STOPPED_NO_CHARGE
        assert case.payment_state is ps.PaymentState.RELEASED
        assert case.case_status is CaseStatus.DONE
        history = store.transition_history(case.case_token)
        assert len(history) == 2
        assert json.loads(history[0].event_json) == {
            "reason": "DECLINED",
            "detail": "customer declined",
        }
        assert history[1].event_name == "ReleaseRequested"


@pytest.mark.parametrize("failure", ["stale", "letter", "payment", "ids", "duplicate"])
def test_refused_events_write_nothing(tmp_path: Path, failure: str) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = mailed(store)
        history = store.transition_history(case.case_token)
        effects = store.effects(case.case_token)
        if failure in ("ids", "duplicate"):
            case = payment(store, case, ps.CaptureFailed("temporary"))
            history = store.transition_history(case.case_token)
            effects = store.effects(case.case_token)

        def refuse() -> None:
            if failure == "stale":
                store.apply_letter_event(
                    case.case_token, cs.DeliveryProofStored("p"), 1, occurred_at=NOW
                )
            elif failure == "letter":
                store.apply_letter_event(
                    case.case_token, cs.ResearchComplete(), case.revision, occurred_at=NOW
                )
            elif failure == "payment":
                store.apply_payment_event(
                    case.case_token, ps.ReleaseRequested(), case.revision, occurred_at=NOW
                )
            else:
                store.apply_payment_event(
                    case.case_token,
                    ps.RetryCapture(),
                    case.revision,
                    occurred_at=NOW,
                    effect_ids=() if failure == "ids" else (effects[0].effect_id,),
                )

        expected_error = {
            "stale": RevisionConflict,
            "letter": IllegalTransition,
            "payment": IllegalTransition,
            "ids": ValueError,
            "duplicate": sqlite3.IntegrityError,
        }[failure]
        with pytest.raises(expected_error, match=r".+"):
            refuse()
        assert store.get_case(case.case_token) == case
        assert store.transition_history(case.case_token) == history
        assert store.effects(case.case_token) == effects


def test_payment_retry_writeoff_effects_match_pure_machine(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = mailed(store)
        for event in (
            ps.CaptureFailed("temporary"),
            ps.RetryCapture(),
            ps.CaptureFailed("again"),
            ps.CardDeclined("declined"),
        ):
            case = payment(store, case, event)
        assert case.payment_state is ps.PaymentState.WRITTEN_OFF
        assert [e.effect for e in store.effects(case.case_token)[-2:]] == [
            ps.PaymentEffect.SEND_PAYMENT_LINK,
            ps.PaymentEffect.LOG_WRITE_OFF,
        ]


def test_outbox_idempotency_and_refusals(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = mailed(store)
        effect = store.pending_effects(case.case_token)[0]
        with pytest.raises(EffectConflict):
            store.complete_effect(effect.effect_id, "ok", NOW)
        with pytest.raises(EffectConflict):
            store.claim_effect("missing", NOW)
        with pytest.raises(ValueError, match="precede"):
            store.claim_effect(effect.effect_id, NOW - timedelta(seconds=1))
        claimed = store.claim_effect(effect.effect_id, NOW)
        assert claimed.claimed_at == NOW
        assert claimed in store.pending_effects(case.case_token)
        with pytest.raises(EffectConflict):
            store.claim_effect(effect.effect_id, NOW)
        completed = store.complete_effect(effect.effect_id, "ok", NOW)
        assert store.complete_effect(effect.effect_id, "ok", NOW + timedelta(days=1)) == completed
        with pytest.raises(EffectConflict):
            store.complete_effect(effect.effect_id, "different", NOW)
        with pytest.raises(EffectConflict):
            store.claim_effect(effect.effect_id, NOW)
        assert completed not in store.pending_effects(case.case_token)
        assert completed in store.effects(case.case_token)


@pytest.mark.parametrize("letter_event", [True, False])
def test_failure_before_commit_rolls_back_state_history_and_outbox(
    tmp_path: Path,
    letter_event: bool,
) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = (
            start(store)
            if letter_event
            else payment(store, mailed(store), ps.CaptureFailed("temporary"))
        )
        history = store.transition_history(case.case_token)
        effects = store.effects(case.case_token)
        connection = store._require_open()

        class FailingCommit:
            @property
            def in_transaction(self) -> bool:
                return connection.in_transaction

            def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
                if sql == "COMMIT":
                    raise RuntimeError("simulated crash before commit")
                return connection.execute(sql, parameters)

            def close(self) -> None:
                connection.close()

        store._database = FailingCommit()  # type: ignore[assignment]

        def crash() -> None:
            if letter_event:
                store.apply_letter_event(
                    case.case_token,
                    cs.WindowExpired(),
                    case.revision,
                    occurred_at=NOW,
                    effect_ids=("release-letter", "release-payment"),
                )
            else:
                store.apply_payment_event(
                    case.case_token,
                    ps.RetryCapture(),
                    case.revision,
                    occurred_at=NOW,
                    effect_ids=("retry-crash",),
                )

        with pytest.raises(RuntimeError, match="simulated crash"):
            crash()
    with SqliteTransactionStore(path) as store:
        assert store.get_case(case.case_token) == case
        assert store.transition_history(case.case_token) == history
        assert store.effects(case.case_token) == effects


def test_competing_lifecycle_writers_have_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = start(store)
    barrier = Barrier(2)

    def apply(suffix: str) -> CaseRecord | None:
        with SqliteTransactionStore(path) as store:
            barrier.wait(timeout=5)
            try:
                return store.apply_letter_event(
                    case.case_token,
                    cs.WindowExpired(),
                    case.revision,
                    occurred_at=NOW,
                    effect_ids=(f"letter-{suffix}", f"payment-{suffix}"),
                )
            except RevisionConflict:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(apply, suffix) for suffix in ("a", "b")]
        winners = [r for f in futures if (r := f.result(timeout=10)) is not None]
    assert len(winners) == 1
    with SqliteTransactionStore(path) as store:
        assert store.get_case(case.case_token) == winners[0]
        assert len(store.transition_history(case.case_token)) == 2
        assert len(store.effects(case.case_token)) == 2


def test_version_one_migration_preserves_initial_records(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = start(store)
    assert case.amount_cents > 0
    # The deployed v1 schema predates the v5 no-charge CHECK, so restore the exact
    # historical paid pb_cases table (same columns, FK, unique and revision checks)
    # before dropping the tables version 1 never had and resetting its version.
    database = sqlite3.connect(path, isolation_level=None)
    database.executescript("""PRAGMA foreign_keys=OFF; PRAGMA legacy_alter_table=ON;
        BEGIN IMMEDIATE;
        DROP TABLE pb_pending_effects;
        DROP TABLE pb_case_history;
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
        UPDATE pb_store_versions SET version=1 WHERE component='transactions';
        COMMIT;
        PRAGMA foreign_keys=ON; PRAGMA legacy_alter_table=OFF;""")
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []
    assert database.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert database.execute(
        "SELECT version FROM pb_store_versions WHERE component='transactions'"
    ).fetchall() == [(1,)]
    schema = database.execute("SELECT sql FROM sqlite_master WHERE name='pb_cases'").fetchall()
    assert len(schema) == 1
    assert "CHECK (amount_cents > 0)" in schema[0][0]
    assert "CHECK (amount_cents >= 0)" not in schema[0][0]
    assert database.execute("SELECT amount_cents FROM pb_cases").fetchall() == [
        (case.amount_cents,)
    ]
    database.close()
    with SqliteTransactionStore(path) as store:
        assert store.get_case(case.case_token) == case
        assert store.transition_history(case.case_token) == ()
        assert store.effects(case.case_token) == ()
        assert store._require_open().execute(
            "SELECT version FROM pb_store_versions WHERE component='transactions'"
        ).fetchall() == [(5,)]
        assert store._require_open().execute("PRAGMA foreign_key_check").fetchall() == []
        assert store._require_open().execute("SELECT amount_cents FROM pb_cases").fetchall() == [
            (case.amount_cents,)
        ]
        letter(store, case, cs.ResearchComplete())


def test_disallowed_mapped_payment_rolls_back_letter_transition(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = payment(store, start(store), ps.ReleaseRequested())
        history = store.transition_history(case.case_token)
        effects = store.effects(case.case_token)
        with pytest.raises(IllegalTransition):
            store.apply_letter_event(
                case.case_token,
                cs.WindowExpired(),
                case.revision,
                occurred_at=NOW,
                effect_ids=("letter", "payment"),
            )
        assert store.get_case(case.case_token) == case
        assert store.transition_history(case.case_token) == history
        assert store.effects(case.case_token) == effects


def test_concurrent_claims_have_one_owner(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = mailed(store)
        effect_id = store.pending_effects(case.case_token)[0].effect_id
    barrier = Barrier(2)

    def claim() -> bool:
        with SqliteTransactionStore(path) as store:
            barrier.wait(timeout=5)
            try:
                store.claim_effect(effect_id, NOW)
                return True
            except EffectConflict:
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        assert sum(f.result(timeout=10) for f in futures) == 1


@pytest.mark.parametrize("reason", [cs.StopReason.UNSUPPORTED, cs.StopReason.UNRESOLVABLE_FACT])
def test_legacy_stop_history_replays_and_preserves_exact_payload(
    tmp_path: Path, reason: cs.StopReason
) -> None:
    expected = '{"detail":"legacy stop","reason":"' + reason.value + '"}'
    event = cs.Stopped(reason, "legacy stop")
    assert lifecycle_event_json(event) == expected
    path = tmp_path / "legacy.sqlite3"
    with SqliteTransactionStore(path) as store:
        case = letter(store, start(store), event)
        assert store.payment_cursor(case.case_token) is None  # Full history replay.
        history = store.transition_history(case.case_token)
        assert history[0].event_json == expected
    with SqliteTransactionStore(path) as reopened:
        assert reopened.payment_cursor(case.case_token) is None
        assert reopened.transition_history(case.case_token) == history


@pytest.mark.parametrize(
    "reason", [cs.StopReason.NOT_YET_VERIFIED, cs.StopReason.PROCESS_INCOMPATIBLE]
)
@pytest.mark.parametrize("hard_stop", list(cs.HardStop))
def test_new_stop_history_replays_and_preserves_hard_stop(
    tmp_path: Path, reason: cs.StopReason, hard_stop: cs.HardStop
) -> None:
    event = cs.Stopped(reason, "Official requirement", hard_stop)
    path = tmp_path / "v7.sqlite3"
    with SqliteTransactionStore(path) as store:
        case = letter(store, start(store), event)
        assert store.payment_cursor(case.case_token) is None
        history = store.transition_history(case.case_token)
        assert history[0].event_json == lifecycle_event_json(event)
        assert json.loads(history[0].event_json)["hard_stop"] == hard_stop.value
    with SqliteTransactionStore(path) as reopened:
        assert reopened.payment_cursor(case.case_token) is None
        assert reopened.transition_history(case.case_token) == history
