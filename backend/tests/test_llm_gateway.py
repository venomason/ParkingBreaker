from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

import pytest

from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import LlmBudgetExceeded, Usage
from parkingbreaker.llm_gateway import CallLoggingLlmClient
from parkingbreaker.oplog.ai_journal import AiCall, AiCallStatus, AiWorker
from parkingbreaker.research.fakes import FakeLlmClient

NOW = datetime(2026, 11, 1, tzinfo=UTC)
CallRow = tuple[str, AiCall, datetime]


def collect(rows: list[CallRow]) -> Callable[[str, AiCall, datetime], bool]:
    def append(subject: str, row: AiCall, at: datetime) -> bool:
        rows.append((subject, row, at))
        return True

    return append


def test_gateway_logs_success_once_and_preserves_result_and_arguments() -> None:
    reply: JsonValue = {"safe": True}
    usage = Usage(12, 4, 3)
    inner = FakeLlmClient(reply, usage)
    rows: list[CallRow] = []
    client = CallLoggingLlmClient(
        inner,
        "intake_a",
        AiWorker.INTAKE_READING,
        "fake-model",
        collect(rows),
        lambda: NOW,
    )
    schema = {"type": cast(JsonValue, "object")}
    result = client.complete_json("private system", "private prompt", schema, max_cost_cents=9)
    assert result[0] is reply
    assert result[1] is usage
    assert inner.ceilings == [9]
    assert len(inner.calls) == 1
    assert len(rows) == 1
    assert rows[0][1].status is AiCallStatus.OK
    assert rows[0][1].input_tokens == 12
    assert rows[0][1].cost_cents == 3
    assert "private prompt" not in rows[0][1].to_json()


def test_gateway_budget_and_provider_failure_preserve_original_exception() -> None:
    failure = LlmBudgetExceeded("private detail")

    class Failing:
        def complete_json(self, *args: object, **kwargs: object) -> tuple[JsonValue, Usage]:
            raise failure

    rows: list[CallRow] = []
    client = CallLoggingLlmClient(
        Failing(),
        "intake_a",
        AiWorker.INTAKE_READING,
        "fake-model",
        collect(rows),
        lambda: NOW,
    )
    with pytest.raises(LlmBudgetExceeded) as caught:
        client.complete_json("s", "u", {})
    assert caught.value is failure
    assert rows[0][1].status is AiCallStatus.BUDGET_EXCEEDED
    assert rows[0][1].input_tokens == rows[0][1].output_tokens == rows[0][1].cost_cents == 0


def test_gateway_unknown_failure_logs_null_usage_and_preserves_exception() -> None:
    failure = RuntimeError("private exception prompt")
    rows: list[CallRow] = []

    class Failing:
        def complete_json(self, *args: object, **kwargs: object) -> tuple[JsonValue, Usage]:
            raise failure

    client = CallLoggingLlmClient(
        Failing(),
        "intake_a",
        AiWorker.INTAKE_READING,
        "fake-model",
        collect(rows),
        lambda: NOW,
    )
    with pytest.raises(RuntimeError) as caught:
        client.complete_json("s", "u", {})
    assert caught.value is failure
    assert rows[0][1].status is AiCallStatus.FALLBACK_AFTER_FAILURE
    assert rows[0][1].input_tokens is rows[0][1].output_tokens is rows[0][1].cost_cents is None


def test_gateway_logging_failures_do_not_change_provider_outcome() -> None:
    reply: JsonValue = {"ok": True}
    usage = Usage(1, 2, 3)

    def broken_clock() -> datetime:
        raise RuntimeError("clock unavailable")

    success = CallLoggingLlmClient(
        FakeLlmClient(reply, usage),
        "intake_a",
        AiWorker.INTAKE_READING,
        "fake-model",
        lambda *_: True,
        broken_clock,
    )
    result = success.complete_json("s", "u", {})
    assert result[0] is reply
    assert result[1] is usage
    failure = LlmBudgetExceeded("budget")

    class Failing:
        def complete_json(self, *args: object, **kwargs: object) -> tuple[JsonValue, Usage]:
            raise failure

    failed = CallLoggingLlmClient(
        Failing(),
        "intake_a",
        AiWorker.INTAKE_READING,
        "fake-model",
        lambda *_: (_ for _ in ()).throw(RuntimeError("sink")),
        lambda: NOW,
    )
    with pytest.raises(LlmBudgetExceeded) as caught:
        failed.complete_json("s", "u", {})
    assert caught.value is failure


def test_call_unknown_usage_is_failure_only_and_legacy_rows_stay_valid() -> None:
    import json

    from parkingbreaker.oplog.ai_journal import AiCall

    failed = AiCall(
        AiWorker.INTAKE_READING, "fake-model", AiCallStatus.FALLBACK_AFTER_FAILURE, None, None, None
    )
    assert AiCall.from_json(failed.to_json()) == failed
    legacy = AiCall.from_json(
        '{"worker":"RESEARCH","model":"offline-fixture","status":"OK","input_tokens":0,"output_tokens":0,"cost_cents":0}'
    )
    assert (legacy.input_tokens, legacy.output_tokens, legacy.cost_cents) == (0, 0, 0)
    for status, values in (
        (AiCallStatus.OK, (None, None, None)),
        (AiCallStatus.OK, (None, 1, 0)),
        (AiCallStatus.FALLBACK_AFTER_FAILURE, (None, None, 0)),
    ):
        with pytest.raises(ValueError, match="unknown usage"):
            AiCall(AiWorker.INTAKE_READING, "fake-model", status, *values)
    for value in (True, -1):
        with pytest.raises(ValueError, match="bounded count"):
            AiCall(AiWorker.INTAKE_READING, "fake-model", AiCallStatus.OK, value, 0, 0)
    assert set(json.loads(failed.to_json())) == {
        "worker",
        "model",
        "status",
        "input_tokens",
        "output_tokens",
        "cost_cents",
    }
