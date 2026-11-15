from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import httpx2 as httpx
import openai
import pytest

from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import LlmBudgetExceeded, LlmProviderError, Usage, schema_json
from parkingbreaker.llm_deepseek import DeepSeekLlmClient
from parkingbreaker.llm_input_budget import (
    UnboundableRequestError,
    request_input_tokens,
)
from parkingbreaker.llm_models import (
    DEEPSEEK_CONTEXT_TOKENS,
    LlmModelConfig,
    ModelPricing,
    deepseek_flash_config,
    deepseek_v4_pro_config,
    request_output_tokens,
    usage_cost_cents,
    worst_case_cents,
)

PRICING = ModelPricing(Decimal("30"), Decimal("0.6"), Decimal("120"))
SYSTEM = "system ✓"
USER = "prompt 🙂"
SCHEMA: dict[str, JsonValue] = {"type": "object"}


def config(model_id: str = "deepseek-flash") -> LlmModelConfig:
    return (
        deepseek_flash_config(pricing=PRICING)
        if model_id == "deepseek-flash"
        else deepseek_v4_pro_config()
    )


def input_bound(
    system: str = SYSTEM, user: str = USER, schema: Mapping[str, JsonValue] = SCHEMA
) -> int:
    schema_text = schema_json(schema)
    return request_input_tokens(system, user, schema_text)


def output_for(ceiling: int | None) -> int:
    return request_output_tokens(config(), input_bound(), ceiling)


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
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    return openai.APIStatusError(
        "provider body", response=httpx.Response(status, request=request), body=None
    )


def test_payload_is_exact_chat_completions_json_mode() -> None:
    transport = Transport()
    client = DeepSeekLlmClient(config(), "offline-key", transport=transport)
    result, usage = client.complete_json("system ✓", "prompt 🙂", {"type": "object"})
    assert result == {"ok": True}
    assert usage == Usage(1000, 100, usage_cost_cents(PRICING, 1000, 0, 100))
    (payload,) = transport.payloads
    assert payload["model"] == "deepseek-flash"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["stream"] is False
    assert payload["max_completion_tokens"] == 8192
    messages = cast(list[dict[str, str]], payload["messages"])
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[1]["content"] == "prompt 🙂"
    assert schema_json({"type": "object"}) in messages[0]["content"]
    assert "JSON only" in messages[0]["content"]


def test_tiny_text_succeeds_at_25_cent_ceiling() -> None:
    transport = Transport()
    client = DeepSeekLlmClient(config(), "offline-key", transport=transport)
    result, usage = client.complete_json(SYSTEM, USER, SCHEMA, max_cost_cents=25)
    assert result == {"ok": True}
    assert usage.input_tokens == 1000
    (payload,) = transport.payloads
    sent = cast(int, payload["max_completion_tokens"])
    assert sent == output_for(25) == 8192
    assert sent >= 1024
    assert worst_case_cents(config(), input_bound(), sent) <= 25


def test_tighter_ceiling_reduces_output_and_stays_within_ceiling() -> None:
    loose = Transport()
    DeepSeekLlmClient(config(), "offline-key", transport=loose).complete_json(
        SYSTEM, USER, SCHEMA, max_cost_cents=25
    )
    tight = Transport()
    DeepSeekLlmClient(config(), "offline-key", transport=tight).complete_json(
        SYSTEM, USER, SCHEMA, max_cost_cents=1
    )
    loose_output = cast(int, loose.payloads[0]["max_completion_tokens"])
    tight_output = cast(int, tight.payloads[0]["max_completion_tokens"])
    assert tight_output < loose_output
    assert tight_output >= 1024
    assert tight_output == output_for(1)
    assert worst_case_cents(config(), input_bound(), tight_output) <= 1


def test_bound_counts_unicode_system_and_schema_in_transport_cap() -> None:
    ascii_transport = Transport()
    DeepSeekLlmClient(config(), "offline-key", transport=ascii_transport).complete_json(
        "s", "a" * 100, {}, max_cost_cents=1
    )
    unicode_transport = Transport()
    DeepSeekLlmClient(config(), "offline-key", transport=unicode_transport).complete_json(
        "s", "🙂" * 100, {}, max_cost_cents=1
    )
    ascii_output = cast(int, ascii_transport.payloads[0]["max_completion_tokens"])
    unicode_output = cast(int, unicode_transport.payloads[0]["max_completion_tokens"])
    assert ascii_output > unicode_output >= 1024
    assert unicode_output == request_output_tokens(config(), input_bound("s", "🙂" * 100, {}), 1)


def test_deepseek_refuses_image_attachments() -> None:
    from parkingbreaker.llm import ImageAttachment

    transport = Transport()
    client = DeepSeekLlmClient(config(), "offline-key", transport=transport)
    img = ImageAttachment(media_type="image/png", data_base64="AAAA")
    with pytest.raises(ValueError, match="does not support image attachments"):
        client.complete_json("s", "u", {}, images=[img])
    assert transport.payloads == []


def test_cached_input_subtracted() -> None:
    transport = Transport(
        response(usage=usage_obj(1_000_000, 500_000, cached=400_000, reasoning=100_000))
    )
    _, usage = DeepSeekLlmClient(config(), "offline-key", transport=transport).complete_json(
        "s", "u", {}
    )
    assert usage.input_tokens == 1_000_000
    assert usage.output_tokens == 500_000
    expected = usage_cost_cents(PRICING, 600_000, 400_000, 500_000)
    assert usage.cost_cents == expected


def test_provider_error_mapping() -> None:
    transport = Transport(error=api_error(429))
    client = DeepSeekLlmClient(config(), "offline-key", transport=transport)
    with pytest.raises(LlmProviderError) as exc_info:
        client.complete_json("s", "u", {})
    assert exc_info.value.retryable is True


def test_zero_ceiling_refused_before_transport() -> None:
    transport = Transport()
    client = DeepSeekLlmClient(config(), "offline-key", transport=transport)
    with pytest.raises(LlmBudgetExceeded):
        client.complete_json("s", "u", {}, max_cost_cents=0)
    assert transport.payloads == []


def test_negative_ceiling_refused_before_transport() -> None:
    transport = Transport()
    client = DeepSeekLlmClient(config(), "offline-key", transport=transport)
    with pytest.raises(ValueError, match="max_cost_cents"):
        client.complete_json("s", "u", {}, max_cost_cents=-1)
    assert transport.payloads == []


def test_genuinely_unaffordable_request_refused_before_transport() -> None:
    transport = Transport()
    client = DeepSeekLlmClient(config(), "offline-key", transport=transport)
    with pytest.raises(LlmBudgetExceeded):
        client.complete_json("s", "x" * 30_000, {}, max_cost_cents=1)
    assert transport.payloads == []


def test_oversized_text_refused_before_transport() -> None:
    transport = Transport()
    client = DeepSeekLlmClient(config(), "offline-key", transport=transport)
    with pytest.raises(UnboundableRequestError, match="context"):
        client.complete_json("s", "x" * 2_000_000, {})
    assert transport.payloads == []
    assert input_bound("s", "x" * 2_000_000, {}) > DEEPSEEK_CONTEXT_TOKENS


def test_oversized_text_under_ceiling_refused_before_transport() -> None:
    transport = Transport()
    client = DeepSeekLlmClient(config(), "offline-key", transport=transport)
    with pytest.raises(LlmBudgetExceeded):
        client.complete_json("s", "x" * 2_000_000, {}, max_cost_cents=25)
    assert transport.payloads == []
