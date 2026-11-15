"""T74: one case can be held, resumed and retried without new money or mail authority.

The create-intent gate inside the mail dispatcher is held for a money review, so these
tests cover research and drafting only; a hold does not yet stop a frozen letter.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient

from paid_draft_support import install_paid_drafter
from parkingbreaker import config
from parkingbreaker.api.app import create_app
from parkingbreaker.case_hold_store import (
    CommandIdReused,
    CommandOutcome,
    HoldCommand,
    HoldReason,
)
from parkingbreaker.case_service import CaseService, ResearchDeps
from parkingbreaker.case_state import CaseState
from parkingbreaker.config import ProviderMode, Settings, build_services
from parkingbreaker.health import AlertKind, StuckWhy, evaluate
from parkingbreaker.health_service import _stuck, observe
from parkingbreaker.lob_submission import Uncertain
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.submission_records import outcome_json
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_config import SECRET, held_case
from test_draft_ownership import HOLDER, CountingDraftLlm
from test_draft_provider_refusal import get_to_drafting

DATABASE = "parkingbreaker.sqlite"


@pytest.fixture
async def runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[CaseService, AsyncClient]]:
    CountingDraftLlm.draft_calls = 0
    CountingDraftLlm.during_call = []
    CountingDraftLlm.failure = None
    monkeypatch.setattr(config, "FixtureLlm", CountingDraftLlm)
    service = build_services(Settings(ProviderMode.FAKE, tmp_path, SECRET, {}))
    assert isinstance(service, CaseService)
    install_paid_drafter(service)
    async with AsyncClient(
        transport=ASGITransport(app=create_app(service, worker_secret=SECRET)),
        base_url="http://fake.test",
    ) as client:
        yield service, client
    service._store.close()


def token_of(service: CaseService, case: str) -> str:
    record = service._store.get_intake(case)
    assert record is not None
    assert record.case_token is not None
    return record.case_token


def state(service: CaseService, token: str) -> CaseState:
    case = service._store.get_case(token)
    assert case is not None
    return case.letter_state


@pytest.mark.anyio
async def test_a_hold_stops_paid_research_and_drafting_until_one_fresh_resume(
    runtime: tuple[CaseService, AsyncClient],
) -> None:
    service, client = runtime
    drafting_case = await held_case(client)
    await get_to_drafting(client, drafting_case)
    drafting = token_of(service, drafting_case)
    outcome, hold = service.hold_case(drafting, HoldReason.INCIDENT, "hold-1")
    assert outcome is CommandOutcome.APPLIED
    assert hold is not None
    assert hold.held
    assert hold.generation == 1
    for _ in range(3):
        service.advance_due()
    service._draft_step(drafting_case)
    service._research(drafting_case)
    assert state(service, drafting) is CaseState.DRAFTING
    assert CountingDraftLlm.draft_calls == 0

    assert service.resume_case(drafting, "resume-1", 0)[0] is CommandOutcome.STALE
    outcome, hold = service.resume_case(drafting, "resume-2", 1)
    assert outcome is CommandOutcome.APPLIED
    assert hold is not None
    assert not hold.held
    assert hold.generation == 2
    assert state(service, drafting) is CaseState.AWAITING_ADOPTION
    assert CountingDraftLlm.draft_calls == 1
    # The same command again returns its first outcome and does no new work.
    assert service.resume_case(drafting, "resume-2", 1)[0] is CommandOutcome.APPLIED
    assert service.resume_case(drafting, "resume-3", 1)[0] is CommandOutcome.STALE
    assert CountingDraftLlm.draft_calls == 1


@pytest.mark.anyio
async def test_a_held_case_still_takes_its_expiry_stop_and_releases_the_hold_once(
    runtime: tuple[CaseService, AsyncClient], tmp_path: Path
) -> None:
    service, client = runtime
    case = await held_case(client)
    await get_to_drafting(client, case)
    token = token_of(service, case)
    assert service.hold_case(token, HoldReason.CODE_FIX, "h1")[0] is CommandOutcome.APPLIED
    later = datetime.now(UTC) + timedelta(hours=2)
    service._deps = replace(service._deps, clock=lambda: later)
    for _ in range(3):
        service.advance_due()
    record = service._store.get_case(token)
    assert record is not None
    assert record.letter_state is CaseState.STOPPED_NO_CHARGE
    assert record.payment_state is PaymentState.RELEASED
    assert CountingDraftLlm.draft_calls == 0
    # A stopped case cannot be resumed or retried.
    assert service.resume_case(token, "r1", 1)[0] is CommandOutcome.REJECTED
    assert service.retry_case(token, "t1")[0] is CommandOutcome.REJECTED
    assert service.hold_case(token, HoldReason.INCIDENT, "h2")[0] is CommandOutcome.REJECTED
    # Its hold cannot be cleared, and has nothing left to pause: it must not alert forever.
    long_after = later + timedelta(days=30)
    snapshot = observe(tmp_path, tmp_path / "recovery", long_after)
    assert snapshot.holds == ()
    assert not any(a.kind is AlertKind.HOLD_REVIEW_DUE for a in evaluate(snapshot, long_after))


@pytest.mark.anyio
async def test_a_repeated_command_id_is_answered_the_same_while_another_call_runs(
    runtime: tuple[CaseService, AsyncClient], tmp_path: Path
) -> None:
    service, client = runtime
    case = await held_case(client)
    await get_to_drafting(client, case)
    token = token_of(service, case)
    assert service.hold_case(token, HoldReason.INCIDENT, "first")[0] is CommandOutcome.APPLIED
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(tmp_path / DATABASE), token],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "owned"
        assert service.hold_case(token, HoldReason.INCIDENT, "first")[0] is CommandOutcome.APPLIED
        with pytest.raises(CommandIdReused):
            service.hold_case(token, HoldReason.CODE_FIX, "first")
        with pytest.raises(ValueError, match="invalid command id"):
            service.hold_case(token, HoldReason.INCIDENT, " ")
        assert service.hold_case(token, HoldReason.INCIDENT, "second")[0] is CommandOutcome.BUSY
        # An open busy id still belongs to its request.
        with pytest.raises(CommandIdReused):
            service.hold_case(token, HoldReason.CODE_FIX, "second")
        with pytest.raises(CommandIdReused):
            service._store.record_busy_hold(
                "second", "another-case", HoldReason.INCIDENT, datetime.now(UTC)
            )
        with pytest.raises(CommandIdReused):
            service.retry_case(token, "second")
    finally:
        holder.kill()
        holder.wait(timeout=30)
    assert service.hold_case(token, HoldReason.INCIDENT, "second")[0] is CommandOutcome.APPLIED


@pytest.mark.anyio
async def test_a_hold_is_busy_while_a_paid_draft_call_is_in_flight(
    runtime: tuple[CaseService, AsyncClient], tmp_path: Path
) -> None:
    service, client = runtime
    case = await held_case(client)
    await get_to_drafting(client, case)
    token = token_of(service, case)
    outcomes: list[CommandOutcome] = []

    def hold_from_elsewhere() -> None:
        def contend() -> None:
            other = SqliteTransactionStore(tmp_path / DATABASE)
            try:
                peer = build_services(
                    Settings(ProviderMode.FAKE, tmp_path, SECRET, {}), store=other
                )
                assert isinstance(peer, CaseService)
                outcomes.append(peer.hold_case(token, HoldReason.INCIDENT, "busy-1")[0])
            finally:
                other.close()

        worker = threading.Thread(target=contend)
        worker.start()
        worker.join(timeout=30)

    CountingDraftLlm.during_call.append(hold_from_elsewhere)
    service._draft_step(case)
    assert outcomes == [CommandOutcome.BUSY]
    assert state(service, token) is CaseState.AWAITING_ADOPTION
    hold = service._store.case_hold(token)
    assert hold is None or not hold.held
    # A busy hold keeps its command id open: the same command takes effect once nothing runs.
    assert service.hold_case(token, HoldReason.INCIDENT, "busy-1")[0] is CommandOutcome.APPLIED
    assert service.hold_case(token, HoldReason.INCIDENT, "busy-1")[0] is CommandOutcome.APPLIED
    busy = service._store._require_open().execute("SELECT COUNT(*) FROM pb_case_hold_busy")
    assert busy.fetchone() == (1,)


@pytest.mark.anyio
async def test_retry_leaves_a_hold_in_place_and_runs_eligible_work_when_not_held(
    runtime: tuple[CaseService, AsyncClient],
) -> None:
    service, client = runtime
    case = await held_case(client)
    await get_to_drafting(client, case)
    token = token_of(service, case)
    assert service.hold_case(token, HoldReason.PROVIDER_OUTAGE, "h1")[0] is CommandOutcome.APPLIED
    before = service._store.case_hold(token)
    # Retry is applied but cannot lift the hold: paid drafting stays paused.
    assert service.retry_case(token, "t1")[0] is CommandOutcome.APPLIED
    hold = service._store.case_hold(token)
    assert hold == before
    assert hold is not None
    assert hold.held
    assert CountingDraftLlm.draft_calls == 0
    assert state(service, token) is CaseState.DRAFTING
    assert service.resume_case(token, "r1", hold.generation)[0] is CommandOutcome.APPLIED
    assert CountingDraftLlm.draft_calls == 1
    assert service.retry_case(token, "t2")[0] is CommandOutcome.APPLIED
    assert CountingDraftLlm.draft_calls == 1
    assert state(service, token) is CaseState.AWAITING_ADOPTION


@pytest.mark.anyio
async def test_a_hold_survives_restart_and_its_command_log_is_append_only(
    runtime: tuple[CaseService, AsyncClient], tmp_path: Path
) -> None:
    service, client = runtime
    case = await held_case(client)
    token = token_of(service, case)
    service.hold_case(token, HoldReason.DATA_CHECK, "h1")
    service._store.close()
    reopened = SqliteTransactionStore(tmp_path / DATABASE)
    try:
        hold = reopened.case_hold(token)
        assert hold is not None
        assert hold.held
        assert hold.reason_code is HoldReason.DATA_CHECK
        database = reopened._require_open()
        for statement in (
            "UPDATE pb_case_hold_commands SET outcome='busy'",
            "DELETE FROM pb_case_hold_commands",
            "DELETE FROM pb_case_holds",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                database.execute(statement)
    finally:
        reopened.close()
    service._store = SqliteTransactionStore(tmp_path / DATABASE)


@pytest.mark.anyio
async def test_health_reports_a_held_case_by_its_review_deadline_and_an_owned_one_as_working(
    runtime: tuple[CaseService, AsyncClient], tmp_path: Path
) -> None:
    service, client = runtime
    case = await held_case(client)
    await get_to_drafting(client, case)
    token = token_of(service, case)
    later = datetime.now(UTC) + timedelta(hours=1)
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(tmp_path / DATABASE), token],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "owned"
        # A live drafting owner means the case is being worked on, whatever its age.
        assert observe(tmp_path, tmp_path / "recovery", later).stuck == ()
    finally:
        holder.kill()
        holder.wait(timeout=30)
    snapshot = observe(tmp_path, tmp_path / "recovery", later)
    assert snapshot.stuck is not None
    assert [s.case_token for s in snapshot.stuck] == [token]
    assert service.hold_case(token, HoldReason.INCIDENT, "h1")[0] is CommandOutcome.APPLIED
    hold = service._store.case_hold(token)
    assert hold is not None
    assert hold.review_due_at is not None
    due = max(later, hold.review_due_at)
    snapshot = observe(tmp_path, tmp_path / "recovery", due)
    assert snapshot.stuck == ()
    assert snapshot.holds is not None
    assert [h.case_token for h in snapshot.holds] == [token]
    alerts = {(a.kind, a.subject) for a in evaluate(snapshot, due)}
    assert (AlertKind.HOLD_REVIEW_DUE, token) in alerts
    assert not any(kind is AlertKind.CASE_STUCK for kind, _ in alerts)
    before_due = hold.review_due_at - timedelta(minutes=1)
    assert not any(
        a.kind is AlertKind.HOLD_REVIEW_DUE
        for a in evaluate(observe(tmp_path, tmp_path / "recovery", before_due), before_due)
    )


@pytest.mark.anyio
async def test_a_hold_recorded_while_waiting_for_drafting_ownership_still_wins(
    runtime: tuple[CaseService, AsyncClient],
) -> None:
    service, client = runtime
    case = await held_case(client)
    await get_to_drafting(client, case)
    token = token_of(service, case)
    assert service.hold_case(token, HoldReason.INCIDENT, "h1")[0] is CommandOutcome.APPLIED
    # The owned step re-reads the hold under ownership, as if the hold landed in between.
    with service._store.draft_ownership(token) as owned:
        assert owned
        service._draft_step_owned(case)
    assert CountingDraftLlm.draft_calls == 0
    assert state(service, token) is CaseState.DRAFTING


@pytest.mark.anyio
async def test_a_hold_recorded_while_research_waits_for_ownership_stops_paid_research(
    runtime: tuple[CaseService, AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, client = runtime
    case = await held_case(client)
    token = token_of(service, case)
    assert state(service, token) is CaseState.RESEARCHING
    assert service.hold_case(token, HoldReason.INCIDENT, "h1")[0] is CommandOutcome.APPLIED
    assert service._store.get_research(case) is None
    called: list[str] = []
    real = service._deps.research

    def research_for(subject: str) -> ResearchDeps:
        called.append(subject)
        assert real is not None
        return real

    monkeypatch.setattr(
        service, "_deps", replace(service._deps, research=None, research_for_subject=research_for)
    )
    # Inside ownership, as if the hold landed between the outer check and the lock.
    with service._store.draft_ownership(token) as owned:
        assert owned
        service._research_owned(case)
    service._research(case)
    assert called == []
    assert state(service, token) is CaseState.RESEARCHING


@pytest.mark.anyio
async def test_a_command_id_reused_for_a_different_request_is_refused(
    runtime: tuple[CaseService, AsyncClient],
) -> None:
    service, client = runtime
    first = token_of(service, await held_case(client))
    assert service.hold_case(first, HoldReason.INCIDENT, "c1")[0] is CommandOutcome.APPLIED
    store = service._store
    now = datetime.now(UTC)
    with pytest.raises(CommandIdReused):
        store.record_hold_command(
            "c1",
            "another-case",
            HoldCommand.HOLD,
            now,
            expected_generation=None,
            reason=HoldReason.INCIDENT,
            review_due_at=now,
        )
    with pytest.raises(CommandIdReused):
        service.hold_case(first, HoldReason.CODE_FIX, "c1")
    hold = store.case_hold(first)
    assert hold is not None
    with pytest.raises(CommandIdReused):
        service.resume_case(first, "c1", hold.generation)
    assert store.case_hold("another-case") is None
    assert service.hold_case(first, HoldReason.INCIDENT, "c1")[0] is CommandOutcome.APPLIED
    assert store.case_hold(first) == hold
    # The same binding holds inside the busy transaction, if another request won the id first.
    busy_rows = store._require_open().execute("SELECT COUNT(*) FROM pb_case_hold_busy")
    assert busy_rows.fetchone() == (0,)
    with pytest.raises(CommandIdReused):
        store.record_busy_hold("c1", "another-case", HoldReason.INCIDENT, now)
    decided = store.record_busy_hold("c1", first, HoldReason.INCIDENT, now)
    assert decided is CommandOutcome.APPLIED
    busy_rows = store._require_open().execute("SELECT COUNT(*) FROM pb_case_hold_busy")
    assert busy_rows.fetchone() == (0,)


def test_a_held_case_still_alerts_for_overdue_safety_work_but_not_its_paused_steps() -> None:
    now = datetime(2026, 11, 3, 12, tzinfo=UTC)
    old = (now - timedelta(days=2)).isoformat()
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE pb_payment_cursor (case_token TEXT PRIMARY KEY, payload TEXT);"
        "CREATE TABLE pb_submission_cursors (case_token TEXT, attempt INTEGER, "
        "next_at TEXT, outcome_json TEXT);"
        "CREATE TABLE pb_pending_effects (effect_id TEXT, case_token TEXT, machine TEXT,"
        " effect TEXT, created_at TEXT, outcome TEXT);"
        "CREATE TABLE pb_cases (case_token TEXT, authorized_at TEXT, letter_state TEXT, "
        "attempt INTEGER);"
        "CREATE TABLE pb_case_history (case_token TEXT, machine TEXT, occurred_at TEXT);"
        "CREATE TABLE pb_case_holds (case_token TEXT, held INTEGER);"
    )
    db.execute(
        "INSERT INTO pb_pending_effects VALUES ('e1', 'releasing', 'letter', 'RELEASE_HOLD', ?,"
        " NULL)",
        (old,),
    )
    db.executemany(
        "INSERT INTO pb_cases VALUES (?, ?, ?, 1)",
        [
            ("paused", old, "DRAFTING"),
            ("mailing", old, "SUBMITTING"),
            ("awaiting_lookup", old, "SUBMITTING"),
        ],
    )
    # A hold does not pause mail, so a held case stuck in submission still alerts.
    db.executemany(
        "INSERT INTO pb_case_holds VALUES (?, 1)", [("releasing",), ("paused",), ("mailing",)]
    )
    # Lookup not due yet: still being worked, whatever the time since the last transition.
    db.execute(
        "INSERT INTO pb_submission_cursors VALUES ('awaiting_lookup', 1, ?, ?)",
        ((now + timedelta(minutes=30)).isoformat(), outcome_json(Uncertain("timeout"))),
    )
    found = {item.case_token: item.why for item in _stuck(db, now)}
    assert found == {
        "releasing": StuckWhy.EFFECT_OVERDUE,
        "mailing": StuckWhy.AUTOMATED_STEP_OVERDUE,
    }
