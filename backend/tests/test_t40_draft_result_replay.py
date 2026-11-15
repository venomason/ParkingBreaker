"""T40 follow-up: a returned paid drafting call is saved with its mark and never paid twice."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from parkingbreaker.appeal.drafting import Draft, DraftResult, DraftSentence
from parkingbreaker.appeal.references import Reference
from parkingbreaker.case_service import CaseService, _RecordedDrafter
from parkingbreaker.case_state import CaseState
from parkingbreaker.draft_ownership_store import _V2_ATTEMPTS_DDL, DraftOutcome
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import Usage
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_config import SECRET
from test_draft_ownership import (
    DATABASE,
    NOW,
    SHA,
    CountingDraftLlm,
    drafting_runtime,  # noqa: F401  (pytest fixture)
)

# One drafting step in its own process, which dies right after the paid call in "crash" mode.
STEP = """
import os, sys
from pathlib import Path
from parkingbreaker import config
from parkingbreaker.appeal.drafting import LlmDrafter
from parkingbreaker.config import ProviderMode, Settings, build_services
service = build_services(Settings(ProviderMode.FAKE, Path(sys.argv[1]), sys.argv[2], {}))
# Paid-draft machinery is opt-in here; production authorship is deterministic.
object.__setattr__(service._deps, "drafter_for_subject", None)
object.__setattr__(
    service._deps, "drafter", LlmDrafter(config.FixtureLlm(config.FIXTURE.read_text()))
)
if sys.argv[4] == "crash":
    service._save_draft = lambda *args, **kwargs: os._exit(9)
service._draft_step(sys.argv[3])
"""


class Crash(Exception):
    """The process dies after the call returned, before the draft is saved."""


def crash_at_next_save(service: CaseService, monkeypatch: pytest.MonkeyPatch) -> None:
    real_save = service._save_draft

    def crash_once(*args: object, **kwargs: object) -> object:
        monkeypatch.setattr(service, "_save_draft", real_save)
        raise Crash

    monkeypatch.setattr(service, "_save_draft", crash_once)


@pytest.fixture
def first_draft_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first paid draft fails its checks, so a paid correction call follows it."""
    real = CountingDraftLlm.complete_json

    def answer(
        self: CountingDraftLlm,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
    ) -> tuple[JsonValue, Usage]:
        value, usage = real(self, system, user, schema, max_cost_cents=max_cost_cents)
        properties = schema.get("properties", {})
        if (
            isinstance(properties, dict)
            and "sentences" in properties
            and CountingDraftLlm.draft_calls == 1
        ):
            assert isinstance(value, dict)
            sentences = value["sentences"]
            assert isinstance(sentences, list)
            invented: dict[str, JsonValue] = {
                "text": "The officer admitted the meter was broken.",
                "support": ["note:0-5"],
                "paragraph": 0,
            }
            return {"sentences": [*sentences, invented]}, usage
        return value, usage

    monkeypatch.setattr(CountingDraftLlm, "complete_json", answer)


@pytest.mark.anyio
async def test_a_crash_after_the_call_returned_replays_its_result_without_another_paid_call(
    drafting_runtime: tuple[CaseService, str, str],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, case, token = drafting_runtime
    crash_at_next_save(service, monkeypatch)
    with pytest.raises(Crash):
        service._draft_step(case)
    assert CountingDraftLlm.draft_calls == 1
    with sqlite3.connect(tmp_path / DATABASE) as database:
        ((outcome, saved),) = database.execute(
            "SELECT outcome, result_json FROM pb_draft_attempts WHERE case_token=?", (token,)
        ).fetchall()
    assert outcome == DraftOutcome.RETURNED.value
    assert saved is not None
    # The recovery run reuses the paid result: one call in total, and the letter is ready.
    service._draft_step(case)
    for _ in range(3):
        service.advance_due()
    assert CountingDraftLlm.draft_calls == 1
    record = service._store.get_case(token)
    assert record is not None
    assert record.letter_state is CaseState.AWAITING_ADOPTION
    assert record.payment_state is not PaymentState.CAPTURED


@pytest.mark.anyio
@pytest.mark.usefixtures("first_draft_is_flagged")
async def test_a_crash_after_a_correction_call_replays_every_paid_call(
    drafting_runtime: tuple[CaseService, str, str],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, case, token = drafting_runtime
    crash_at_next_save(service, monkeypatch)
    with pytest.raises(Crash):
        service._draft_step(case)
    assert CountingDraftLlm.draft_calls == 2  # the first draft and its paid correction
    # Correction feedback names spans by the run's fresh revision; that is not a new call.
    service._draft_step(case)
    assert CountingDraftLlm.draft_calls == 2
    record = service._store.get_case(token)
    assert record is not None
    assert record.letter_state is CaseState.AWAITING_ADOPTION


def test_a_repeated_identical_call_is_paid_and_recovery_replays_each_result_in_order(
    tmp_path: Path,
) -> None:
    class Inner:
        calls = 0

        def draft(
            self,
            notes: str,
            references: Sequence[Reference],
            facts: object,
            problems: Sequence[str] = (),
        ) -> DraftResult:
            self.calls += 1
            sentence = DraftSentence(f"answer {self.calls}", ())
            return DraftResult(Draft((sentence,)), Usage())

    store = SqliteTransactionStore(tmp_path / DATABASE)
    inner = Inner()
    same = ("draft: mode_boundary: empty: the draft has no spans",)
    try:
        run = _RecordedDrafter(inner, store, "case-a", 7, SHA, lambda: NOW)
        first = run.draft("notes", (), None, same)  # type: ignore[arg-type]
        second = run.draft("notes", (), None, same)  # type: ignore[arg-type]
        # Inside one run a repeat is a real attempt, as it always was.
        assert inner.calls == 2
        assert first.draft != second.draft
        recovery = _RecordedDrafter(inner, store, "case-a", 7, SHA, lambda: NOW)
        assert recovery.draft("notes", (), None, same).draft == first.draft  # type: ignore[arg-type]
        assert recovery.draft("notes", (), None, same).draft == second.draft  # type: ignore[arg-type]
        assert inner.calls == 2
    finally:
        store.close()


@pytest.mark.anyio
async def test_a_process_dying_after_the_call_is_recovered_by_another_without_paying_again(
    drafting_runtime: tuple[CaseService, str, str],  # noqa: F811
    tmp_path: Path,
) -> None:
    service, case, token = drafting_runtime

    def step(mode: str, seed: str) -> int:
        return subprocess.run(
            [sys.executable, "-c", STEP, str(tmp_path), SECRET, case, mode],
            env={**os.environ, "PYTHONHASHSEED": seed},
            timeout=120,
            check=False,
            capture_output=True,
        ).returncode

    def calls() -> list[tuple[int, str]]:
        with sqlite3.connect(tmp_path / DATABASE) as database:
            return database.execute(
                "SELECT attempt, outcome FROM pb_draft_attempts WHERE case_token=?", (token,)
            ).fetchall()

    # Different hash seeds, as after a real restart: the call's fingerprint must not depend on them.
    assert step("crash", "1") == 9
    assert calls() == [(1, DraftOutcome.RETURNED.value)]
    assert step("finish", "2") == 0
    assert calls() == [(1, DraftOutcome.RETURNED.value)]
    record = service._store.get_case(token)
    assert record is not None
    assert record.letter_state is CaseState.AWAITING_ADOPTION


def test_only_a_returned_call_keeps_a_result_and_only_for_its_own_inputs(tmp_path: Path) -> None:
    store = SqliteTransactionStore(tmp_path / DATABASE)
    try:
        assert store.begin_draft_attempt("case-a", 1, SHA, NOW) == 1
        with pytest.raises(ValueError, match="only a returned call"):
            store.finish_draft_attempt("case-a", 1, 1, SHA, DraftOutcome.FAILED, "{}")
        store.finish_draft_attempt("case-a", 1, 1, SHA, DraftOutcome.RETURNED, '{"r": 1}')
        assert store.returned_draft("case-a", 1, SHA) == '{"r": 1}'
        assert store.returned_draft("case-a", 1, "b" * 64) is None
        assert store.returned_draft("case-a", 2, SHA) is None
        assert store.returned_draft("case-b", 1, SHA) is None
        database = store._require_open()
        for statement in (
            "UPDATE pb_draft_attempts SET result_json='{}'",
            "INSERT INTO pb_draft_attempts VALUES ('case-c', 1, 1, '" + SHA + "', 'x', "
            "'failed', '{}')",
            "INSERT INTO pb_draft_attempts VALUES ('case-d', 1, 1, '" + SHA + "', 'x', NULL, '{}')",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                database.execute(statement)
    finally:
        store.close()


def test_a_v2_store_migrates_keeping_every_recorded_call_without_a_result(
    tmp_path: Path,
) -> None:
    SqliteTransactionStore(tmp_path / DATABASE).close()
    v2 = sqlite3.connect(tmp_path / DATABASE)
    for trigger in ("pb_draft_attempts_no_delete", "pb_draft_attempts_finish_once"):
        v2.execute(f"DROP TRIGGER {trigger}")
    v2.execute("DROP TABLE pb_draft_attempts")
    v2.execute(_V2_ATTEMPTS_DDL)
    v2.execute(
        "INSERT INTO pb_draft_attempts VALUES ('old', 1, 1, ?, ?, 'returned')",
        (SHA, NOW.isoformat()),
    )
    v2.execute("UPDATE pb_store_versions SET version=2 WHERE component='draft_ownership'")
    v2.commit()
    v2.close()
    store = SqliteTransactionStore(tmp_path / DATABASE)
    try:
        database = store._require_open()
        assert database.execute(
            "SELECT version FROM pb_store_versions WHERE component='draft_ownership'"
        ).fetchone() == (3,)
        assert database.execute(
            "SELECT case_token, attempt, outcome, result_json FROM pb_draft_attempts"
        ).fetchall() == [("old", 1, "returned", None)]
        # An earlier returned call has no saved result, so it is never replayed.
        assert store.returned_draft("old", 1, SHA) is None
        with pytest.raises(sqlite3.IntegrityError):
            database.execute("DELETE FROM pb_draft_attempts")
    finally:
        store.close()
