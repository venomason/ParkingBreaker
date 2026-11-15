"""T40: at most one paid drafting call per case at a time, bounded after a crash mid-call."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest
from httpx2 import ASGITransport, AsyncClient

from paid_draft_support import install_paid_drafter
from parkingbreaker import config
from parkingbreaker.api.app import create_app
from parkingbreaker.appeal.drafting import Draft, DraftResult
from parkingbreaker.case_service import CaseService, _RecordedDrafter
from parkingbreaker.case_state import CaseState
from parkingbreaker.config import ProviderMode, Settings, build_services
from parkingbreaker.config_fakes import FixtureLlm
from parkingbreaker.draft_ownership_store import (
    _V1_ATTEMPTS_DDL,
    DRAFT_CALL_LIMIT,
    DRAFT_RUN_LIMIT,
    DraftOutcome,
    DraftRunRefused,
)
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import LlmProviderError, Usage
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_config import SECRET, held_case
from test_draft_provider_refusal import get_to_drafting

DATABASE = "parkingbreaker.sqlite"
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
SHA = "a" * 64


class CountingDraftLlm(FixtureLlm):
    draft_calls: ClassVar[int] = 0
    during_call: ClassVar[list[object]] = []
    failure: ClassVar[Exception | None] = None

    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
    ) -> tuple[JsonValue, Usage]:
        properties = schema.get("properties", {})
        if isinstance(properties, dict) and "sentences" in properties:
            type(self).draft_calls += 1
            hooks = list(type(self).during_call)
            type(self).during_call.clear()
            for hook in hooks:
                assert callable(hook)
                hook()
            failure = type(self).failure
            if failure is not None:
                raise failure
        return super().complete_json(system, user, schema, max_cost_cents=max_cost_cents)


@pytest.fixture
async def drafting_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[CaseService, str, str]]:
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
        case = await held_case(client)
        await get_to_drafting(client, case)
    record = service._store.get_intake(case)
    assert record is not None
    assert record.case_token is not None
    yield service, case, record.case_token
    service._store.close()


def second_service(tmp_path: Path) -> CaseService:
    """Another process's view: its own store connection on the same database."""
    store = SqliteTransactionStore(tmp_path / DATABASE)
    service = build_services(Settings(ProviderMode.FAKE, tmp_path, SECRET, {}), store=store)
    assert isinstance(service, CaseService)
    install_paid_drafter(service)
    return service


def state(service: CaseService, token: str) -> CaseState:
    case = service._store.get_case(token)
    assert case is not None
    return case.letter_state


def attempts(tmp_path: Path, token: str) -> list[tuple[int, int, str | None]]:
    with sqlite3.connect(tmp_path / DATABASE) as database:
        rows = database.execute(
            "SELECT generation, attempt, outcome FROM pb_draft_attempts "
            "WHERE case_token=? ORDER BY generation, attempt",
            (token,),
        ).fetchall()
    return [(int(g), int(a), o) for g, a, o in rows]


@pytest.mark.anyio
async def test_a_second_owner_never_starts_a_paid_call_while_one_is_running(
    drafting_runtime: tuple[CaseService, str, str], tmp_path: Path
) -> None:
    service, case, token = drafting_runtime
    seen: list[CaseState] = []

    def contend() -> None:
        # Runs while the first model call is still in flight, on its own connection.
        other = second_service(tmp_path)
        try:
            other._draft_step(case)
            seen.append(state(other, token))
        finally:
            other._store.close()

    def in_thread() -> None:
        worker = threading.Thread(target=contend)
        worker.start()
        worker.join(timeout=30)
        assert not worker.is_alive()

    CountingDraftLlm.during_call.append(in_thread)
    service._draft_step(case)

    assert seen == [CaseState.DRAFTING]
    assert CountingDraftLlm.draft_calls == 1
    assert state(service, token) is CaseState.AWAITING_ADOPTION
    generation = attempts(tmp_path, token)[0][0]
    assert attempts(tmp_path, token) == [(generation, 1, DraftOutcome.RETURNED.value)]
    for _ in range(3):
        service.advance_due()
    assert CountingDraftLlm.draft_calls == 1


HOLDER = """
import sys, time
from pathlib import Path
from parkingbreaker.transaction_store import SqliteTransactionStore
store = SqliteTransactionStore(Path(sys.argv[1]))
with store.draft_ownership(sys.argv[2]) as owned:
    print("owned" if owned else "refused", flush=True)
    time.sleep(120)
"""


@pytest.mark.anyio
async def test_ownership_held_by_another_process_blocks_until_that_process_dies(
    drafting_runtime: tuple[CaseService, str, str], tmp_path: Path
) -> None:
    service, case, token = drafting_runtime
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(tmp_path / DATABASE), token],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "owned"
        service._draft_step(case)
        service.advance_due()
        assert CountingDraftLlm.draft_calls == 0
        assert state(service, token) is CaseState.DRAFTING
        assert attempts(tmp_path, token) == []
    finally:
        # Death without cleanup, as in a crash: the kernel releases the lock.
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=30)
    service._draft_step(case)
    assert CountingDraftLlm.draft_calls == 1
    assert state(service, token) is CaseState.AWAITING_ADOPTION


@pytest.mark.anyio
@pytest.mark.parametrize("earlier", ["one_crashed_run", "two_crashed_runs", "refused"])
async def test_after_a_crash_only_one_recovery_run_and_a_recorded_refusal_is_final(
    drafting_runtime: tuple[CaseService, str, str], tmp_path: Path, earlier: str
) -> None:
    service, case, token = drafting_runtime
    research = service._store.get_research(case)
    assert research is not None
    generation = research.revision
    crashed_runs = {"one_crashed_run": 1, "two_crashed_runs": DRAFT_RUN_LIMIT, "refused": 1}
    for _ in range(crashed_runs[earlier]):
        # A process that started a run and its first call, then died mid-call.
        assert isinstance(service._store.begin_draft_run(token, generation, NOW), int)
        assert service._store.begin_draft_attempt(token, generation, SHA, NOW) is not None
    if earlier == "refused":
        # The refusal was recorded, then the process died before the stop committed.
        service._store.finish_draft_attempt(token, generation, 1, SHA, DraftOutcome.REFUSED)
        assert service._store.begin_draft_run(token, generation, NOW) is DraftRunRefused.REFUSED
    service._draft_step(case)
    for _ in range(3):
        service.advance_due()
    case_record = service._store.get_case(token)
    assert case_record is not None
    if earlier == "one_crashed_run":
        assert CountingDraftLlm.draft_calls == 1
        assert case_record.letter_state is CaseState.AWAITING_ADOPTION
        assert attempts(tmp_path, token)[-1] == (generation, 2, DraftOutcome.RETURNED.value)
    else:
        # No further paid call; the stop itself is left to the existing window stop.
        assert CountingDraftLlm.draft_calls == 0
        assert case_record.letter_state is CaseState.DRAFTING
        assert case_record.payment_state is not PaymentState.CAPTURED
        assert len(attempts(tmp_path, token)) == crashed_runs[earlier]


def test_each_correction_call_is_fingerprinted_and_a_refusal_is_recorded(
    tmp_path: Path,
) -> None:
    store = SqliteTransactionStore(tmp_path / DATABASE)
    seen: list[tuple[str, ...]] = []

    class Inner:
        def draft(
            self, notes: str, references: object, facts: object, problems: object = ()
        ) -> object:
            assert isinstance(problems, tuple)
            seen.append(problems)
            if "refuse" in problems:
                raise LlmProviderError("private detail", retryable=False)
            return DraftResult(Draft(()), Usage())

    try:
        recorded = _RecordedDrafter(Inner(), store, "case-a", 7, SHA, lambda: NOW)  # type: ignore[arg-type]
        recorded.draft("notes", (), None, ())  # type: ignore[arg-type]
        recorded.draft("notes", (), None, ("too long",))  # type: ignore[arg-type]
        with pytest.raises(LlmProviderError):
            recorded.draft("notes", (), None, ("refuse",))  # type: ignore[arg-type]
        assert seen == [(), ("too long",), ("refuse",)]
        rows = (
            store._require_open()
            .execute(
                "SELECT attempt, inputs_sha256, outcome FROM pb_draft_attempts ORDER BY attempt"
            )
            .fetchall()
        )
        assert [(a, o) for a, _, o in rows] == [(1, "returned"), (2, "returned"), (3, "refused")]
        assert len({sha for _, sha, _ in rows}) == 3
        assert store.begin_draft_run("case-a", 7, NOW) is DraftRunRefused.REFUSED
    finally:
        store.close()


def test_calls_are_append_only_finish_once_for_their_inputs_and_slots_are_fixed(
    tmp_path: Path,
) -> None:
    # A v1 store (calls without runs or refusals) migrates in place, keeping its rows.
    SqliteTransactionStore(tmp_path / DATABASE).close()
    v1 = sqlite3.connect(tmp_path / DATABASE)
    for trigger in (
        "pb_draft_runs_fixed",
        "pb_draft_runs_no_delete",
        "pb_draft_attempts_no_delete",
        "pb_draft_attempts_finish_once",
    ):
        v1.execute(f"DROP TRIGGER {trigger}")
    v1.execute("DROP TABLE pb_draft_runs")
    v1.execute("DROP TABLE pb_draft_attempts")
    v1.execute(_V1_ATTEMPTS_DDL)
    v1.execute(
        "INSERT INTO pb_draft_attempts VALUES ('old', 1, 1, ?, ?, 'returned')",
        (SHA, NOW.isoformat()),
    )
    v1.execute("UPDATE pb_store_versions SET version=1 WHERE component='draft_ownership'")
    v1.commit()
    v1.close()
    store = SqliteTransactionStore(tmp_path / DATABASE)
    try:
        assert attempts(tmp_path, "old") == [(1, 1, DraftOutcome.RETURNED.value)]
        assert store.begin_draft_run("case-a", 1, NOW) == 1
        assert store.begin_draft_run("case-a", 1, NOW) == DRAFT_RUN_LIMIT
        assert store.begin_draft_run("case-a", 1, NOW) is DraftRunRefused.EXHAUSTED
        assert store.begin_draft_run("case-a", 2, NOW) == 1
        assert store.begin_draft_attempt("case-a", 1, SHA, NOW) == 1
        store.finish_draft_attempt("case-a", 1, 1, "b" * 64, DraftOutcome.RETURNED)
        assert attempts(tmp_path, "case-a") == [(1, 1, None)]
        store.finish_draft_attempt("case-a", 1, 1, SHA, DraftOutcome.FAILED)
        store.finish_draft_attempt("case-a", 1, 1, SHA, DraftOutcome.RETURNED)
        assert attempts(tmp_path, "case-a") == [(1, 1, DraftOutcome.FAILED.value)]
        for expected in range(2, DRAFT_CALL_LIMIT + 1):
            assert store.begin_draft_attempt("case-a", 1, SHA, NOW) == expected
        assert store.begin_draft_attempt("case-a", 1, SHA, NOW) is None
        with store.draft_ownership("case-a") as first, store.draft_ownership("case-b") as other:
            assert first
            assert other
            with store.draft_ownership("case-a") as again:
                assert not again
        with store.draft_ownership("case-a") as released:
            assert released
        database = store._require_open()
        slots = dict(database.execute("SELECT case_token, slot FROM pb_draft_slots").fetchall())
        assert slots == {"case-a": 0, "case-b": 1}
        for statement in (
            "DELETE FROM pb_draft_attempts",
            "UPDATE pb_draft_attempts SET outcome='returned' WHERE outcome='failed'",
            "DELETE FROM pb_draft_runs",
            "UPDATE pb_draft_runs SET run=9",
            "UPDATE pb_draft_slots SET slot=5",
            "DELETE FROM pb_draft_slots",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                database.execute(statement)
    finally:
        store.close()


@pytest.mark.anyio
async def test_a_storage_fault_while_recording_a_refusal_still_stops_without_charge(
    drafting_runtime: tuple[CaseService, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, case, token = drafting_runtime
    CountingDraftLlm.failure = LlmProviderError("private detail", retryable=False)

    def busy(*_args: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service._store, "finish_draft_attempt", busy)
    service._draft_step(case)
    case_record = service._store.get_case(token)
    assert case_record is not None
    assert case_record.letter_state is CaseState.STOPPED_NO_CHARGE
    assert case_record.payment_state is PaymentState.RELEASED
    assert CountingDraftLlm.draft_calls == 1
