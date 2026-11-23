"""T75 feed privacy, process roles, stored IDs, snapshot and freshness boundaries."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx2 import ASGITransport, AsyncClient

from mail_samples import make_request
from parkingbreaker import overseer_feed as feed_module
from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app, overseer_options
from parkingbreaker.case_hold_store import CommandOutcome, HoldCommand, HoldReason
from parkingbreaker.case_service import CaseService
from parkingbreaker.config import ProviderMode, Settings, build_services
from parkingbreaker.health import Alert, AlertKind, HealthSnapshot, Severity
from parkingbreaker.lob_submission import (
    Accepted,
    Canceled,
    LetterEvidence,
    NotSent,
    Rejected,
    SubmissionOutcome,
    Uncertain,
)
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.oplog.ai_journal import AiCall, AiCallStatus, AiWorker
from parkingbreaker.overseer_feed import FreshnessState, ops_id
from parkingbreaker.submission_records import outcome_json
from parkingbreaker.sweep_run_store import SweepOutcome
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_config import ALICE, SECRET, held_case

KEY = bytes(range(32))
FEED_SECRET = "overseer-feed-secret-0123456789"
FEED = {"Authorization": f"Bearer {FEED_SECRET}"}
VALUES = {"PB_OVERSEER_FEED_SECRET": FEED_SECRET, "PB_OVERSEER_OPS_ID_KEY": KEY.hex()}
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
PRIVATE = "Private-Alex-123-Example-Street-customer-secret"


def bare_case(store: SqliteTransactionStore, number: int = 1, at: datetime = NOW) -> str:
    workspace = f"private-workspace-{number}"
    store.save_workspace(workspace, Workspace(notes=PRIVATE, position=PRIVATE), at)
    return store.start_case(
        workspace, f"private-authorization-{number}", at, at + timedelta(days=5)
    ).case_token


def seed_submission_cursor(
    store: SqliteTransactionStore,
    token: str,
    attempt: int,
    outcome: SubmissionOutcome,
    due: datetime,
) -> None:
    store.prepare(token, attempt, make_request(token=f"synthetic-attempt-{attempt}"), NOW)
    store._require_open().execute(
        "INSERT INTO pb_submission_cursors VALUES (?, ?, ?, 1, ?, 0, 1, NULL, 1, ?)",
        (token, attempt, NOW.isoformat(), due.isoformat(), outcome_json(outcome)),
    )


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[tuple[CaseService, AsyncClient]]:
    service = build_services(Settings(ProviderMode.FAKE, tmp_path, SECRET, {}))
    assert isinstance(service, CaseService)
    app = create_app(service, worker_secret=SECRET, **overseer_options(VALUES, service, SECRET))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://feed.test") as c:
            yield service, c
    finally:
        service._store.close()


@pytest.mark.anyio
async def test_feed_schema_never_includes_customer_ids_text_or_saved_paid_output(
    runtime: tuple[CaseService, AsyncClient],
) -> None:
    service, client = runtime
    case_id = await held_case(client)
    record = service._store.get_intake(case_id)
    assert record is not None
    assert record.case_token is not None
    token = record.case_token
    service._store._require_open().execute(
        "INSERT INTO pb_draft_attempts VALUES (?, 1, 1, ?, ?, 'returned', ?)",
        (token, "a" * 64, NOW.isoformat(), json.dumps({"letter_text": PRIVATE})),
    )
    response = await client.get("/internal/overseer/feed", headers=FEED)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == set(s.OverseerFeed.model_fields)
    (row,) = body["cases"]
    assert set(row) == set(s.OverseerCase.model_fields)
    assert row["ops_id"] == ops_id(KEY, token)
    planted = (
        token,
        case_id,
        record.workspace_token,
        PRIVATE,
        "Alex Example",
        "415-555-0100",
        "alex@example.test",
        "123 Example Street",
        "94103",
        "SYN-0001",
        "SYN123",
        "The meter was broken",
        "photo_",
        "alice",
    )
    assert not any(value in response.text for value in planted)


@pytest.mark.anyio
async def test_feed_has_its_own_credential_and_rejects_unknown_pagination(
    runtime: tuple[CaseService, AsyncClient],
) -> None:
    _service, client = runtime
    for headers in ({}, {"Authorization": f"Bearer {SECRET}"}, ALICE):
        assert (await client.get("/internal/overseer/feed", headers=headers)).status_code == 401
    for query in ("limit=0", "limit=101", "after=" + PRIVATE, "unknown=" + PRIVATE):
        response = await client.get("/internal/overseer/feed?" + query, headers=FEED)
        assert response.status_code == 422
        assert PRIVATE not in response.text


@pytest.mark.parametrize(
    "authorization",
    [b"Bearer private-non-ascii-\xff", b"Bearer private-control-\x01", b"Bearer private-tab-\t"],
    ids=["non-ascii", "control", "tab"],
)
@pytest.mark.anyio
async def test_malformed_feed_bearer_is_refused_without_echoing_or_logging_it(
    runtime: tuple[CaseService, AsyncClient],
    authorization: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _service, client = runtime
    response = await client.get(
        "/internal/overseer/feed", headers=[(b"authorization", authorization)]
    )
    assert response.status_code == 401
    assert "private-" not in response.text
    assert "private-" not in caplog.text


@pytest.mark.parametrize(
    ("outcome", "pending"),
    [
        (Accepted("private-letter", evidence=LetterEvidence.PRODUCTION), False),
        (Accepted("private-letter", evidence=LetterEvidence.EXPIRY_FALLBACK), False),
        (Rejected(422, PRIVATE), False),
        (NotSent(), False),
        (Canceled("private-letter"), False),
        (Accepted("private-letter", evidence=None), True),
        (Uncertain(PRIVATE), True),
    ],
    ids=["accepted", "fallback", "rejected", "not-sent", "canceled", "legacy", "uncertain"],
)
def test_submission_due_time_requires_current_pending_evidence(
    tmp_path: Path,
    outcome: SubmissionOutcome,
    pending: bool,
) -> None:
    due = NOW + timedelta(minutes=20)
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        store.configure_ops_ids(KEY)
        token = bare_case(store)
        seed_submission_cursor(store, token, 1, outcome, due)
        (row,) = store.overseer_page(NOW, None, 10).cases
        assert row.submission_due_at == (due if pending else None)


@pytest.mark.parametrize(
    ("current", "pending"),
    [(Uncertain(PRIVATE), True), (Accepted("private-current-letter"), False), (None, False)],
    ids=["pending-current", "completed-current", "no-current-cursor"],
)
def test_attempt_two_submission_due_time_excludes_the_prior_attempt(
    tmp_path: Path,
    current: SubmissionOutcome | None,
    pending: bool,
) -> None:
    due = NOW + timedelta(minutes=30)
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        store.configure_ops_ids(KEY)
        token = bare_case(store)
        seed_submission_cursor(store, token, 1, Uncertain(PRIVATE), NOW - timedelta(hours=2))
        store._require_open().execute("UPDATE pb_cases SET attempt=2 WHERE case_token=?", (token,))
        if current is not None:
            seed_submission_cursor(store, token, 2, current, due)
        (row,) = store.overseer_page(NOW, None, 10).cases
        assert row.attempt == 2
        assert row.submission_due_at == (due if pending else None)
        assert row.stuck is None


@pytest.mark.anyio
async def test_both_process_roles_index_new_cases_but_customer_never_serves_feed(
    tmp_path: Path,
) -> None:
    customer = build_services(Settings(ProviderMode.FAKE, tmp_path, SECRET, {}))
    worker = build_services(Settings(ProviderMode.FAKE, tmp_path, SECRET, {}))
    assert isinstance(customer, CaseService)
    assert isinstance(worker, CaseService)
    try:
        customer_options = overseer_options(VALUES, customer, SECRET, "customer")
        worker_options = overseer_options(VALUES, worker, SECRET, "worker")
        assert customer_options == {}
        # Creation occurs after both independently opened processes finish startup.
        token = bare_case(customer._store)
        assert worker._store.case_for_ops_id(ops_id(KEY, token)) == token
        for role, service, options, expected in (
            ("customer", customer, customer_options, 404),
            ("worker", worker, worker_options, 200),
        ):
            app = create_app(service, worker_secret=SECRET, role=role, **options)  # type: ignore[arg-type]
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://role.test"
            ) as client:
                result = await client.get("/internal/overseer/feed", headers=FEED)
                assert result.status_code == expected
                if expected == 200:
                    assert result.json()["cases"][0]["ops_id"] == ops_id(KEY, token)
    finally:
        customer._store.close()
        worker._store.close()


def test_configuration_validates_before_provisioning_and_supports_key_only_case_writers(
    tmp_path: Path,
) -> None:
    service = build_services(Settings(ProviderMode.FAKE, tmp_path, SECRET, {}))
    assert isinstance(service, CaseService)
    try:
        assert overseer_options({}, service, SECRET) == {}
        for values in (
            {"PB_OVERSEER_FEED_SECRET": FEED_SECRET},
            {**VALUES, "PB_OVERSEER_FEED_SECRET": SECRET},
            {**VALUES, "PB_OVERSEER_FEED_SECRET": "short"},
            {**VALUES, "PB_OVERSEER_FEED_SECRET": "space is forbidden"},
            {**VALUES, "PB_OVERSEER_OPS_ID_KEY": "zz"},
            {**VALUES, "PB_OVERSEER_OPS_ID_KEY": "ab" * 8},
            {**VALUES, "PB_OVERSEER_OPS_ID_KEY_VERSION": "65536"},
            {**VALUES, "PB_OVERSEER_OPS_ID_KEY_VERSION": "01"},
        ):
            with pytest.raises(RuntimeError):
                overseer_options(values, service, SECRET)
        assert service._store._require_open().execute(
            "SELECT COUNT(*) FROM pb_overseer_key_versions"
        ).fetchone() == (0,)
        assert (
            overseer_options({"PB_OVERSEER_OPS_ID_KEY": KEY.hex()}, service, SECRET, "customer")
            == {}
        )
        token = bare_case(service._store)
        assert service._store.case_for_ops_id(ops_id(KEY, token)) == token
        for secret in (None, "short", SECRET, "bad\nheader-value-1234567"):
            with pytest.raises(RuntimeError, match="overseer_feed_secret_invalid"):
                create_app(
                    service,
                    worker_secret=SECRET,
                    overseer_feed=service.overseer_feed,
                    overseer_feed_secret=secret,
                )
    finally:
        service._store.close()


def test_identifiers_backfill_persist_and_walk_multiple_pages_across_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        old = bare_case(store)
        store.configure_ops_ids(KEY)
        tokens = [old, *(bare_case(store, n) for n in range(2, 8))]
        expected = sorted(ops_id(KEY, token) for token in tokens)
        seen: list[str] = []
        after = None
        while True:
            page = store.overseer_page(NOW, after, 2)
            seen.extend(case.ops_id for case in page.cases)
            if page.next_after is None:
                break
            after = page.next_after
        assert seen == expected
        assert len(set(seen)) == 7
        indexes = store._require_open().execute("PRAGMA index_list(pb_case_ops_ids)").fetchall()
        assert any(
            unique
            and store._require_open().execute(f'PRAGMA index_info("{name}")').fetchall()
            == [(0, 1, "ops_id")]
            for _, name, unique, *_ in indexes
        )
    with SqliteTransactionStore(path) as store:
        store.configure_ops_ids(KEY)
        assert [case.ops_id for case in store.overseer_page(NOW, None, 100).cases] == expected
        with pytest.raises(StorageCorruption, match="already bound"):
            store.configure_ops_ids(bytes(reversed(KEY)), 1)
        with pytest.raises(StorageCorruption, match="already bound"):
            store.configure_ops_ids(KEY, 2)
        rotated = bytes(reversed(KEY))
        store.configure_ops_ids(rotated, 2)
        new = bare_case(store, 8)
        assert store.case_for_ops_id(ops_id(KEY, old)) == old
        assert store.case_for_ops_id(ops_id(rotated, new, 2)) == new
    with SqliteTransactionStore(path) as store:
        store.configure_ops_ids(rotated, 2)
        assert len(store.overseer_page(NOW, None, 100).cases) == 8


@pytest.mark.anyio
async def test_request_uses_a_read_only_connection_and_changes_no_database_rows(
    runtime: tuple[CaseService, AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client = runtime
    bare_case(service._store)
    db = service._store._require_open()
    before, changes = tuple(db.iterdump()), db.total_changes
    original = feed_module._health

    def read_only(database: sqlite3.Connection, path: Path, now: datetime) -> HealthSnapshot:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            database.execute("INSERT INTO pb_overseer_key_versions VALUES (99, 'forbidden')")
        return original(database, path, now)

    monkeypatch.setattr(feed_module, "_health", read_only)
    assert (await client.get("/internal/overseer/feed", headers=FEED)).status_code == 200
    assert (tuple(db.iterdump()), db.total_changes) == (before, changes)


def test_case_hold_spend_and_t24_share_one_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as reader, SqliteTransactionStore(path) as writer:
        reader.configure_ops_ids(KEY)
        writer.configure_ops_ids(KEY)
        token = bare_case(writer)
        original = feed_module._health

        def concurrent_write(
            database: sqlite3.Connection, path: Path, now: datetime
        ) -> HealthSnapshot:
            writer.record_hold_command(
                "new-hold",
                token,
                HoldCommand.HOLD,
                NOW,
                expected_generation=None,
                reason=HoldReason.INCIDENT,
                review_due_at=NOW - timedelta(seconds=1),
            )
            reservation = writer.reserve_ai_spend(token, AiWorker.DRAFTING, 50, NOW, 20)
            assert reservation is not None
            writer.settle_ai_spend(token, AiWorker.DRAFTING, reservation[0], 7)
            return original(database, path, now)

        monkeypatch.setattr(feed_module, "_health", concurrent_write)
        page = reader.overseer_page(NOW, None, 10)
        (row,) = page.cases
        assert not row.held
        assert row.ai_settled_cents == 0
        assert AlertKind.HOLD_REVIEW_DUE not in {alert.kind for alert in page.alerts}
        monkeypatch.setattr(feed_module, "_health", original)
        newer = reader.overseer_page(NOW, None, 10)
        assert newer.cases[0].held
        assert newer.cases[0].ai_settled_cents == 7
        assert AlertKind.HOLD_REVIEW_DUE in {alert.kind for alert in newer.alerts}


@pytest.mark.parametrize(
    ("outcome", "age", "state", "error"),
    [
        (None, 0, FreshnessState.IN_PROGRESS, None),
        (None, 16, FreshnessState.INTERRUPTED, "sweep_interrupted"),
        (SweepOutcome.OK, 0, FreshnessState.FRESH, None),
        (SweepOutcome.OK, 4, FreshnessState.STALE, None),
        (SweepOutcome.PARTIAL, 0, FreshnessState.PARTIAL, "sweep_partial"),
        (SweepOutcome.FAILED, 0, FreshnessState.FAILED, "sweep_failed"),
    ],
)
def test_freshness_distinguishes_current_outcomes_and_old_success(
    tmp_path: Path,
    outcome: SweepOutcome | None,
    age: int,
    state: FreshnessState,
    error: str | None,
) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        assert store.overseer_page(NOW, None, 10).freshness.state is FreshnessState.NO_RUN
        # A prior success must not hide a more recent failed/partial/unfinished run.
        old = store.start_sweep_run(NOW - timedelta(hours=1))
        store.finish_sweep_run(old, NOW - timedelta(hours=1), SweepOutcome.OK, 0, 0)
        run = store.start_sweep_run(NOW - timedelta(minutes=age, seconds=30))
        if outcome is not None:
            store.finish_sweep_run(run, NOW - timedelta(minutes=age, seconds=20), outcome, 1, 0)
        freshness = store.overseer_page(NOW, None, 10).freshness
        assert freshness.state is state
        assert freshness.error_class == error
        assert freshness.last_outcome is outcome
        assert freshness.unfinished_runs == (outcome is None)
        assert freshness.stale is (state is not FreshnessState.FRESH)


@pytest.mark.anyio
async def test_global_t24_alerts_are_current_closed_counts_and_drop_subjects_and_details(
    runtime: tuple[CaseService, AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, client = runtime
    monkeypatch.setattr(
        "parkingbreaker.overseer_feed.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=1, total=100),
    )
    response = await client.get("/internal/overseer/feed", headers=FEED)
    assert response.status_code == 200
    kinds = {alert["kind"] for alert in response.json()["alerts"]}
    assert {"scheduler_stale", "sweep_failing", "disk_low", "backup_stale"} <= kinds
    monkeypatch.setattr(
        feed_module,
        "evaluate",
        lambda _snapshot, _now: (
            Alert(AlertKind.CASE_STUCK, Severity.CRITICAL, PRIVATE, PRIVATE),
            Alert(AlertKind.CASE_STUCK, Severity.CRITICAL, "other-private-id", PRIVATE),
            Alert(AlertKind.OBSERVATION_UNKNOWN, Severity.WARNING, "backup", PRIVATE),
        ),
    )
    response = await client.get("/internal/overseer/feed", headers=FEED)
    assert response.status_code == 200
    assert PRIVATE not in response.text
    assert response.json()["alerts"] == [
        {"kind": "case_stuck", "severity": "critical", "domain": "cases", "count": 2},
        {"kind": "observation_unknown", "severity": "warning", "domain": "backup", "count": 1},
    ]


@pytest.mark.anyio
async def test_spend_counts_case_and_intake_costs_without_merging_unknown_reservations(
    runtime: tuple[CaseService, AsyncClient],
) -> None:
    service, client = runtime
    case_id = await held_case(client)
    record = service._store.get_intake(case_id)
    assert record is not None
    assert record.case_token is not None
    store = service._store
    baseline = service.overseer_feed(None, 10).cases[0]
    for subject, worker, settled, reserved in (
        (record.case_token, AiWorker.DRAFTING, 7, 20),
        (case_id, AiWorker.INTAKE_READING, 3, 10),
    ):
        first = store.reserve_ai_spend(subject, worker, 150, NOW, 20)
        assert first is not None
        store.settle_ai_spend(subject, worker, first[0], settled)
        assert store.reserve_ai_spend(subject, worker, 150, NOW, reserved) is not None
        assert store.try_append_ai_call(
            subject,
            AiCall(
                worker,
                "fixture",
                AiCallStatus.FALLBACK_AFTER_FAILURE,
                None,
                None,
                None,
            ),
            NOW,
        )
    assert (
        service.hold_case(record.case_token, HoldReason.PROVIDER_OUTAGE, "h1")[0]
        is CommandOutcome.APPLIED
    )
    row = service.overseer_feed(None, 10).cases[0]
    assert row.ai_settled_cents == baseline.ai_settled_cents + 10
    assert row.ai_unsettled_reserved_cents == baseline.ai_unsettled_reserved_cents + 30
    assert row.ai_unknown_cost_calls == baseline.ai_unknown_cost_calls + 2
    assert (row.held, row.hold_generation, row.hold_reason) == (True, 1, HoldReason.PROVIDER_OUTAGE)


@pytest.mark.parametrize("source", ["letter", "payment", "identifier", "hold", "ai_call"])
@pytest.mark.anyio
async def test_corrupt_stored_values_fail_closed_without_private_output_or_log(
    runtime: tuple[CaseService, AsyncClient],
    source: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, client = runtime
    token = bare_case(service._store)
    db = service._store._require_open()
    if source in {"letter", "payment"}:
        db.execute(f"UPDATE pb_cases SET {source}_state=? WHERE case_token=?", (PRIVATE, token))
    elif source == "identifier":
        db.execute("DROP TRIGGER pb_case_ops_ids_no_update")
        db.execute("UPDATE pb_case_ops_ids SET ops_id=? WHERE case_token=?", (PRIVATE, token))
    elif source == "hold":
        db.execute(
            "INSERT INTO pb_case_holds VALUES (?, 1, 1, ?, ?, ?)",
            (token, PRIVATE, NOW.isoformat(), NOW.isoformat()),
        )
    else:
        db.execute(
            "INSERT INTO pb_ai_calls VALUES (?, 1, ?, ?)",
            (token, json.dumps({"cost_cents": PRIVATE}), NOW.isoformat()),
        )
    response = await client.get("/internal/overseer/feed", headers=FEED)
    assert response.status_code == 503
    assert response.json() == {"detail": "overseer_feed_unavailable"}
    assert PRIVATE not in response.text
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize(
    ("letter", "payment", "expected"),
    [
        ("STOPPED_NO_CHARGE", "RELEASED", "stopped_no_charge"),
        ("IN_TRANSIT", "CAPTURE_RETRYING", "payment_retrying"),
        ("IN_TRANSIT", "WRITTEN_OFF", "payment_written_off"),
    ],
)
@pytest.mark.anyio
async def test_case_error_class_reports_only_observed_closed_state_categories(
    runtime: tuple[CaseService, AsyncClient],
    letter: str,
    payment: str,
    expected: str,
) -> None:
    service, client = runtime
    token = bare_case(service._store)
    service._store._require_open().execute(
        "UPDATE pb_cases SET letter_state=?, payment_state=? WHERE case_token=?",
        (letter, payment, token),
    )
    response = await client.get("/internal/overseer/feed", headers=FEED)
    assert response.status_code == 200
    assert response.json()["cases"][0]["error_class"] == expected


@pytest.mark.anyio
async def test_feed_reports_unanchored_and_desynced_cases_as_aggregates(
    runtime: tuple[CaseService, AsyncClient],
) -> None:
    service, client = runtime
    old = datetime.now(UTC) - timedelta(hours=25)
    db = service._store._require_open()
    waiting = bare_case(service._store, 71, old)
    anchored = bare_case(service._store, 72, old)
    for token in (waiting, anchored):
        db.execute("UPDATE pb_cases SET letter_state='SUBMITTING' WHERE case_token=?", (token,))
    seed_submission_cursor(service._store, anchored, 1, Accepted(PRIVATE), datetime.now(UTC))
    db.execute(
        "INSERT INTO pb_webhook_unmatched VALUES ('lob', ?, ?, 'CONFLICTING_LETTER', ?, ?)",
        (PRIVATE, json.dumps({"private": PRIVATE}), NOW.isoformat(), NOW.isoformat()),
    )
    response = await client.get("/internal/overseer/feed", headers=FEED)
    assert response.status_code == 200
    assert PRIVATE not in response.text
    alerts = {row["kind"]: row for row in response.json()["alerts"]}
    assert alerts["webhook_desync"] == {
        "kind": "webhook_desync",
        "severity": "critical",
        "domain": "webhooks",
        "count": 1,
    }
    assert alerts["submission_unanchored"] == {
        "kind": "submission_unanchored",
        "severity": "warning",
        "domain": "cases",
        "count": 1,
    }
