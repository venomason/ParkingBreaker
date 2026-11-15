from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from parkingbreaker.llm import Usage
from parkingbreaker.oplog.ai_journal import AiCall, AiCallStatus, AiNote, AiWorker
from parkingbreaker.research.search import SearchError
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_case_service import WORKER, researched_case
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


def test_calls_hold_only_labels_and_bounded_counts(tmp_path: Path) -> None:
    call = AiCall(AiWorker.RESEARCH, "minimax-m3.1", AiCallStatus.OK, 1200, 300, 4)
    assert AiCall.from_json(call.to_json()) == call
    for bad in ("Pat Customer", "", "x" * 81):
        with pytest.raises(ValueError, match="configuration label"):
            AiCall(AiWorker.RESEARCH, bad, AiCallStatus.OK)
    with pytest.raises(ValueError, match="bounded count"):
        AiCall(AiWorker.RESEARCH, "m", AiCallStatus.OK, cost_cents=-1)
    with SqliteTransactionStore(tmp_path / "calls.sqlite") as store:
        assert store.try_append_ai_call("case_a", call, NOW)
        assert store.ai_calls("case_a") == (call,)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._require_open().execute("DELETE FROM pb_ai_calls")


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_research_runs_log_their_cost_and_failures_without_changing_the_appeal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_tier_acceptance import extract_case, model_answers

    result, _client, _searcher = extract_case(model_answers())
    result = replace(result, cost=Usage(1500, 250, 3))
    (tmp_path / "ok").mkdir()
    (tmp_path / "failed").mkdir()
    client, real, case = await researched_case(tmp_path / "ok", monkeypatch, result)
    research = real._deps.research
    assert research is not None
    object.__setattr__(real._deps, "research", replace(research, model_label="minimax-m3.1"))
    try:
        step = await client.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
        )
        assert step.status_code == 200, step.text
    finally:
        await client.aclose()
    token = real._intake(case).case_token
    assert token is not None
    assert real._store.ai_calls(token) == (
        AiCall(AiWorker.RESEARCH, "minimax-m3.1", AiCallStatus.OK, 1500, 250, 3),
    )
    (entry,) = real._store.ai_journal(token)
    assert (entry.note, entry.status, entry.calls) == (AiNote.SOURCE_FOUND, AiCallStatus.OK, 1)

    client, failed, case = await researched_case(
        tmp_path / "failed", monkeypatch, SearchError("rate limited")
    )
    try:
        status = (
            await client.post(
                f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
            )
        ).json()
        assert status["state"] == "STOPPED_NO_CHARGE"
    finally:
        await client.aclose()
    token = failed._intake(case).case_token
    assert token is not None
    assert failed._store.ai_calls(token) == (
        AiCall(AiWorker.RESEARCH, "unrecorded", AiCallStatus.FALLBACK_AFTER_FAILURE),
    )


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_a_failing_call_log_cannot_stop_research_from_resolving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_tier_acceptance import extract_case, model_answers

    result, _client, _searcher = extract_case(model_answers())
    client, real, case = await researched_case(tmp_path, monkeypatch, result)

    def broken(*_args: object) -> bool:
        raise RuntimeError("log store unavailable")

    monkeypatch.setattr(real._store, "try_append_ai_call", broken)
    try:
        step = await client.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
        )
        assert step.status_code == 200, step.text
        assert step.json()["state"] in {"NEEDS_CUSTOMER", "DRAFTING"}
    finally:
        await client.aclose()
