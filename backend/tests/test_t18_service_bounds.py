"""T18 contract tests: service-side history reads stay bounded."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from mail_samples import make_request
from parkingbreaker.api import schemas as s
from parkingbreaker.blob_store import FakeBlobStore
from parkingbreaker.case_service import DESTINATION_DECLINED, CaseService, CaseServiceDeps
from parkingbreaker.case_state import CaseState, StopReason, stop_family
from parkingbreaker.intake_store import IntakeRecord
from parkingbreaker.lob_submission import Accepted
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.prehold import PreholdDeps
from parkingbreaker.service_policy import SERVICE_CONTROL, STATE_BASIS
from parkingbreaker.stop_reasons import stop_message
from parkingbreaker.submission_records import outcome_json
from parkingbreaker.transaction_records import workspace_json
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
HISTORY_SIZE = 2000
# The newest history rows one stop lookup may read; the test is the bound.
WINDOW = 8
STOP_AT = HISTORY_SIZE - 2
SUBMIT_AT = 3
RETURN_AT = 4
STOP_PAYLOAD = json.dumps({"reason": "DECLINED", "detail": DESTINATION_DECLINED})
SUBMIT_PAYLOAD = json.dumps({"lob_letter_id": "ltr-1"})


class _CursorSpy:
    def __init__(self, cursor: sqlite3.Cursor, owner: _ConnectionSpy) -> None:
        self._cursor = cursor
        self._owner = owner

    def __iter__(self) -> _CursorSpy:
        return self

    def __next__(self) -> tuple[Any, ...]:
        row = next(self._cursor)
        self._owner.rows_read += 1
        return cast("tuple[Any, ...]", row)

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows = self._cursor.fetchall()
        self._owner.rows_read += len(rows)
        return rows

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        rows = self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)
        self._owner.rows_read += len(rows)
        return rows

    def fetchone(self) -> tuple[Any, ...] | None:
        row = self._cursor.fetchone()
        if row is not None:
            self._owner.rows_read += 1
        return cast("tuple[Any, ...] | None", row)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _ConnectionSpy:
    """Counts every case-history row a service call reads back from the store."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.rows_read = 0

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> _CursorSpy | sqlite3.Cursor:
        cursor = self._connection.execute(sql, parameters)
        if "FROM pb_case_history" not in sql:
            return cursor
        return _CursorSpy(cursor, self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _stamp(position: int) -> str:
    return (NOW + timedelta(minutes=position)).isoformat()


def _seed(path: Path, size: int, *, stop: bool = True, letter_state: str = "RETURNED") -> str:
    """One synthetic case whose long history holds every row the service scans seek.

    A confirmed and returned original sits at the head of the history; the recorded
    stop (a destination decline) is the last letter row, with settled payment rows
    behind it. The middle is payment churn. Not a reachable lifecycle: like the store
    bounds seeds, this exists to bound the reads, not to replay one.
    """
    token = uuid.uuid4().hex
    with SqliteTransactionStore(path):
        pass
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
                _stamp(1),
                _stamp(2999),
                2999,
                letter_state,
                "CAPTURED",
                1,
                size + 1,
            ),
        )
        database.executemany(
            "INSERT INTO pb_case_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            _history_rows(token, size, stop=stop),
        )
    return token


def _history_rows(token: str, size: int, *, stop: bool) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for position in range(1, size + 1):
        machine, source, target = "payment", "CAPTURE_RETRYING", "CAPTURE_RETRYING"
        event, payload = "RetryCapture", "{}"
        if position <= 2:
            machine, source, target, event, payload = (
                "letter",
                "NEEDS_CUSTOMER",
                "NEEDS_CUSTOMER",
                "Tick",
                "{}",
            )
        elif position == SUBMIT_AT:
            machine, source, target, event, payload = (
                "letter",
                "SUBMITTING",
                "IN_TRANSIT",
                "SubmitConfirmed",
                SUBMIT_PAYLOAD,
            )
        elif position == RETURN_AT:
            machine, source, target, event, payload = (
                "letter",
                "IN_TRANSIT",
                "RETURNED",
                "ReturnConfirmed",
                "{}",
            )
        elif stop and position == STOP_AT:
            machine, source, target, event, payload = (
                "letter",
                "RETURNED",
                "STOPPED_NO_CHARGE",
                "Stopped",
                STOP_PAYLOAD,
            )
        rows.append(
            (token, position + 1, machine, source, target, event, payload, _stamp(position))
        )
    return rows


def _service(store: SqliteTransactionStore) -> CaseService:
    unused: Any = object()
    deps = PreholdDeps(
        unused, None, unused, unused, unused, lambda: NOW, 25, STATE_BASIS, SERVICE_CONTROL
    )
    return CaseService(
        CaseServiceDeps(store, deps, lambda: NOW, FakePaymentClient(), FakeBlobStore())
    )


def _accepted_cursor(store: SqliteTransactionStore, token: str) -> None:
    """A frozen first attempt whose create operation accepted letter ltr-1."""
    accepted = outcome_json(Accepted("ltr-1", "940000001"))
    store.prepare(token, 1, make_request(), NOW - timedelta(hours=1))
    database = store._require_open()
    database.execute(
        "INSERT INTO pb_submission_operations "
        "(operation_id, case_token, attempt, cursor_revision, kind, started_at, "
        "outcome_json, completed_at) VALUES (?, ?, 1, 1, 'create', ?, ?, ?)",
        ("op-1", token, _stamp(SUBMIT_AT), accepted, _stamp(SUBMIT_AT + 1)),
    )
    database.execute(
        "INSERT INTO pb_submission_cursors "
        "(case_token, attempt, first_sent_at, sends, next_at, lookup_only, revision, "
        "owner, case_revision, outcome_json) VALUES (?, 1, ?, 1, ?, 0, 2, NULL, ?, ?)",
        (token, _stamp(SUBMIT_AT), _stamp(SUBMIT_AT + 1), HISTORY_SIZE + 1, accepted),
    )


def _fixed_intake(case_token: str) -> Callable[[str], IntakeRecord]:
    def intake(case_id: str) -> IntakeRecord:
        return IntakeRecord(
            intake_id=case_id,
            workspace_token="workspace",
            owner="owner-1",
            intake_json="{}",
            prehold_json="{}",
            agency_json=None,
            selected_tier=None,
            case_token=case_token,
            created_at=NOW,
            revision=1,
        )

    return intake


def _fixed_status(case_id: str) -> s.CaseStatus:
    return s.CaseStatus(
        case_id=case_id,
        workspace_id="workspace",
        state=CaseState.RETURNED,
        payment_state=None,
        selected_tier=None,
        window_started_at=None,
        window_expires_at=None,
        seconds_remaining=None,
        window_status=None,
        question=None,
        stop=None,
    )


def test_case_stop_reads_only_the_newest_history_window(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    token = _seed(path, HISTORY_SIZE, letter_state="STOPPED_NO_CHARGE")
    with SqliteTransactionStore(path) as store:
        service = _service(store)
        connection = _ConnectionSpy(store._require_open())
        store._database = connection  # type: ignore[assignment]

        stop = service._case_stop("case-1", token)

        message = stop_message(StopReason.DECLINED, None)
        assert stop == s.Stop(
            case_id="case-1",
            family=stop_family(StopReason.DECLINED),
            hard_stop=None,
            reason_key=message.key,
            message=message.text,
            official_quote=None,
        )
        assert connection.rows_read <= WINDOW


def test_case_stop_without_a_stop_still_reads_only_the_window(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    token = _seed(path, HISTORY_SIZE, stop=False)
    with SqliteTransactionStore(path) as store:
        service = _service(store)
        connection = _ConnectionSpy(store._require_open())
        store._database = connection  # type: ignore[assignment]

        assert service._case_stop("case-1", token) is None
        assert connection.rows_read <= WINDOW


def test_declined_check_reads_only_the_newest_history_window(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    token = _seed(path, HISTORY_SIZE, letter_state="STOPPED_NO_CHARGE")
    with SqliteTransactionStore(path) as store:
        service = _service(store)
        connection = _ConnectionSpy(store._require_open())
        store._database = connection  # type: ignore[assignment]

        assert service._declined(token) is True
        assert connection.rows_read <= WINDOW


def test_tracking_accepted_scan_stops_at_the_first_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cases.sqlite"
    token = _seed(path, HISTORY_SIZE)
    with SqliteTransactionStore(path) as store:
        _accepted_cursor(store, token)
        service = _service(store)
        monkeypatch.setattr(service, "_intake", _fixed_intake(token))
        monkeypatch.setattr(service, "status", _fixed_status)
        connection = _ConnectionSpy(store._require_open())
        store._database = connection  # type: ignore[assignment]

        tracking = service.tracking("case-1")

        assert [(event.event, event.occurred_at) for event in tracking.events] == [
            ("accepted", datetime.fromisoformat(_stamp(SUBMIT_AT)))
        ]
        assert tracking.tracking_number == "940000001"
        assert connection.rows_read <= SUBMIT_AT


def test_first_return_confirmation_stops_at_the_first_match(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    token = _seed(path, HISTORY_SIZE)
    with SqliteTransactionStore(path) as store:
        case = store.get_case(token)
        assert case is not None
        service = _service(store)
        connection = _ConnectionSpy(store._require_open())
        store._database = connection  # type: ignore[assignment]

        assert service._first_return_confirmation(case) == datetime.fromisoformat(_stamp(RETURN_AT))
        assert connection.rows_read <= RETURN_AT
