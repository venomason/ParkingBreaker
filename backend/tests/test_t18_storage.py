"""T18 contract tests: storage edges and telemetry correctness, via the real SQLite stores."""

from __future__ import annotations

import hashlib
import inspect
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mail_samples import make_request
from parkingbreaker import case_state as cs
from parkingbreaker.lob_submission import idempotency_key
from parkingbreaker.mail_attempt_store import (
    PreparationConflict,
    SqliteMailAttemptStore,
)
from parkingbreaker.oplog.counters import CounterKey
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import (
    EffectConflict,
    RevisionConflict,
    SqliteTransactionStore,
)
from parkingbreaker.workspace import Workspace

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
LEGACY_PDF = b"%PDF-1.4\nfrozen synthetic packet"
LEGACY_TOKEN = "synthetic-random-token-1"
REQUEST = make_request(LEGACY_PDF, LEGACY_TOKEN)


def _legacy_database(path: Path) -> tuple[object, ...]:
    """A pre-envelope v1 database holding one frozen row that has no envelope."""
    with sqlite3.connect(path) as database:
        database.execute("""CREATE TABLE pb_mail_attempts (
            case_token TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK (attempt IN (1, 2)),
            pdf BLOB NOT NULL,
            submission_token TEXT NOT NULL UNIQUE,
            idempotency_key TEXT NOT NULL UNIQUE,
            pdf_sha256 TEXT NOT NULL,
            prepared_at TEXT NOT NULL,
            PRIMARY KEY (case_token, attempt)
        )""")
        row = (
            "legacy-case",
            1,
            LEGACY_PDF,
            LEGACY_TOKEN,
            idempotency_key("legacy-case", 1),
            hashlib.sha256(LEGACY_PDF).hexdigest(),
            NOW.isoformat(),
        )
        database.execute("INSERT INTO pb_mail_attempts VALUES (?, ?, ?, ?, ?, ?, ?)", row)
    return row


def _identity_columns(path: Path) -> tuple[object, ...] | None:
    with sqlite3.connect(path) as database:
        row = database.execute(
            """SELECT case_token, attempt, pdf, submission_token, idempotency_key,
                      pdf_sha256, prepared_at FROM pb_mail_attempts
               WHERE case_token = 'legacy-case' AND attempt = 1"""
        ).fetchone()
    return None if row is None else tuple(row)


# --- 1. legacy incomplete requests -------------------------------------------------------


def test_legacy_row_is_recoverable_when_caller_resupplies_the_same_frozen_request(
    tmp_path: Path,
) -> None:
    """A v1 row (NULL envelope) must not stay permanently unreadable.

    Re-preparing the identical frozen PDF/token (the only identity the row holds) is bounded
    recovery: the envelope is completed and the case becomes readable again.
    """
    path = tmp_path / "cases.sqlite"
    original = _legacy_database(path)
    with SqliteMailAttemptStore(path) as store:
        recovered = store.prepare("legacy-case", 1, REQUEST, NOW + timedelta(hours=1))
        assert recovered.request == REQUEST
        assert recovered.key == idempotency_key("legacy-case", 1)
        assert store.get("legacy-case", 1) == recovered
    # Recovery keeps the frozen identity, including the original preparation time.
    assert _identity_columns(path) == original
    with SqliteMailAttemptStore(path) as reopened:
        assert reopened.get("legacy-case", 1) == recovered


def test_legacy_recovery_never_changes_frozen_pdf_token_or_key(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    original = _legacy_database(path)
    different = (
        make_request(b"%PDF-1.4\nsomething else", LEGACY_TOKEN),
        make_request(LEGACY_PDF, "another-random-token"),
    )
    with SqliteMailAttemptStore(path) as store:
        for request in different:
            # A request that does not match the frozen identity is refused as a conflict
            # (a clear terminal answer), not silently adopted and not reported as corruption.
            with pytest.raises(PreparationConflict):
                store.prepare("legacy-case", 1, request, NOW)
            assert _identity_columns(path) == original


def test_legacy_incomplete_row_does_not_block_other_cases(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    _legacy_database(path)
    with SqliteMailAttemptStore(path) as store:
        fresh = store.prepare("new-case", 1, make_request(b"%PDF-1.4\nnew", "new-token"), NOW)
        assert store.get("new-case", 1) == fresh


# --- 2. atomicity, revision checks, immutable outcomes ----------------------------------


def _started(store: SqliteTransactionStore) -> CaseRecord:
    store.save_workspace("workspace", Workspace(), NOW)
    return store.start_case("workspace", "auth", NOW, NOW + timedelta(days=5))


def _submitting(store: SqliteTransactionStore) -> CaseRecord:
    case = _started(store)
    for event in (cs.ResearchComplete(), cs.DraftReady(), cs.CustomerApproved()):
        case = store.apply_letter_event(case.case_token, event, case.revision, occurred_at=NOW)
    return case


def _new_id() -> str:
    return uuid.uuid4().hex


def _submit(store: SqliteTransactionStore, case: CaseRecord) -> CaseRecord:
    return store.apply_letter_event(
        case.case_token,
        cs.SubmitRequested(),
        case.revision,
        occurred_at=NOW,
        new_effect_id=_new_id,
    )


def _snapshot(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    tables = ("pb_cases", "pb_case_history", "pb_pending_effects")
    with sqlite3.connect(path) as database:
        return {t: database.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables}


def test_stale_revision_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = _started(store)
        before = _snapshot(path)
        with pytest.raises(RevisionConflict):
            store.apply_letter_event(
                case.case_token,
                cs.ResearchComplete(),
                case.revision + 1,
                occurred_at=NOW,
            )
        assert _snapshot(path) == before
        assert store.get_case(case.case_token) == case
        advanced = store.apply_letter_event(
            case.case_token, cs.ResearchComplete(), case.revision, occurred_at=NOW
        )
        assert advanced.revision == case.revision + 1


def test_failed_event_application_is_atomic(tmp_path: Path) -> None:
    """A failure while naming effects aborts the whole transition: no partial rows."""
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = _submitting(store)
        before = _snapshot(path)

        def explode() -> str:
            raise RuntimeError("effect id source failed")

        with pytest.raises(RuntimeError, match="effect id source failed"):
            store.apply_letter_event(
                case.case_token,
                cs.SubmitRequested(),
                case.revision,
                occurred_at=NOW,
                new_effect_id=explode,
            )
        assert _snapshot(path) == before
        assert store.get_case(case.case_token) == case
        assert _submit(store, case).revision == case.revision + 1


def test_recorded_effect_outcome_cannot_be_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = _submit(store, _submitting(store))
        effect = store.effects(case.case_token)[0]
        store.claim_effect(effect.effect_id, NOW)
        done = store.complete_effect(effect.effect_id, "ok", NOW)
        with pytest.raises(EffectConflict):
            store.complete_effect(effect.effect_id, "different", NOW)
        with pytest.raises(EffectConflict):
            store.claim_effect(effect.effect_id, NOW)
        stored = next(e for e in store.effects(case.case_token) if e.effect_id == effect.effect_id)
        assert stored == done
        assert stored.outcome == "ok"


# --- 3. counter identity and bounded reads ----------------------------------------------


def _counter_rows(path: Path) -> list[str]:
    with sqlite3.connect(path) as database:
        return [r[0] for r in database.execute("SELECT event_token FROM pb_coverage_counters")]


def test_distinct_events_never_collapse_into_one_counter_row(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        # Two different requests with an identical key are two events.
        store.record_counter("request-1", CounterKey(), occurred_at=NOW)
        store.record_counter("request-2", CounterKey(), occurred_at=NOW)
        # A replay of the same (request, milestone) is the same event.
        store.record_counter("request-1", CounterKey(), occurred_at=NOW)
        assert store.flush_counters(occurred_at=NOW)
    rows = _counter_rows(path)
    assert len(rows) == 2
    assert len(set(rows)) == 2


def test_counter_flush_is_bounded_by_a_limit(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    assert "limit" in inspect.signature(SqliteTransactionStore.flush_counters).parameters
    with SqliteTransactionStore(path) as store:
        for index in range(7):
            store.record_counter(f"request-{index}", CounterKey(), occurred_at=NOW)
        flush: Callable[..., bool] = store.flush_counters
        assert flush(occurred_at=NOW, limit=3)
        assert len(_counter_rows(path)) == 3
        assert flush(occurred_at=NOW, limit=3)
        assert len(_counter_rows(path)) == 6
        # The remainder stays pending and is delivered exactly once, never duplicated.
        assert flush(occurred_at=NOW, limit=3)
        assert flush(occurred_at=NOW, limit=3)
        assert len(_counter_rows(path)) == 7


def test_hot_path_history_reads_accept_a_bound(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = _submit(store, _submitting(store))
        history: Callable[..., tuple[Any, ...]] = store.transition_history
        full = store.transition_history(case.case_token)
        assert len(full) >= 3
        limited = history(case.case_token, limit=2)
        assert len(limited) == 2
        assert all(row in full for row in limited)
        effects: Callable[..., tuple[Any, ...]] = store.effects
        assert len(effects(case.case_token, limit=1)) == 1


# --- 4. rollback failures and clock skew ------------------------------------------------


class _FailingRollback:
    """Delegates to the real connection but fails every explicit ROLLBACK."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self.inner = inner

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if sql.strip().upper() == "ROLLBACK":
            raise sqlite3.OperationalError("injected rollback failure")
        return self.inner.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _inject(store: SqliteMailAttemptStore) -> sqlite3.Connection:
    inner = store._require_open()
    store._database = _FailingRollback(inner)  # type: ignore[assignment]
    return inner


def _restore(store: SqliteMailAttemptStore, inner: sqlite3.Connection) -> None:
    store._database = inner
    if inner.in_transaction:
        inner.execute("ROLLBACK")


def test_mail_prepare_rollback_failure_does_not_mask_original_error(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        store.prepare("case-1", 1, REQUEST, NOW)
        inner = _inject(store)
        try:
            with pytest.raises(PreparationConflict):
                store.prepare("case-1", 1, make_request(b"%PDF-1.4\nchanged", LEGACY_TOKEN), NOW)
        finally:
            _restore(store, inner)
        assert store.get("case-1", 1) is not None


def test_workspace_save_rollback_failure_does_not_mask_original_error(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        store.save_workspace("workspace", Workspace(), NOW)
        inner = _inject(store)
        try:
            with pytest.raises(RevisionConflict):
                store.save_workspace("workspace", Workspace(), NOW, expected_revision=9)
        finally:
            _restore(store, inner)
        assert store.get_workspace("workspace") is not None


def test_event_rollback_failure_does_not_mask_original_error(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = _started(store)
        inner = _inject(store)
        try:
            with pytest.raises(RevisionConflict):
                store.apply_letter_event(
                    case.case_token, cs.ResearchComplete(), 99, occurred_at=NOW
                )
        finally:
            _restore(store, inner)
        assert store.get_case(case.case_token) == case


def test_effect_rollback_failure_does_not_mask_original_error(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = _started(store)
        inner = _inject(store)
        try:
            with pytest.raises(EffectConflict):
                store.claim_effect("missing-effect", NOW)
        finally:
            _restore(store, inner)
        assert store.get_case(case.case_token) == case


def test_counter_flush_rollback_failure_does_not_mask_original_error(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        store.record_counter("request-1", CounterKey(), occurred_at=NOW)
        inner = _inject(store)
        try:
            with pytest.raises(ValueError, match="cannot precede creation"):
                store.flush_counters(occurred_at=NOW - timedelta(hours=1))
        finally:
            _restore(store, inner)


def test_workspace_clock_skew_is_recoverable_and_leaves_history_intact(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        first = store.save_workspace("workspace", Workspace(), NOW)
        with pytest.raises(ValueError, match="precede"):
            store.save_workspace(
                "workspace", Workspace(notes="late"), NOW - timedelta(hours=1), expected_revision=1
            )
        assert store.get_workspace("workspace") == first
        assert store.workspace_history("workspace") == (first,)
        second = store.save_workspace(
            "workspace", Workspace(notes="ok"), NOW + timedelta(minutes=1), expected_revision=1
        )
        assert store.workspace_history("workspace") == (first, second)


def test_counter_flush_clock_skew_is_recoverable_and_delivers_once(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        store.record_counter("request-1", CounterKey(), occurred_at=NOW)
        with pytest.raises(ValueError, match="cannot precede creation"):
            store.flush_counters(occurred_at=NOW - timedelta(hours=1))
        assert _counter_rows(path) == []
        assert store.flush_counters(occurred_at=NOW + timedelta(minutes=1))
        assert len(_counter_rows(path)) == 1
        assert store.flush_counters(occurred_at=NOW + timedelta(minutes=2))
        assert len(_counter_rows(path)) == 1


def test_effect_clock_skew_is_recoverable(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = _submit(store, _submitting(store))
        effect = store.effects(case.case_token)[0]
        with pytest.raises(ValueError, match="precede"):
            store.claim_effect(effect.effect_id, NOW - timedelta(seconds=1))
        claimed = store.claim_effect(effect.effect_id, NOW)
        with pytest.raises(ValueError, match="precede"):
            store.complete_effect(effect.effect_id, "ok", NOW - timedelta(seconds=1))
        done = store.complete_effect(effect.effect_id, "ok", NOW + timedelta(seconds=1))
        assert claimed.claimed_at == NOW
        assert done.outcome == "ok"


def test_event_clock_skew_never_corrupts_the_case(tmp_path: Path) -> None:
    """An event stamped before its predecessor is refused or harmless; the case stays usable."""
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = _started(store)
        case = store.apply_letter_event(
            case.case_token, cs.ResearchComplete(), case.revision, occurred_at=NOW
        )
        try:
            skewed = store.apply_letter_event(
                case.case_token, cs.DraftReady(), case.revision, occurred_at=NOW - timedelta(days=1)
            )
        except ValueError:
            skewed = case
        # Whatever happened, every later correct-clock operation still works and history reads.
        store.transition_history(case.case_token)
        later = store.apply_letter_event(
            case.case_token,
            cs.DraftReady() if skewed is case else cs.CustomerApproved(),
            skewed.revision,
            occurred_at=NOW + timedelta(minutes=1),
        )
        assert later.revision == skewed.revision + 1
        assert store.get_case(case.case_token) == later
