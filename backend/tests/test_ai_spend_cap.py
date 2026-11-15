"""T40: a durable per-appeal spending cap for each AI worker, enforced before any call."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import LlmBudgetExceeded, Usage
from parkingbreaker.llm_gateway import CallLoggingLlmClient, SpendLedger
from parkingbreaker.oplog.ai_journal import AiCallStatus, AiWorker
from parkingbreaker.research.fakes import FakeLlmClient
from parkingbreaker.transaction_store import SqliteTransactionStore

NOW = datetime(2026, 11, 3, tzinfo=UTC)
SCHEMA = {"type": cast(JsonValue, "object")}


class FailingLlm:
    def complete_json(
        self, system: str, user: str, schema: object, *, max_cost_cents: int | None = None
    ) -> tuple[JsonValue, Usage]:
        raise TimeoutError("provider answer lost")


def _gateway(store: SqliteTransactionStore, inner: object, cap: int) -> CallLoggingLlmClient:
    return CallLoggingLlmClient(
        inner,  # type: ignore[arg-type]
        "case_spend",
        AiWorker.DRAFTING,
        "fake-model",
        store.try_append_ai_call,
        lambda: NOW,
        spend=cast(SpendLedger, store),
        cap_cents=cap,
    )


def test_each_call_gets_only_the_remaining_allowance_and_a_spent_cap_stops_the_worker(
    tmp_path: Path,
) -> None:
    with SqliteTransactionStore(tmp_path / "s.sqlite") as store:
        inner = FakeLlmClient({"ok": True}, Usage(10, 10, 30))
        client = _gateway(store, inner, 50)
        client.complete_json("s", "u", SCHEMA, max_cost_cents=40)
        assert inner.ceilings == [40]  # the caller's ceiling, inside the allowance
        assert store.ai_spent_cents("case_spend", AiWorker.DRAFTING) == 30
        with pytest.raises(LlmBudgetExceeded):
            client.complete_json("s", "u", SCHEMA, max_cost_cents=40)  # 30 can't fit 20 left
        assert inner.ceilings == [40, 20]
        assert store.ai_spent_cents("case_spend", AiWorker.DRAFTING) == 30  # refused: free
        # A call whose cost is unknown keeps its reservation counted; without a caller
        # ceiling one call reserves at most half the allowance (here all 20 that is left).
        with pytest.raises(TimeoutError):
            _gateway(store, FailingLlm(), 50).complete_json("s", "u", SCHEMA)
        assert store.ai_spent_cents("case_spend", AiWorker.DRAFTING) == 50
        # Nothing is left, so the next call is refused before any provider request.
        with pytest.raises(LlmBudgetExceeded):
            client.complete_json("s", "u", SCHEMA)
        assert inner.ceilings == [40, 20]
        statuses = [call.status for call in store.ai_calls("case_spend")]
        assert statuses[-1] is AiCallStatus.BUDGET_EXCEEDED
        # Other workers and other appeals keep their own allowance.
        assert store.ai_spent_cents("case_spend", AiWorker.RESEARCH) == 0
        assert store.ai_spent_cents("case_other", AiWorker.DRAFTING) == 0
    with SqliteTransactionStore(tmp_path / "s.sqlite") as store:
        assert store.ai_spent_cents("case_spend", AiWorker.DRAFTING) == 50  # survives restart


def test_a_concurrent_second_call_finds_nothing_left_and_history_cannot_be_rewritten(
    tmp_path: Path,
) -> None:
    with SqliteTransactionStore(tmp_path / "s.sqlite") as store:
        assert store.reserve_ai_spend("case_spend", AiWorker.RESEARCH, 150, NOW, 100) == (1, 100)
        # While the first call is in flight its reservation counts in full.
        assert store.reserve_ai_spend("case_spend", AiWorker.RESEARCH, 150, NOW, 100) == (2, 50)
        assert store.reserve_ai_spend("case_spend", AiWorker.RESEARCH, 150, NOW, 100) is None
        store.settle_ai_spend("case_spend", AiWorker.RESEARCH, 1, 120)  # an overrun shows
        assert store.ai_spent_cents("case_spend", AiWorker.RESEARCH) == 170
        database = store._require_open()
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            database.execute("DELETE FROM pb_ai_spend")
        with pytest.raises(sqlite3.DatabaseError, match="settles once"):
            database.execute("UPDATE pb_ai_spend SET settled_cents=0 WHERE seq=1")


def test_a_busy_ledger_is_retried_later_and_a_broken_one_fails_closed(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "s.sqlite") as store:
        inner = FakeLlmClient({"ok": True}, Usage(1, 1, 1))
        client = _gateway(store, inner, 50)
        busy = sqlite3.OperationalError("database is locked")
        with (
            patch.object(store, "reserve_ai_spend", side_effect=busy),
            pytest.raises(sqlite3.OperationalError),
        ):
            client.complete_json("s", "u", SCHEMA)  # not a budget stop: the sweep retries
        with (
            patch.object(store, "reserve_ai_spend", side_effect=ValueError("corrupt")),
            pytest.raises(LlmBudgetExceeded),
        ):
            client.complete_json("s", "u", SCHEMA)
        assert inner.ceilings == []  # neither reached the provider
