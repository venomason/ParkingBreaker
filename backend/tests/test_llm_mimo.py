from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import httpx2 as httpx
import openai
import pytest

from parkingbreaker import llm_mimo
from parkingbreaker.llm import LlmBudgetExceeded, LlmProviderError, Usage, schema_json
from parkingbreaker.llm_gateway import CallLoggingLlmClient
from parkingbreaker.llm_mimo import MiMoLlmClient
from parkingbreaker.llm_models import (
    MIMO_BASE_URL,
    MIMO_CONTEXT_TOKENS,
    MIMO_MAX_OUTPUT_TOKENS,
    LlmModelConfig,
    ModelPricing,
    mimo_config,
    request_output_tokens,
    usage_cost_cents,
    worst_case_cents,
)
from parkingbreaker.oplog.ai_journal import AiCall, AiCallStatus, AiWorker
from parkingbreaker.transaction_store import SqliteTransactionStore

# Clearly synthetic rates: 100 / 10 / 200 cents per million tokens.
PRICING = ModelPricing(Decimal("100"), Decimal("10"), Decimal("200"))
# Cheap synthetic rates keep a full-context reservation inside the 50/150/50 cent caps.
CHEAP = ModelPricing(Decimal("0.5"), Decimal("0.05"), Decimal("1"))


def config(**kwargs: object) -> LlmModelConfig:
    values: dict[str, object] = {
        "pricing_source": "synthetic-test-source",
        "pricing_version": "synthetic-v1",
        "pricing": PRICING,
    }
    values.update(kwargs)
    return mimo_config("mimo-v2.6-flash", **values)  # type: ignore[arg-type]


def usage_obj(
    prompt: object = 1000,
    completion: object = 100,
    *,
    cached: object = 0,
    reasoning: object = 0,
    total: object = None,
    details: bool = True,
) -> object:
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=(
            (prompt + completion)
            if total is None and isinstance(prompt, int) and isinstance(completion, int)
            else total
        ),
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached) if details else None,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning) if details else None,
    )


def response(
    content: object = '{"ok": true}',
    finish: str = "stop",
    *,
    usage: object | None = None,
    role: str = "assistant",
    tool_calls: object = None,
    refusal: object = None,
    choices: object = None,
) -> object:
    if choices is not None:
        return SimpleNamespace(usage=usage if usage is not None else usage_obj(), choices=choices)
    message = SimpleNamespace(role=role, content=content, tool_calls=tool_calls, refusal=refusal)
    return SimpleNamespace(
        usage=usage if usage is not None else usage_obj(),
        choices=[SimpleNamespace(message=message, finish_reason=finish)],
    )


class Transport:
    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self.result = response() if result is None else result
        self.error = error
        self.payloads: list[dict[str, object]] = []

    def create(self, payload: object) -> object:
        assert isinstance(payload, dict)
        self.payloads.append(cast(dict[str, object], payload))
        if len(self.payloads) > 1:
            raise AssertionError("adapter made more than one transport attempt")
        if self.error is not None:
            raise self.error
        return self.result


def api_error(status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.xiaomimimo.com/v1/chat/completions")
    return openai.APIStatusError(
        "provider body", response=httpx.Response(status, request=request), body=None
    )


def test_payload_is_exact_chat_completions_json_mode() -> None:
    transport = Transport()
    client = MiMoLlmClient(config(), "offline-key", transport=transport)
    result, usage = client.complete_json("system ✓", "prompt 🙂", {"type": "object"})
    assert result == {"ok": True}
    assert usage == Usage(1000, 100, usage_cost_cents(PRICING, 1000, 0, 100))
    (payload,) = transport.payloads
    assert payload["model"] == "mimo-v2.6-flash"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["stream"] is False
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["max_completion_tokens"] == 16000
    messages = cast(list[dict[str, str]], payload["messages"])
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[1]["content"] == "prompt 🙂"
    assert schema_json({"type": "object"}) in messages[0]["content"]
    assert "JSON only" in messages[0]["content"]
    assert "reasoning" not in payload
    assert "tools" not in payload


def test_thinking_is_constructor_owned() -> None:
    transport = Transport()
    MiMoLlmClient(config(), "offline-key", transport=transport, thinking=True).complete_json(
        "s", "u", {}
    )
    assert transport.payloads[0]["thinking"] == {"type": "enabled"}


def test_ordinary_sdk_construction_uses_fixed_endpoint_without_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    fake = SimpleNamespace()

    def build(**kwargs: object) -> object:
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(llm_mimo.openai, "OpenAI", build)
    MiMoLlmClient(config(), "offline-key")
    assert captured == {
        "api_key": "offline-key",
        "base_url": MIMO_BASE_URL,
        "max_retries": 0,
        "timeout": 20.0,
    }


def test_cached_input_subtracted_and_reasoning_not_double_counted() -> None:
    transport = Transport(
        response(usage=usage_obj(1_000_000, 500_000, cached=400_000, reasoning=300_000))
    )
    _, usage = MiMoLlmClient(config(), "offline-key", transport=transport).complete_json(
        "s", "u", {}
    )
    assert usage.input_tokens == 1_000_000
    assert usage.output_tokens == 500_000
    assert usage.cost_cents == 164
    assert usage_cost_cents(PRICING, 600_000, 400_000, 500_000) == 164


def test_missing_cache_breakdown_charges_prompt_at_higher_rate() -> None:
    transport = Transport(response(usage=usage_obj(1_000_000, 0, details=False)))
    _, usage = MiMoLlmClient(config(), "offline-key", transport=transport).complete_json(
        "s", "u", {}
    )
    assert usage.cost_cents == 100

    pricier_cache = replace(
        config(), pricing=ModelPricing(Decimal("1"), Decimal("300"), Decimal("2"))
    )
    transport = Transport(response(usage=usage_obj(1_000_000, 0, details=False)))
    _, usage = MiMoLlmClient(pricier_cache, "offline-key", transport=transport).complete_json(
        "s", "u", {}
    )
    assert usage.cost_cents == 300


@pytest.mark.parametrize(
    "bad_usage",
    [
        usage_obj(prompt=True),
        usage_obj(prompt=-1),
        usage_obj(completion="1"),
        usage_obj(1_000, 100, cached=1_001),
        usage_obj(1_000, 100, reasoning=101),
        usage_obj(1_000, 100, total=7),
        usage_obj(prompt=1.5),
        SimpleNamespace(prompt_tokens=1),
    ],
)
def test_malformed_or_missing_usage_is_a_sanitized_provider_error(bad_usage: object) -> None:
    transport = Transport(response(usage=bad_usage))
    with pytest.raises(LlmProviderError) as caught:
        MiMoLlmClient(config(), "offline-key", transport=transport).complete_json(
            "secret system", "secret user", {}
        )
    assert "secret" not in str(caught.value)
    assert len(transport.payloads) == 1


@pytest.mark.parametrize(
    "result",
    [
        response("{}", finish="length"),
        response("{}", finish="content_filter"),
        response("{}", finish="repetition_truncation"),
        response("{}", finish="tool_calls", tool_calls=[{"id": "x"}]),
        response("{}", tool_calls=[{"id": "x"}]),
        response("{}", refusal="no"),
        response("{}", role="tool"),
        response("not json"),
        response(""),
        response(None),
        response("{" + "x" * (1_048_577) + "}"),
        response(choices=[]),
        response(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(role="assistant", content="{}"),
                    finish_reason="stop",
                )
            ]
            * 2
        ),
    ],
)
def test_unusable_output_returns_empty_with_known_usage(result: object) -> None:
    transport = Transport(result)
    parsed, usage = MiMoLlmClient(config(), "offline-key", transport=transport).complete_json(
        "s", "u", {}
    )
    assert parsed == {}
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 100
    assert usage.cost_cents == usage_cost_cents(PRICING, 1000, 0, 100)


def test_nan_json_is_rejected_as_unparseable() -> None:
    parsed, usage = MiMoLlmClient(
        config(), "offline-key", transport=Transport(response('{"x": NaN}'))
    ).complete_json("s", "u", {})
    assert parsed == {}
    assert usage.input_tokens == 1000


@pytest.mark.parametrize("status", [402, 429, 500, 503])
def test_status_errors_are_sanitized_provider_errors_never_budget(status: int) -> None:
    transport = Transport(error=api_error(status))
    with pytest.raises(LlmProviderError) as caught:
        MiMoLlmClient(config(), "offline-key", transport=transport).complete_json("s", "u", {})
    assert caught.value.retryable is (status == 429 or status >= 500)
    assert len(transport.payloads) == 1


def test_timeout_and_connection_are_retryable_provider_errors() -> None:
    request = httpx.Request("POST", "https://api.xiaomimimo.com/v1/chat/completions")
    for error in (
        openai.APITimeoutError(request=request),
        openai.APIConnectionError(request=request),
    ):
        transport = Transport(error=error)
        with pytest.raises(LlmProviderError) as caught:
            MiMoLlmClient(config(), "offline-key", transport=transport).complete_json("s", "u", {})
        assert caught.value.retryable


def test_zero_and_unaffordable_ceilings_make_no_transport_call() -> None:
    transport = Transport()
    client = MiMoLlmClient(
        config(max_output_tokens=MIMO_MAX_OUTPUT_TOKENS), "offline-key", transport=transport
    )
    with pytest.raises(LlmBudgetExceeded):
        client.complete_json("s", "u", {}, max_cost_cents=0)
    with pytest.raises(LlmBudgetExceeded):
        client.complete_json("s", "u", {}, max_cost_cents=105)
    assert transport.payloads == []


def test_capped_output_fits_worst_case_and_configured_bound() -> None:
    cfg = config(max_output_tokens=MIMO_MAX_OUTPUT_TOKENS)
    transport = Transport()
    client = MiMoLlmClient(cfg, "offline-key", transport=transport)
    client.complete_json("é🙂", "非ASCII", {"title": "café"}, max_cost_cents=200)
    output = cast(int, transport.payloads[0]["max_completion_tokens"])
    assert output <= MIMO_MAX_OUTPUT_TOKENS
    assert worst_case_cents(cfg, MIMO_CONTEXT_TOKENS, output) <= 200
    assert output == request_output_tokens(cfg, MIMO_CONTEXT_TOKENS, 200)


def test_gateway_success_settles_one_reservation_in_real_ledger(tmp_path: object) -> None:
    from pathlib import Path

    rows: list[tuple[str, AiCall]] = []

    def record(subject: str, call: AiCall, at: object) -> bool:
        rows.append((subject, call))
        return True

    with SqliteTransactionStore(Path(str(tmp_path)) / "ledger.sqlite") as store:
        inner = MiMoLlmClient(config(pricing=CHEAP), "offline-key", transport=Transport())
        client = CallLoggingLlmClient(
            inner,
            "subject_a",
            AiWorker.DRAFTING,
            "mimo-v2.6-flash",
            record,
            lambda: __import__("datetime").datetime.now(__import__("datetime").UTC),
            spend=store,
            cap_cents=50,
        )
        parsed, usage = client.complete_json("s", "u", {})
        assert parsed == {"ok": True}
        assert usage.cost_cents == 1
        assert len(rows) == 1
        assert rows[0][1].status is AiCallStatus.OK
        assert store.ai_spent_cents("subject_a", AiWorker.DRAFTING) == 1


def test_gateway_timeout_keeps_reservation_and_cap_refusal_sends_nothing(tmp_path: object) -> None:
    from datetime import UTC, datetime
    from pathlib import Path

    request = httpx.Request("POST", "https://api.xiaomimimo.com/v1/chat/completions")
    with SqliteTransactionStore(Path(str(tmp_path)) / "ledger.sqlite") as store:
        timing_out = MiMoLlmClient(
            config(pricing=CHEAP),
            "offline-key",
            transport=Transport(error=openai.APITimeoutError(request=request)),
        )
        client = CallLoggingLlmClient(
            timing_out,
            "subject_b",
            AiWorker.RESEARCH,
            "mimo-v2.6-flash",
            lambda *_: True,
            lambda: datetime.now(UTC),
            spend=store,
            cap_cents=150,
        )
        with pytest.raises(LlmProviderError):
            client.complete_json("s", "u", {})
        assert store.ai_spent_cents("subject_b", AiWorker.RESEARCH) == 75

        refusing = Transport()
        capped = CallLoggingLlmClient(
            MiMoLlmClient(config(), "offline-key", transport=refusing),
            "subject_c",
            AiWorker.DRAFTING,
            "mimo-v2.6-flash",
            lambda *_: True,
            lambda: datetime.now(UTC),
            spend=store,
            cap_cents=50,
        )
        with pytest.raises(LlmBudgetExceeded):
            capped.complete_json("s", "u", {})
        assert refusing.payloads == []
        assert store.ai_spent_cents("subject_c", AiWorker.DRAFTING) == 0


def test_gateway_logging_failure_does_not_change_result(tmp_path: object) -> None:
    from datetime import UTC, datetime
    from pathlib import Path

    with SqliteTransactionStore(Path(str(tmp_path)) / "ledger.sqlite") as store:
        client = CallLoggingLlmClient(
            MiMoLlmClient(config(pricing=CHEAP), "offline-key", transport=Transport()),
            "subject_d",
            AiWorker.INTAKE_READING,
            "mimo-v2.6-flash",
            lambda *_: (_ for _ in ()).throw(RuntimeError("sink")),
            lambda: datetime.now(UTC),
            spend=store,
            cap_cents=50,
        )
        parsed, _ = client.complete_json("s", "u", {})
        assert parsed == {"ok": True}
        assert store.ai_spent_cents("subject_d", AiWorker.INTAKE_READING) == 1


def test_gateway_unknown_usage_keeps_reservation_and_subjects_stay_isolated(
    tmp_path: object,
) -> None:
    from datetime import UTC, datetime
    from pathlib import Path

    with SqliteTransactionStore(Path(str(tmp_path)) / "ledger.sqlite") as store:
        missing = CallLoggingLlmClient(
            MiMoLlmClient(
                config(pricing=CHEAP),
                "offline-key",
                transport=Transport(SimpleNamespace(usage=None)),
            ),
            "subject_unknown",
            AiWorker.RESEARCH,
            "mimo-v2.6-flash",
            lambda *_: True,
            lambda: datetime.now(UTC),
            spend=store,
            cap_cents=150,
        )
        with pytest.raises(LlmProviderError):
            missing.complete_json("s", "u", {})
        assert store.ai_spent_cents("subject_unknown", AiWorker.RESEARCH) == 75

        other = CallLoggingLlmClient(
            MiMoLlmClient(config(pricing=CHEAP), "offline-key", transport=Transport()),
            "subject_other",
            AiWorker.RESEARCH,
            "mimo-v2.6-flash",
            lambda *_: True,
            lambda: datetime.now(UTC),
            spend=store,
            cap_cents=150,
        )
        other.complete_json("s", "u", {})
        assert store.ai_spent_cents("subject_other", AiWorker.RESEARCH) == 1
        assert store.ai_spent_cents("subject_unknown", AiWorker.RESEARCH) == 75


def test_existing_strict_caller_validation_rejects_wrong_schema() -> None:
    from datetime import date

    from parkingbreaker.research.extractor import LlmExtractor
    from parkingbreaker.research.findings import FindingStatus
    from parkingbreaker.research.questions import Ticket, build_questions
    from samples import source

    questions = build_questions(
        Ticket("us-ca-san_francisco", "TC 7.2.22", "Street", date(2026, 1, 5))
    )
    page = source("You may contest within 21 calendar days.")
    transport = Transport(response('{"findings": "not a list"}'))
    extractor = LlmExtractor(
        MiMoLlmClient(config(pricing=CHEAP), "offline-key", transport=transport)
    )
    result = extractor.extract(questions, [page])
    assert len(transport.payloads) == 1
    assert result.usage.cost_cents >= 1
    assert len(result.findings) == len(questions)
    assert all(finding.status is FindingStatus.UNKNOWN for finding in result.findings)
