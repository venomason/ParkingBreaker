"""T20 review fixes: a bounded, indexed backlog where every event reaches a final outcome."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker import webhook_backlog_store as store_module
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.case_service import CaseService
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import Accepted, Canceled, LetterEvidence, LobResponse
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.transaction_store import RevisionConflict, SqliteTransactionStore
from test_submission_coordinator import NOW, prepared
from test_t17_capture_policy import fallback_world, run_dispatches, scan
from test_t17_mail_evidence import ORIGINAL, TOKEN, cursor_for, resource
from test_t17_pieceB_review import app_for as clocked_app_for
from test_t17_pieceB_review import distinct_event
from test_t20_webhook_recovery import (
    app_for,
    capture_effect_count,
    land_cursor,
    letter_body,
    numbered_case,
)

# The supervisor ruling: one pass per sweep reads at most this many rows, oldest first.
BATCH = 100
RETENTION = timedelta(days=30)
WARNING_LOGGER = "parkingbreaker.case_service"


class _CursorSpy:
    def __init__(self, cursor: sqlite3.Cursor, owner: _ConnectionSpy, table: str) -> None:
        self._cursor = cursor
        self._owner = owner
        self._table = table

    def __iter__(self) -> _CursorSpy:
        return self

    def __next__(self) -> tuple[Any, ...]:
        row = next(self._cursor)
        self._owner.count(self._table, 1)
        return cast("tuple[Any, ...]", row)

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows = self._cursor.fetchall()
        self._owner.count(self._table, len(rows))
        return rows

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        rows = self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)
        self._owner.count(self._table, len(rows))
        return rows

    def fetchone(self) -> tuple[Any, ...] | None:
        row = self._cursor.fetchone()
        if row is not None:
            self._owner.count(self._table, 1)
        return cast("tuple[Any, ...] | None", row)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _ConnectionSpy:
    """Counts the backlog and case rows a service call reads; trips fast when unbounded."""

    def __init__(self, connection: sqlite3.Connection, ceilings: dict[str, int]) -> None:
        self._connection = connection
        self._ceilings = ceilings
        self.rows_read = {"backlog": 0, "cases": 0}

    def count(self, table: str, rows: int) -> None:
        self.rows_read[table] += rows
        ceiling = self._ceilings.get(table)
        if ceiling is not None and self.rows_read[table] > ceiling:
            raise AssertionError(f"unbounded {table} read: more than {ceiling} rows")

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> _CursorSpy | sqlite3.Cursor:
        cursor = self._connection.execute(sql, parameters)
        if not sql.lstrip().upper().startswith("SELECT"):
            return cursor
        if "FROM pb_webhook_backlog" in sql:
            return _CursorSpy(cursor, self, "backlog")
        if "FROM pb_cases" in sql:
            return _CursorSpy(cursor, self, "cases")
        return cursor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def watch(real: CaseService, **ceilings: int) -> _ConnectionSpy:
    spy = _ConnectionSpy(real._store._require_open(), ceilings)
    real._store._database = spy  # type: ignore[assignment]
    return spy


def unwatch(real: CaseService, spy: _ConnectionSpy) -> None:
    real._store._database = spy._connection


def reduced(
    event_id: str, letter_id: str, token: str | None, kind: str = "letter.in_transit"
) -> str:
    """The verified, reduced envelope ingress stores for an early Lob event."""
    return json.dumps(
        {
            "id": event_id,
            "event_type": kind,
            "date_created": NOW.isoformat(),
            "body": {
                "id": letter_id,
                "tracking_number": None,
                "metadata": {"submission_token": token},
                "tracking_events": [],
            },
        }
    )


def park(
    store: SqliteTransactionStore,
    event_id: str,
    letter_id: str,
    token: str | None,
    *,
    at: Any = NOW,
) -> None:
    assert store.record_webhook(
        "lob",
        event_id,
        None,
        reduced(event_id, letter_id, token),
        occurred_at=at,
        new_effect_id=lambda: uuid4().hex,
    )


def park_foreign(store: SqliteTransactionStore, count: int) -> None:
    """Signed traffic for letters this system never anchored, oldest first."""
    for index in range(count):
        park(
            store,
            f"evt_foreign_{index}",
            f"ltr_foreign_{index}",
            f"tok_foreign_{index}",
            at=NOW + timedelta(seconds=index),
        )


def accepted_cases(store: SqliteTransactionStore, count: int) -> None:
    for number in range(1, count + 1):
        land_cursor(
            store,
            numbered_case(store, number),
            LobResponse(200, f"ltr_{number}", evidence=LetterEvidence.PRODUCTION),
        )


def unmatched(real: CaseService) -> list[tuple[str, str]]:
    return [(row.event_id, row.reason) for row in real._store.unmatched_webhooks()]


def later(real: CaseService, delta: timedelta) -> None:
    object.__setattr__(real._deps, "clock", lambda: NOW + delta)


def warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# --- H1(a): expand-only schema --------------------------------------------------------


V1_BACKLOG_DDL = """CREATE TABLE pb_webhook_backlog (
    provider TEXT NOT NULL CHECK (provider = 'lob'),
    event_id TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    received_at TEXT NOT NULL,
    PRIMARY KEY(provider, event_id)
)"""


def columns(database: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in database.execute(f"PRAGMA table_info({table})")]


def indexed_columns(database: sqlite3.Connection, table: str) -> set[tuple[str | None, ...]]:
    found: set[tuple[str | None, ...]] = set()
    for index in database.execute(f"PRAGMA index_list({table})").fetchall():
        info = database.execute("SELECT name FROM pragma_index_info(?)", (index[1],))
        found.add(tuple(row[0] for row in info))
    return found


def test_backlog_schema_stores_indexed_keys_and_a_bounded_unmatched_table(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        database = store._require_open()
        assert columns(database, "pb_webhook_backlog") == [
            "provider",
            "event_id",
            "payload",
            "received_at",
            "letter_id",
            "submission_token",
            "replay_order",
        ]
        assert {
            ("letter_id",),
            ("submission_token",),
            ("received_at",),
            ("replay_order",),
        } <= indexed_columns(database, "pb_webhook_backlog")
        assert columns(database, "pb_webhook_unmatched") == [
            "provider",
            "event_id",
            "payload",
            "reason",
            "received_at",
            "moved_at",
        ]
        assert database.execute(
            "SELECT version FROM pb_store_versions WHERE component='webhook_backlog'"
        ).fetchone() == (3,)


def test_v1_backlog_is_extended_and_backfilled_in_place(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path):
        pass
    rows = [
        ("evt_a", reduced("evt_a", "ltr_a", "tok_a")),
        ("evt_b", reduced("evt_b", "ltr_b", None)),
        (
            "evt_c",
            json.dumps({"id": "evt_c", "event_type": "letter.in_transit", "body": {"id": "ltr_c"}}),
        ),
    ]
    with sqlite3.connect(path) as raw:
        raw.execute("DROP TABLE pb_webhook_backlog")
        raw.execute("DROP TABLE IF EXISTS pb_webhook_unmatched")
        for (name,) in raw.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL "
            "AND tbl_name='pb_submission_cursors'"
        ).fetchall():
            raw.execute(f'DROP INDEX "{name}"')
        raw.execute(V1_BACKLOG_DDL)
        raw.executemany(
            "INSERT INTO pb_webhook_backlog VALUES ('lob', ?, ?, ?)",
            [(event_id, payload, NOW.isoformat()) for event_id, payload in rows],
        )
        raw.execute("UPDATE pb_store_versions SET version=1 WHERE component='webhook_backlog'")
    with SqliteTransactionStore(path) as store:
        database = store._require_open()
        assert database.execute(
            "SELECT version FROM pb_store_versions WHERE component='webhook_backlog'"
        ).fetchone() == (3,)
        assert database.execute(
            "SELECT event_id, payload, letter_id, submission_token, received_at "
            "FROM pb_webhook_backlog ORDER BY rowid"
        ).fetchall() == [
            ("evt_a", rows[0][1], "ltr_a", "tok_a", NOW.isoformat()),
            ("evt_b", rows[1][1], "ltr_b", None, NOW.isoformat()),
            ("evt_c", rows[2][1], "ltr_c", None, NOW.isoformat()),
        ]
        assert database.execute(
            "SELECT event_id FROM pb_webhook_backlog WHERE letter_id='ltr_a'"
        ).fetchall() == [("evt_a",)]
        assert database.execute(
            "SELECT event_id FROM pb_webhook_backlog WHERE submission_token='tok_a'"
        ).fetchall() == [("evt_a",)]
        assert columns(database, "pb_webhook_unmatched")


ODD_ENVELOPES = [
    {"body": {"id": "ltr", "metadata": {"submission_token": "tok"}}},
    {"body": {"id": "ltr"}},
    {"body": {"id": "ltr", "metadata": {"submission_token": None}}},
    {"body": {"id": "ltr", "metadata": "not-a-dict"}},
    {"body": {"id": 7, "metadata": {"submission_token": 9}}},
    {"body": "not-a-dict"},
    {"event_type": "letter.in_transit"},
    [1, 2],
    "text",
]


@pytest.mark.parametrize("envelope", ODD_ENVELOPES, ids=range(len(ODD_ENVELOPES)))
def test_migration_backfill_derives_the_same_keys_as_insert(envelope: object) -> None:
    payload = json.dumps(envelope)
    database = sqlite3.connect(":memory:")
    try:
        database.execute(V1_BACKLOG_DDL)
        database.execute("ALTER TABLE pb_webhook_backlog ADD COLUMN letter_id TEXT")
        database.execute("ALTER TABLE pb_webhook_backlog ADD COLUMN submission_token TEXT")
        database.execute(
            "INSERT INTO pb_webhook_backlog (provider, event_id, payload, received_at) "
            "VALUES ('lob', 'evt', ?, ?)",
            (payload, NOW.isoformat()),
        )
        database.execute(store_module._BACKFILL)
        backfilled = database.execute(
            "SELECT letter_id, submission_token FROM pb_webhook_backlog"
        ).fetchone()
    finally:
        database.close()
    assert backfilled == store_module.backlog_keys(payload)


def test_v2_backlog_fails_closed_without_its_unmatched_table(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path):
        pass
    with sqlite3.connect(path) as raw:
        raw.execute("DROP TABLE pb_webhook_unmatched")
    with (
        pytest.raises(StorageCorruption, match="unsupported webhook backlog schema"),
        SqliteTransactionStore(path),
    ):
        pass


def test_v2_backlog_fails_closed_on_a_changed_index(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path):
        pass
    with sqlite3.connect(path) as raw:
        raw.execute("DROP INDEX pb_webhook_backlog_token")
        raw.execute("CREATE INDEX pb_webhook_backlog_token ON pb_webhook_backlog(event_id)")
    with (
        pytest.raises(StorageCorruption, match="unsupported webhook backlog schema"),
        SqliteTransactionStore(path),
    ):
        pass


@pytest.mark.parametrize(
    "dropped",
    [
        "pb_webhook_backlog_letter",
        "pb_webhook_backlog_token",
        "pb_webhook_backlog_received",
        "pb_submission_cursors_letter",
    ],
)
def test_a_lost_index_is_rebuilt_on_open(tmp_path: Path, dropped: str) -> None:
    """Indexes are derived: a rebuilt cursor table (submission migration) loses one safely."""
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path):
        pass
    with sqlite3.connect(path) as raw:
        raw.execute(f"DROP INDEX {dropped}")
    with SqliteTransactionStore(path) as store:
        found = store._require_open().execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (dropped,)
        )
        assert found.fetchone() is not None


# --- H1(b): matching never scans every row or every case -----------------------------


@pytest.mark.parametrize(
    "name",
    [
        "WEBHOOK_BACKLOG_BATCH_SQL",
        "WEBHOOK_BACKLOG_ROW_SQL",
        "WEBHOOK_BACKLOG_FOR_CASE_SQL",
        "WEBHOOK_BACKLOG_TOKEN_LETTERS_SQL",
        "WEBHOOK_ATTEMPTS_BY_LETTER_SQL",
        "WEBHOOK_ATTEMPT_BY_TOKEN_SQL",
        "WEBHOOK_CASE_BY_AUTHORIZATION_SQL",
    ],
)
def test_every_matching_query_is_answered_from_an_index(tmp_path: Path, name: str) -> None:
    statement: str = getattr(store_module, name)
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        marks = statement.count("?")
        plan = store._require_open().execute("EXPLAIN QUERY PLAN " + statement, ("x",) * marks)
        details = [row[3] for row in plan]
    assert details
    scans = [line for line in details if line.startswith("SCAN") and "USING" not in line]
    assert not scans, details


# --- H1(c)/(e): one bounded pass per sweep, targeted replay per case ----------------


@pytest.mark.parametrize(("cases", "parked"), [(4, 250), (16, 250), (16, 600)])
def test_sweep_reads_the_bounded_batch_never_cases_times_rows(
    tmp_path: Path, cases: int, parked: int
) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        accepted_cases(store, cases)
        park_foreign(store, parked)
    real = app_for(path, FakePaymentClient())
    object.__setattr__(real._deps, "lob", FakeLobClient([]))
    # One batch of rows, plus one emptiness probe before and after each case's dispatch.
    allowed = BATCH + 2 * cases
    spy = watch(real, backlog=allowed)
    try:
        result = real.advance_due()
        assert not result.failures
        assert spy.rows_read["backlog"] <= allowed
        unwatch(real, spy)
        assert len(real._store.pending_webhooks()) == parked
    finally:
        real._store.close()


def test_replay_pass_takes_at_most_the_hundred_oldest_rows(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        park_foreign(store, BATCH + 30)
    real = app_for(path)
    seen: list[str] = []
    original = real._record_verified_webhook

    def record(*args: Any, **kwargs: Any) -> bool:
        seen.append(args[1]["id"])
        return original(*args, **kwargs)

    object.__setattr__(real, "_record_verified_webhook", record)
    try:
        real._replay_webhooks()
        assert seen == [f"evt_foreign_{index}" for index in range(BATCH)]
    finally:
        real._store.close()


def test_batch_pass_never_walks_the_cases(tmp_path: Path) -> None:
    reads: list[int] = []
    for cases in (3, 12):
        path = tmp_path / f"cases-{cases}.sqlite"
        with SqliteTransactionStore(path) as store:
            accepted_cases(store, cases)
            park_foreign(store, BATCH)
        real = app_for(path)
        spy = watch(real, backlog=BATCH, cases=BATCH)
        try:
            real._replay_webhooks()
            reads.append(spy.rows_read["cases"])
            assert spy.rows_read["backlog"] <= BATCH
        finally:
            real._store.close()
    assert reads[0] == reads[1]
    assert reads[1] <= BATCH


def binding_world(path: Path, unrelated: int, early: int = 3, *, anchored: bool = True) -> str:
    """One case whose early events were parked before its cursor lands, among unrelated rows."""
    with SqliteTransactionStore(path) as store:
        case = numbered_case(store, 1)
        park_foreign(store, unrelated)
        for index in range(early):
            park(
                store,
                f"evt_mine_{index}",
                "ltr_mine",
                "tok-1",
                at=NOW + timedelta(days=1, seconds=index),
            )
        land_cursor(
            store,
            case,
            LobResponse(200, "ltr_mine", evidence=LetterEvidence.PRODUCTION)
            if anchored
            else LobResponse(None),
        )
    return case.case_token


def test_binding_one_case_reads_only_its_matching_rows(tmp_path: Path) -> None:
    reads = []
    for unrelated in (40, 400):
        path = tmp_path / f"cases-{unrelated}.sqlite"
        token = binding_world(path, unrelated)
        real = app_for(path, FakePaymentClient())
        object.__setattr__(real._deps, "lob", FakeLobClient([]))
        spy = watch(real, backlog=40)
        try:
            real._dispatch(token)
            reads.append(spy.rows_read["backlog"])
            unwatch(real, spy)
            left = [row[1] for row in real._store.pending_webhooks()]
            assert len(left) == unrelated
            assert all(event.startswith("evt_foreign_") for event in left)
            assert len(real._store.tracking_payloads(token)) == 3
        finally:
            real._store.close()
    assert reads[0] == reads[1]
    assert reads[1] <= 3 * 3 + 1


def test_matching_rows_bind_even_when_the_oldest_hundred_are_foreign(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    token = binding_world(path, BATCH + 50)
    real = app_for(path, FakePaymentClient())
    object.__setattr__(real._deps, "lob", FakeLobClient([]))
    try:
        assert not real.advance_due().failures
        assert len(real._store.tracking_payloads(token)) == 3
        assert len(real._store.pending_webhooks()) == BATCH + 50
    finally:
        real._store.close()


def test_rows_matching_only_by_token_bind_through_the_case_lookup(tmp_path: Path) -> None:
    """The case's frozen token finds its rows even when the oldest hundred are foreign."""
    path = tmp_path / "cases.sqlite"
    token = binding_world(path, BATCH + 50, anchored=False)
    real = app_for(path, FakePaymentClient())
    object.__setattr__(real._deps, "lob", FakeLobClient([]))
    try:
        assert not real.advance_due().failures
        assert len(real._store.tracking_payloads(token)) == 3
        assert len(real._store.pending_webhooks()) == BATCH + 50
    finally:
        real._store.close()


def test_sweep_gives_expired_rows_no_case_claims_their_final_outcome(tmp_path: Path) -> None:
    """Only the sweep's one pass reaches an event that no case holds a key for."""
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        accepted_cases(store, 2)
        park_foreign(store, 3)
    real = app_for(path, FakePaymentClient())
    object.__setattr__(real._deps, "lob", FakeLobClient([]))
    later(real, RETENTION + timedelta(days=1))
    try:
        assert not real.advance_due().failures
        assert real._store.pending_webhooks() == ()
        assert [reason for _event, reason in unmatched(real)] == ["UNMATCHED_EXPIRED"] * 3
    finally:
        real._store.close()


def test_cursor_landed_elsewhere_binds_before_the_same_dispatch_acts(tmp_path: Path) -> None:
    """The replay before dispatch lets that very call start the capture; no Lob call is made."""
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
    real = app_for(path)
    payload, headers = distinct_event(letter_body(), "letter.in_transit", NOW, "evt_early")
    try:
        assert real.webhook("lob", payload, headers)
    finally:
        real._store.close()
    with SqliteTransactionStore(path) as store:
        land_cursor(store, case, LobResponse(None))
    payments = FakePaymentClient()
    lob = FakeLobClient([])
    real = app_for(path, payments)
    object.__setattr__(real._deps, "lob", lob)
    try:
        real._dispatch(case.case_token)
        assert [call[0] for call in payments.calls] == ["retrieve"]
        assert not lob.calls
        assert real._store.pending_webhooks() == ()
        real._dispatch(case.case_token)
        assert payments.captures == 1
        assert not lob.calls
    finally:
        real._store.close()


def test_failing_batch_pass_is_reported_and_never_stops_the_mailing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        accepted_cases(store, 2)
    payments = FakePaymentClient()
    real = app_for(path, payments)
    object.__setattr__(real._deps, "lob", FakeLobClient([]))

    def broken(_limit: int) -> object:
        raise RuntimeError("injected backlog failure")

    monkeypatch.setattr(real._store, "backlog_batch", broken)
    try:
        result = real.advance_due()
        assert [(f.case_id, f.reason) for f in result.failures] == [
            ("webhook-backlog", "internal_error:RuntimeError")
        ]
        assert payments.calls
    finally:
        real._store.close()


def test_live_event_for_a_known_letter_reads_only_its_own_rows(tmp_path: Path) -> None:
    reads = []
    for others in (1, 12):
        path = tmp_path / f"cases-{others}.sqlite"
        with SqliteTransactionStore(path) as store:
            case = numbered_case(store, 1)
            land_cursor(store, case, LobResponse(200, "ltr_mine"))
            for number in range(2, others + 1):
                land_cursor(store, numbered_case(store, number), LobResponse(200, f"ltr_{number}"))
            park_foreign(store, 300)
        real = app_for(path)
        body: dict[str, object] = {"id": "ltr_mine", "metadata": {"submission_token": "tok-1"}}
        payload, headers = distinct_event(body, "letter.in_transit", NOW, "evt_live")
        spy = watch(real, backlog=10)
        try:
            assert real.webhook("lob", payload, headers)
            reads.append((spy.rows_read["backlog"], spy.rows_read["cases"]))
            unwatch(real, spy)
            assert len(real._store.pending_webhooks()) == 300
            assert len(real._store.tracking_payloads(case.case_token)) == 1
        finally:
            real._store.close()
    assert reads[0] == reads[1]
    assert reads[1][0] <= 3


# --- H1(d): every parked event reaches a final outcome ------------------------------


def test_unmatched_event_leaves_the_backlog_after_thirty_days_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        park(store, "evt_old", "ltr_old", "tok_old")
        park(store, "evt_new", "ltr_new", "tok_new", at=NOW + timedelta(days=10))
    real = app_for(path)
    caplog.set_level(logging.WARNING, logger=WARNING_LOGGER)
    try:
        later(real, RETENTION)
        real._replay_webhooks()
        assert [row[1] for row in real._store.pending_webhooks()] == ["evt_old", "evt_new"]
        assert unmatched(real) == []
        later(real, RETENTION + timedelta(seconds=1))
        real._replay_webhooks()
        assert [row[1] for row in real._store.pending_webhooks()] == ["evt_new"]
        assert unmatched(real) == [("evt_old", "UNMATCHED_EXPIRED")]
        (moved,) = real._store.unmatched_webhooks()
        assert json.loads(moved.payload) == json.loads(reduced("evt_old", "ltr_old", "tok_old"))
        assert moved.received_at == NOW
        assert moved.moved_at == NOW + RETENTION + timedelta(seconds=1)
        messages = warnings(caplog)
        assert len(messages) == 1
        assert "evt_old" in messages[0]
        assert "UNMATCHED_EXPIRED" in messages[0]
        # A repeat pass neither moves nor warns again.
        real._replay_webhooks()
        assert len(unmatched(real)) == 1
        assert len(warnings(caplog)) == 1
    finally:
        real._store.close()


def test_moved_event_is_final_and_a_redelivery_is_a_duplicate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "cases.sqlite"
    real = app_for(path)
    body: dict[str, object] = {"id": "ltr_foreign", "metadata": {"submission_token": "tok_foreign"}}
    payload, headers = distinct_event(body, "letter.in_transit", NOW, "evt_once")
    caplog.set_level(logging.WARNING, logger=WARNING_LOGGER)
    try:
        assert real.webhook("lob", payload, headers)
        later(real, RETENTION + timedelta(days=1))
        real._replay_webhooks()
        assert unmatched(real) == [("evt_once", "UNMATCHED_EXPIRED")]
        object.__setattr__(real._deps, "clock", lambda: NOW)
        assert not real.webhook("lob", payload, headers)
        assert real._store.pending_webhooks() == ()
        assert len(unmatched(real)) == 1
        assert len(warnings(caplog)) == 1
    finally:
        real._store.close()


def test_scan_after_a_confirmed_cancel_release_reaches_an_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    wire, payments, path, case, now = fallback_world(tmp_path, monkeypatch, resource(), "confirmed")
    with SqliteTransactionStore(path) as store:
        park(store, "evt_backlogged_scan", ORIGINAL, TOKEN)
    run_dispatches(path, wire, payments, now)
    with SqliteTransactionStore(path) as store:
        assert isinstance(cursor_for(store, case).outcome, Canceled)
    release_time = now + timedelta(hours=1)
    real = clocked_app_for(path, payments, release_time)
    object.__setattr__(real._deps, "lob", wire.client)
    live, headers = distinct_event(
        resource(tracking_events=scan()), "letter.processed_for_delivery", release_time, "evt_live"
    )
    caplog.set_level(logging.WARNING, logger=WARNING_LOGGER)
    try:
        real._replay_webhooks()
        assert real.webhook("lob", live, headers)
        assert real._store.pending_webhooks() == ()
        assert sorted(unmatched(real)) == [
            ("evt_backlogged_scan", "CONTRADICTS_CASE_STATE"),
            ("evt_live", "CONTRADICTS_CASE_STATE"),
        ]
        current = real._store.get_case(case.case_token)
        assert current is not None
        assert current.payment_state is ps.PaymentState.RELEASED
        assert current.letter_state is cs.CaseState.STOPPED_NO_CHARGE
        assert capture_effect_count(real._store, case) == 0
        assert not payments.captures
        messages = warnings(caplog)
        assert len(messages) == 2
        assert all("CONTRADICTS_CASE_STATE" in message for message in messages)
    finally:
        real._store.close()


def test_lobs_deleted_notice_for_a_letter_we_canceled_is_final_without_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    wire, payments, path, case, now = fallback_world(tmp_path, monkeypatch, resource(), "confirmed")
    run_dispatches(path, wire, payments, now)
    release_time = now + timedelta(hours=1)
    real = clocked_app_for(path, payments, release_time)
    payload, headers = distinct_event(
        resource(deleted=True), "letter.deleted", release_time, "evt_deleted"
    )
    caplog.set_level(logging.INFO, logger=WARNING_LOGGER)
    try:
        assert real.webhook("lob", payload, headers)
        assert real._store.pending_webhooks() == ()
        assert unmatched(real) == [("evt_deleted", "CANCEL_CONFIRMED")]
        assert warnings(caplog) == []
        assert [r.levelno for r in caplog.records] == [logging.INFO]
        current = real._store.get_case(case.case_token)
        assert current is not None
        assert current.payment_state is ps.PaymentState.RELEASED
    finally:
        real._store.close()


def test_unmatched_table_is_bounded(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        for index in range(12):
            assert store.move_unmatched(
                "lob",
                f"evt_{index}",
                reduced(f"evt_{index}", f"ltr_{index}", None),
                "UNMATCHED_EXPIRED",
                received_at=NOW,
                moved_at=NOW + timedelta(seconds=index),
                retain=5,
            )
        assert [row.event_id for row in store.unmatched_webhooks()] == [
            f"evt_{index}" for index in range(7, 12)
        ]


# --- L2: a second letter for a bound token is conflicting, never bound --------------


def one_and_two(real: CaseService) -> None:
    for name in ("one", "two"):
        body = letter_body()
        body["id"] = f"ltr_{name}"
        payload, headers = distinct_event(body, "letter.in_transit", NOW, f"evt_{name}")
        assert real.webhook("lob", payload, headers)


def test_second_letter_after_the_token_bound_is_moved_and_never_bound(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        land_cursor(store, case, LobResponse(None))
    real = app_for(path)
    caplog.set_level(logging.WARNING, logger=WARNING_LOGGER)
    try:
        one_and_two(real)
        cursor = real._store.submission_cursor(case.case_token, 1)
        assert cursor is not None
        assert isinstance(cursor.outcome, Accepted)
        assert cursor.outcome.letter_id == "ltr_one"
        assert real._store.pending_webhooks() == ()
        assert unmatched(real) == [("evt_two", "CONFLICTING_LETTER")]
        saved = [json.loads(text) for text in real._store.tracking_payloads(case.case_token)]
        assert [row["letter_id"] for row in saved] == ["ltr_one"]
        assert capture_effect_count(real._store, case) == 1
        messages = warnings(caplog)
        assert len(messages) == 1
        assert "evt_two" in messages[0]
        assert "CONFLICTING_LETTER" in messages[0]
    finally:
        real._store.close()


def test_conflicting_letter_parked_before_the_cursor_lands_is_moved_on_replay(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
    real = app_for(path)
    try:
        one_and_two(real)
        assert len(real._store.pending_webhooks()) == 2
    finally:
        real._store.close()
    with SqliteTransactionStore(path) as store:
        land_cursor(store, case, LobResponse(200, "ltr_one"))
    real = app_for(path)
    try:
        real._replay_webhooks()
        assert real._store.pending_webhooks() == ()
        assert unmatched(real) == [("evt_two", "CONFLICTING_LETTER")]
        saved = [json.loads(text) for text in real._store.tracking_payloads(case.case_token)]
        assert [row["letter_id"] for row in saved] == ["ltr_one"]
        assert capture_effect_count(real._store, case) == 1
    finally:
        real._store.close()


# --- L1: one busy row never aborts dispatch ------------------------------------------


def busy_world(path: Path) -> str:
    with SqliteTransactionStore(path) as store:
        case = numbered_case(store, 1)
        park(store, "evt_busy", "ltr_mine", "tok-1")
        park(store, "evt_ok", "ltr_mine", "tok-1", at=NOW + timedelta(seconds=1))
        land_cursor(store, case, LobResponse(200, "ltr_mine"))
    return case.case_token


def make_busy(real: CaseService, monkeypatch: pytest.MonkeyPatch, busy: set[str]) -> None:
    original = real._store.record_webhook

    def record(provider: str, event_id: str, *args: Any, **kwargs: Any) -> bool:
        if provider == "lob" and event_id in busy:
            raise RevisionConflict("injected")
        return original(provider, event_id, *args, **kwargs)

    monkeypatch.setattr(real._store, "record_webhook", record)


@pytest.mark.parametrize("through", ["pass", "case"])
def test_one_busy_row_does_not_stop_the_next_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, through: str
) -> None:
    path = tmp_path / "cases.sqlite"
    token = busy_world(path)
    real = app_for(path, FakePaymentClient())
    object.__setattr__(real._deps, "lob", FakeLobClient([]))
    make_busy(real, monkeypatch, {"evt_busy"})
    try:
        if through == "pass":
            real._replay_webhooks()
        else:
            real._dispatch(token)
        assert [row[1] for row in real._store.pending_webhooks()] == ["evt_busy"]
        assert len(real._store.tracking_payloads(token)) == 1
    finally:
        real._store.close()


def test_lock_held_elsewhere_leaves_dispatch_and_the_row_intact(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    token = busy_world(path)
    real = app_for(path, FakePaymentClient())
    object.__setattr__(real._deps, "lob", FakeLobClient([]))
    try:
        with SqliteTransactionStore(path) as other, other.submission_ownership():
            assert real._dispatch(token) == 0
            real._replay_webhooks()
        assert len(real._store.pending_webhooks()) == 2
        real._dispatch(token)
        assert real._store.pending_webhooks() == ()
    finally:
        real._store.close()


def stripe_event(case_ref: str) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(
        {
            "id": "evt_stripe_busy",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": case_ref}},
        }
    ).encode()
    stamp = str(int(NOW.timestamp()))
    digest = hmac.new(b"secret", stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return body, {"stripe-signature": f"t={stamp},v1={digest}"}


def test_committed_stripe_webhook_is_not_turned_into_a_conflict_by_a_busy_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cases.sqlite"
    busy_world(path)
    real = app_for(path, FakePaymentClient())
    object.__setattr__(real._deps, "lob", FakeLobClient([]))
    object.__setattr__(real._deps, "stripe_webhook_secret", "secret")
    monkeypatch.setattr("parkingbreaker.case_service.dispatch_once", lambda *a, **k: 0)
    make_busy(real, monkeypatch, {"evt_busy", "evt_ok"})
    payload, headers = stripe_event("auth-1")
    try:
        try:
            assert real.webhook("stripe", payload, headers)
        except ServiceConflict as exc:  # pragma: no cover - the failure being pinned
            pytest.fail(f"committed Stripe webhook became {exc}")
        assert not real.webhook("stripe", payload, headers)
        assert len(real._store.pending_webhooks()) == 2
    finally:
        real._store.close()
