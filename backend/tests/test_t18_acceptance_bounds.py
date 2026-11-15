"""Independent T18 strict bounds contracts, adapted from the acceptance probes."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from mail_samples import RECIPIENT
from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.effect_dispatch import dispatch_once
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.payment_coordinator import run_payment_step
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.payment_records import PaymentOutcome
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import EffectConflict, SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_payment_coordinator import step as payment_step
from test_submission_coordinator import prepared
from test_submission_coordinator import step as submission_step
from test_t18_service_bounds import _accepted_cursor, _seed, _service
from test_t18_store_bounds import NOW, _letter, _lifecycle, _new_id, _payment
from test_transaction_lifecycle import mailed

SIZE = 128


class _Cursor:
    def __init__(self, inner: sqlite3.Cursor, owner: _Reads, category: str) -> None:
        self.inner, self.owner, self.category = inner, owner, category
        self.rows = 0

    def __iter__(self) -> _Cursor:
        return self

    def __next__(self) -> tuple[Any, ...]:
        row = next(self.inner)
        self.rows += 1
        return cast(tuple[Any, ...], row)

    def fetchone(self) -> tuple[Any, ...] | None:
        row = self.inner.fetchone()
        self.rows += row is not None
        return cast(tuple[Any, ...] | None, row)

    def fetchall(self) -> list[tuple[Any, ...]]:
        assert not self.owner.stream_operations or self.category != "operations", (
            "operation histories must be streamed, never fetched whole"
        )
        rows = self.inner.fetchall()
        self.rows += len(rows)
        return rows

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        rows = self.inner.fetchmany() if size is None else self.inner.fetchmany(size)
        self.rows += len(rows)
        return rows

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class _Reads:
    """Count fetched rows, separating mandatory replay from predicate reads."""

    def __init__(self, inner: sqlite3.Connection, *, stream_operations: bool = False) -> None:
        self.inner, self.stream_operations = inner, stream_operations
        self.queries: dict[str, list[_Cursor]] = {
            "tracking": [],
            "pending": [],
            "history": [],
            "operations": [],
        }

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> _Cursor | sqlite3.Cursor:
        cursor = self.inner.execute(sql, parameters)
        category = None
        if sql.lstrip().upper().startswith("SELECT"):
            if "SELECT payload FROM pb_webhooks" in sql:
                category = "tracking"
            elif "FROM pb_pending_effects" in sql and "completed_at IS NULL" in sql:
                category = "pending"
            elif "FROM pb_case_history" in sql:
                category = "history"
            elif "ORDER BY rowid" in sql and any(
                f"FROM {table}" in sql
                for table in ("pb_payment_operations", "pb_submission_operations")
            ):
                category = "operations"
        if category is None:
            return cursor
        result = _Cursor(cursor, self, category)
        self.queries[category].append(result)
        return result

    def rows(self, category: str) -> int:
        return sum(query.rows for query in self.queries[category])

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _watch(store: SqliteTransactionStore, *, stream_operations: bool = False) -> _Reads:
    spy = _Reads(store._require_open(), stream_operations=stream_operations)
    store._database = spy  # type: ignore[assignment]
    return spy


def _pending_retries(store: SqliteTransactionStore) -> CaseRecord:
    store.save_workspace("workspace", Workspace(), NOW)
    case = store.start_case("workspace", "auth", NOW, NOW + timedelta(days=5))
    for event in (
        cs.ResearchComplete(),
        cs.DraftReady(),
        cs.CustomerApproved(),
        cs.SubmitRequested(),
        cs.SubmitConfirmed("lob-1"),
    ):
        case = _letter(store, case, event)
    case = _payment(store, case, ps.CaptureFailed("transient"))
    for _ in range(SIZE):
        case = _payment(store, case, ps.RetryCapture())
        case = _payment(store, case, ps.CaptureFailed("transient"))
    assert store._payment_case(case.case_token) == case
    return case


def test_pending_predicate_does_not_materialize_all_effects(tmp_path: Path) -> None:
    """The real coordinator rejects duplicate money intents after just two matches."""
    with SqliteTransactionStore(tmp_path / "pending.sqlite") as store:
        case = _pending_retries(store)
        client = FakePaymentClient()
        spy = _watch(store)
        with pytest.raises(EffectConflict, match="exactly one pending payment effect"):
            run_payment_step(
                store,
                client,
                case.case_token,
                expected_case_revision=case.revision,
                expected_cursor_revision=None,
                operation_id="guard",
                key="original",
                clock=lambda: NOW,
            )
        assert not client.calls
        assert spy.rows("pending") <= 2, (
            f"exactly-one guard read {spy.rows('pending')} rows instead of at most two"
        )


def test_dispatch_money_predicate_stops_at_first_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SqliteTransactionStore(tmp_path / "dispatch.sqlite") as store:
        case = _pending_retries(store)
        store._require_open().execute(
            "UPDATE pb_pending_effects SET claimed_at=?,completed_at=?,outcome='resolved' "
            "WHERE case_token=? AND effect='SUBMIT_LETTER'",
            (NOW.isoformat(), NOW.isoformat(), case.case_token),
        )
        spy = _watch(store)

        # Stop before claiming; this contract measures the dispatch predicate itself.
        def stop_before_provider(*args: Any, **kwargs: Any) -> None:
            raise EffectConflict("predicate answered")

        monkeypatch.setattr("parkingbreaker.effect_dispatch.run_payment_step", stop_before_provider)
        with pytest.raises(EffectConflict, match="predicate answered"):
            dispatch_once(
                store,
                FakeLobClient(),
                FakePaymentClient(),
                lambda: NOW,
                _new_id,
                case_id=case.case_token,
            )
        assert len(spy.queries["pending"]) == 2
        assert spy.queries["pending"][1].rows == 1


def test_done_payment_refund_predicate_stops_at_first_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SqliteTransactionStore(tmp_path / "done.sqlite") as store:
        case = mailed(store)
        client = FakePaymentClient()
        previous = payment_step(store, client, case)
        previous = payment_step(store, client, case, previous)
        assert previous.done
        # Freeze mandatory validation at the valid captured snapshot. Synthetic tail
        # isolates consumption of the completed-coordinator's pending-refund predicate.
        monkeypatch.setattr(store, "payment_cursor", lambda _: previous)
        database = store._require_open()
        database.executemany(
            "INSERT INTO pb_pending_effects "
            "(effect_id,case_token,revision,machine,effect,created_at) "
            "VALUES (?,?,?,'payment',?,?)",
            (
                (
                    f"tail-{i}",
                    case.case_token,
                    case.revision,
                    "REFUND_RETURNED_MAIL" if i == 0 else "LOG_WRITE_OFF",
                    NOW.isoformat(),
                )
                for i in range(SIZE)
            ),
        )

        def stop_before_provider(*args: Any, **kwargs: Any) -> None:
            raise EffectConflict("refund found")

        monkeypatch.setattr(store, "claim_payment", stop_before_provider)
        current = store.get_case(case.case_token)
        assert current is not None
        spy = _watch(store)
        with pytest.raises(EffectConflict, match="refund found"):
            run_payment_step(
                store,
                client,
                case.case_token,
                expected_case_revision=current.revision,
                expected_cursor_revision=previous.revision,
                operation_id="refund",
                key=previous.key,
                clock=lambda: NOW,
            )
        assert spy.rows("pending") == 1
        assert len(client.calls) == 2


@pytest.mark.parametrize("lookup", ["return", "failure"])
@pytest.mark.parametrize("trailing", [0, 3])
def test_recent_return_lookup_does_not_materialize_whole_tracking_history(
    tmp_path: Path, lookup: str, trailing: int
) -> None:
    with SqliteTransactionStore(tmp_path / "tracking.sqlite") as store:
        case = _lifecycle(store)
        if lookup == "failure":
            _accepted_cursor(store, case.case_token)
        for i in range(SIZE + trailing):
            data: dict[str, object] = {
                "event": "letter.in_transit",
                "attempt": 1,
                "letter_id": "lob-1" if lookup == "return" else "ltr-1",
            }
            if i == SIZE - 1:
                data.update(
                    event="letter.returned_to_sender" if lookup == "return" else "letter.failed",
                    failure_evidence=True,
                    return_cause="UNKNOWN",
                    mailing_cost_cents=777,
                    mailing_cost_status="ESTIMATE",
                )
            store.record_webhook(
                "lob",
                str(i),
                case.case_token,
                json.dumps(data),
                occurred_at=NOW,
                new_effect_id=_new_id,
            )
        spy = _watch(store)
        if lookup == "return":
            facts, cost, estimated = _service(store)._returned_mail_evidence(
                "case-1", case, RECIPIENT, "lob-1"
            )
            assert facts.return_cause.value == "UNKNOWN"
            assert (cost, estimated) == (777, True)
        else:
            assert store.mail_failure_recorded(case.case_token, 1)
        assert spy.rows("tracking") <= trailing + 1, (
            f"recent matching record: read {spy.rows('tracking')} rows instead of {trailing + 1}"
        )


def test_duplicate_refund_guard_stops_after_second_intent(tmp_path: Path) -> None:
    path = tmp_path / "duplicates.sqlite"
    token = _seed(path, SIZE)
    with SqliteTransactionStore(path) as store:
        store._require_open().execute("DROP TRIGGER pb_case_history_no_update")  # tampering
        store._require_open().execute(
            "UPDATE pb_case_history SET machine='payment',event_name='ReturnedMailRefundRequested'"
        )
        spy = _watch(store)
        with pytest.raises(StorageCorruption, match="multiple returned-mail refund intents"):
            store.returned_mail_refund(token)
        assert spy.rows("history") <= 2, (
            f"duplicate guard read {spy.rows('history')} matches before rejecting"
        )


@pytest.mark.parametrize("machine", ["payment", "submission"])
@pytest.mark.parametrize("corrupt_at", [None, 0, 7])
def test_operation_history_validators_stream_and_validate_every_row(
    tmp_path: Path, machine: str, corrupt_at: int | None
) -> None:
    with SqliteTransactionStore(tmp_path / "operations.sqlite") as store:
        if machine == "payment":
            case = mailed(store)
            payments = FakePaymentClient()
            payments.responses = [PaymentOutcome("uncertain", "auth")] * 8
            payment = None
            for _ in range(8):
                payment = payment_step(store, payments, case, payment)
            expected: object = payment
        else:
            case = prepared(store)
            lob = FakeLobClient()
            submission = None
            now = NOW
            for _ in range(8):
                submission = submission_step(store, lob, case, submission, now)
                now = submission.next_at
            expected = submission
        table = "pb_payment_operations" if machine == "payment" else "pb_submission_operations"
        if corrupt_at is not None:
            column, value = ("intent", "{}") if machine == "payment" else ("cursor_revision", 1000)
            store._require_open().execute(
                f"UPDATE {table} SET {column}=? WHERE rowid="
                f"(SELECT rowid FROM {table} ORDER BY rowid LIMIT 1 OFFSET ?)",
                (value, corrupt_at),
            )
        spy = _watch(store, stream_operations=True)

        def read_cursor() -> object:
            return (
                store.payment_cursor(case.case_token)
                if machine == "payment"
                else store.submission_cursor(case.case_token, 1)
            )

        if corrupt_at is None:
            assert read_cursor() == expected
            assert spy.rows("operations") == 8
        else:
            with pytest.raises(StorageCorruption):
                read_cursor()
            assert spy.rows("operations") == corrupt_at + 1
