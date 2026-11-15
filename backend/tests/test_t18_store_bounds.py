"""T18 contract tests: case history and outbox reads stay bounded at the store."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.transaction_records import CaseRecord, workspace_json
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
SEEDED = 512


class _CursorSpy:
    def __init__(self, cursor: sqlite3.Cursor, owner: _ConnectionSpy, table: str) -> None:
        self._cursor = cursor
        self._owner = owner
        self._table = table

    def __iter__(self) -> _CursorSpy:
        return self

    def __next__(self) -> tuple[Any, ...]:
        row = next(self._cursor)
        self._owner.rows_read[self._table] += 1
        return cast(tuple[Any, ...], row)

    def fetchall(self) -> list[tuple[Any, ...]]:
        if self._owner.stream_only:
            raise AssertionError(f"{self._table} rows must be streamed, never fetched whole")
        rows = self._cursor.fetchall()
        self._owner.rows_read[self._table] += len(rows)
        return rows

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        rows = self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)
        self._owner.rows_read[self._table] += len(rows)
        return rows

    def fetchone(self) -> tuple[Any, ...] | None:
        row = self._cursor.fetchone()
        if row is not None:
            self._owner.rows_read[self._table] += 1
        return cast(tuple[Any, ...] | None, row)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _ConnectionSpy:
    """Counts every row the store reads back from the case history and the outbox."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.rows_read = {"history": 0, "effects": 0}
        self.stream_only = False

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> _CursorSpy | sqlite3.Cursor:
        cursor = self._connection.execute(sql, parameters)
        if not sql.lstrip().upper().startswith("SELECT"):
            return cursor
        if "FROM pb_case_history" in sql:
            return _CursorSpy(cursor, self, "history")
        if "FROM pb_pending_effects" in sql:
            return _CursorSpy(cursor, self, "effects")
        return cursor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _stamp(revision: int) -> str:
    return (NOW + timedelta(minutes=revision)).isoformat()


def _seed(path: Path, size: int, *, settled: int = 0) -> str:
    """One synthetic case with ``size`` history rows and one outbox row per revision.

    The oldest ``settled`` outbox rows are already completed; the rest stay pending.
    """
    token = "seeded-case"
    with sqlite3.connect(path) as database:
        database.execute(
            "INSERT INTO pb_workspaces VALUES (?, ?, ?, ?)",
            ("workspace", workspace_json(Workspace()), _stamp(1), 1),
        )
        database.execute(
            "INSERT INTO pb_cases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token,
                "workspace",
                "auth",
                NOW.isoformat(),
                (NOW + timedelta(days=5)).isoformat(),
                2999,
                "NEEDS_CUSTOMER",
                "AUTHORIZED",
                1,
                size + 1,
            ),
        )
        database.executemany(
            "INSERT INTO pb_case_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    token,
                    revision,
                    "letter",
                    "NEEDS_CUSTOMER",
                    "NEEDS_CUSTOMER",
                    "Tick",
                    "{}",
                    _stamp(revision),
                )
                for revision in range(2, size + 2)
            ),
        )
        database.executemany(
            "INSERT INTO pb_pending_effects "
            "(effect_id, case_token, revision, machine, effect, created_at, "
            "claimed_at, outcome, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    f"effect-{revision}",
                    token,
                    revision,
                    "letter",
                    "SUBMIT_LETTER",
                    _stamp(revision),
                    _stamp(revision) if revision - 2 < settled else None,
                    "submitted" if revision - 2 < settled else None,
                    _stamp(revision) if revision - 2 < settled else None,
                )
                for revision in range(2, size + 2)
            ),
        )
    return token


def _new_id() -> str:
    return uuid.uuid4().hex


def _letter(store: SqliteTransactionStore, case: CaseRecord, event: cs.Event) -> CaseRecord:
    return store.apply_letter_event(
        case.case_token, event, case.revision, occurred_at=NOW, new_effect_id=_new_id
    )


def _payment(store: SqliteTransactionStore, case: CaseRecord, event: ps.PaymentEvent) -> CaseRecord:
    effects = ps.payment_transition(case.payment_state, event).effects
    return store.apply_payment_event(
        case.case_token,
        event,
        case.revision,
        occurred_at=NOW,
        effect_ids=tuple(_new_id() for _ in effects),
    )


def _lifecycle(store: SqliteTransactionStore) -> CaseRecord:
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
    case = _payment(store, case, ps.CaptureSucceeded("charge-1"))
    return _letter(store, case, cs.DeliveryProofStored("proof-1"))


def test_newest_first_with_a_limit_reads_only_the_newest_rows(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path):
        pass
    token = _seed(path, SEEDED)
    with SqliteTransactionStore(path) as store:
        full_history = store.transition_history(token)
        full_effects = store.effects(token)
        assert len(full_history) == len(full_effects) == SEEDED

        connection = _ConnectionSpy(store._require_open())
        store._database = connection  # type: ignore[assignment]

        history: Callable[..., tuple[Any, ...]] = store.transition_history
        newest_history = history(token, limit=3, newest_first=True)
        assert newest_history == tuple(reversed(full_history[-3:]))
        assert connection.rows_read["history"] == 3

        effects: Callable[..., tuple[Any, ...]] = store.effects
        newest_effects = effects(token, limit=3, newest_first=True)
        assert newest_effects == tuple(reversed(full_effects[-3:]))
        assert connection.rows_read["effects"] == 3

        # Defaults stay unchanged: the full case, oldest first.
        assert history(token) == full_history
        assert effects(token) == full_effects
        assert history(token, newest_first=True) == tuple(reversed(full_history))
        assert effects(token, newest_first=True) == tuple(reversed(full_effects))


def test_newest_reads_reject_a_bad_bound_or_flag(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path):
        pass
    token = _seed(path, 2)
    with SqliteTransactionStore(path) as store:
        history: Callable[..., tuple[Any, ...]] = store.transition_history
        effects: Callable[..., tuple[Any, ...]] = store.effects
        for bad in (0, -1, 1.5, "2", True):
            with pytest.raises(ValueError, match="limit must be a positive integer"):
                history(token, limit=bad)
            with pytest.raises(ValueError, match="limit must be a positive integer"):
                effects(token, limit=bad)
        with pytest.raises(ValueError, match="newest_first must be a boolean"):
            history(token, newest_first=1)
        with pytest.raises(ValueError, match="newest_first must be a boolean"):
            effects(token, limit=1, newest_first=1)


def test_latest_effect_reads_exactly_one_row(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path):
        pass
    token = _seed(path, SEEDED)
    with SqliteTransactionStore(path) as store:
        newest = store.effects(token)[-1]

        connection = _ConnectionSpy(store._require_open())
        store._database = connection  # type: ignore[assignment]

        before = connection.rows_read["effects"]
        assert store.latest_effect(token) == newest
        assert connection.rows_read["effects"] == before + 1
        assert store.latest_effect("no-such-case") is None
        assert connection.rows_read["effects"] == before + 1


def test_pending_effects_reads_only_incomplete_rows(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path):
        pass
    token = _seed(path, SEEDED, settled=SEEDED - 7)
    with SqliteTransactionStore(path) as store:
        expected = tuple(e for e in store.effects(token) if e.completed_at is None)
        assert len(expected) == 7
        # The default result is unchanged: the same incomplete rows in creation order.
        assert store.pending_effects(token) == expected

        connection = _ConnectionSpy(store._require_open())
        store._database = connection  # type: ignore[assignment]
        assert store.pending_effects(token) == expected
        assert connection.rows_read["effects"] == 7


def test_payment_case_replay_streams_history_row_by_row(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = _lifecycle(store)
        expected = store._payment_case(case.case_token)
        assert expected == store.get_case(case.case_token)
        history_rows = len(store.transition_history(case.case_token))
        effect_rows = len(store.effects(case.case_token))

        connection = _ConnectionSpy(store._require_open())
        store._database = connection  # type: ignore[assignment]
        connection.stream_only = True
        assert store._payment_case(case.case_token) == expected
        assert connection.rows_read["history"] == history_rows
        assert connection.rows_read["effects"] == effect_rows
