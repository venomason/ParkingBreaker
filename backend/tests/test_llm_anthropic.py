from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Unpack

import anthropic
import httpx2
import pytest

from parkingbreaker import llm_anthropic as adapter
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import (
    LlmBudgetExceeded,
    LlmClient,
    LlmProviderError,
    Usage,
    conservative_input_tokens,
    schema_json,
)
from parkingbreaker.llm_models import (
    MINIMAX_PRICING,
    SONNET_5_5,
    LlmModelConfig,
    minimax_config,
    output_cap_for,
)
from parkingbreaker.research.extractor import FINDINGS_SCHEMA
from parkingbreaker.research.issuer_assessment import ISSUER_ASSESSMENT_SCHEMA
from parkingbreaker.research.link_assessment import LINK_ASSESSMENTS_SCHEMA

SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


@dataclass
class FakeUsage:
    input_tokens: int = 10000
    output_tokens: int = 2000
    cache_read_input_tokens: int | None = 100000
    cache_creation_input_tokens: int | None = 5000


@dataclass
class FakeMessage:
    usage: FakeUsage = field(default_factory=FakeUsage)
    stop_reason: str | None = "end_turn"
    content: list[object] = field(
        default_factory=lambda: [SimpleNamespace(type="text", text='{"ok": true}')],
    )


class FakeMessages:
    def __init__(self) -> None:
        self.count_calls: list[adapter.CountRequest] = []
        self.calls: list[adapter.MessageRequest] = []
        self.count = 500
        self.count_error: Exception | None = None
        self.error: Exception | None = None
        self.response = FakeMessage()

    def count_tokens(self, **kwargs: Unpack[adapter.CountRequest]) -> adapter.TokenCount:
        self.count_calls.append(kwargs)
        if self.count_error is not None:
            raise self.count_error
        return FakeUsage(input_tokens=self.count)

    def create(self, **kwargs: Unpack[adapter.MessageRequest]) -> adapter.MessageResponse:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


@dataclass
class FakeClient:
    messages: FakeMessages = field(default_factory=FakeMessages)


def test_minimax_images_reach_both_token_count_and_inference() -> None:
    from test_ticket_intake_service import FRONT, attachment

    image = attachment(FRONT)
    fake = FakeClient()
    fake.messages.response.usage = FakeUsage(100, 200, None, None)
    client = adapter.AnthropicLlmClient(minimax_config(), "offline-key", client=fake)
    result, _ = client.complete_json(
        "Copy ticket text only", "Read", SCHEMA, images=[image], max_cost_cents=3
    )
    assert result == {"ok": True}
    messages = fake.messages.calls[0]["messages"]
    assert messages == fake.messages.count_calls[0]["messages"]
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert content[1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": image.data_base64,
        },
    }
    assert len(fake.messages.calls) == 1


def test_minimax_failed_image_count_never_calls_inference() -> None:
    from test_ticket_intake_service import FRONT, attachment

    fake = FakeClient()
    fake.messages.count_error = RuntimeError("offline count failure")
    client = adapter.AnthropicLlmClient(minimax_config(), "offline-key", client=fake)
    with pytest.raises(LlmProviderError, match="count_tokens failed"):
        client.complete_json("Copy", "Read", SCHEMA, images=[attachment(FRONT)], max_cost_cents=3)
    assert fake.messages.calls == []


def test_structured_request_usage_and_ceiling() -> None:
    fake = FakeClient()
    fake.messages.response.usage = FakeUsage(100, 200, None, None)
    client: LlmClient = adapter.AnthropicLlmClient(SONNET_5_5, "PRIVATE", client=fake)
    result, usage = client.complete_json("system", "user", SCHEMA, max_cost_cents=3)
    assert result == {"ok": True}
    assert usage == Usage(100, 200, 1)
    request = fake.messages.calls[0]
    assert request == {
        "model": SONNET_5_5.model_id,
        "system": "system",
        "messages": [{"role": "user", "content": "user"}],
        "max_tokens": 2800,
        "output_config": {
            "format": {"type": "json_schema", "schema": SCHEMA},
            "effort": "medium",
        },
    }
    assert fake.messages.count_calls == [
        {
            "model": SONNET_5_5.model_id,
            "system": "system",
            "messages": request["messages"],
            "output_config": request["output_config"],
        }
    ]
    assert "thinking" not in request


def test_usage_includes_cache_reads_and_creation() -> None:
    fake = FakeClient()
    client = adapter.AnthropicLlmClient(SONNET_5_5, "PRIVATE", client=fake)
    _, usage = client.complete_json(
        "system",
        "user",
        SCHEMA,
    )
    assert usage == Usage(115000, 2000, 7)
    assert fake.messages.calls[0]["max_tokens"] == SONNET_5_5.max_output_tokens
    assert client.last_call is not None
    assert client.last_call.cost_status == "estimated"
    assert client.last_call.outcome == "completed"


@pytest.mark.parametrize(
    ("ceiling", "expected"),
    [(0, LlmBudgetExceeded), (1, LlmBudgetExceeded), (-1, ValueError)],
)
def test_unusable_ceiling_refused_before_any_request(
    ceiling: int, expected: type[Exception]
) -> None:
    fake = FakeClient()
    client = adapter.AnthropicLlmClient(SONNET_5_5, "PRIVATE", client=fake)
    with pytest.raises(expected):
        client.complete_json(
            "system",
            "user",
            SCHEMA,
            max_cost_cents=ceiling,
        )
    assert fake.messages.calls == []
    assert fake.messages.count_calls == []
    assert client.last_call is None


def test_preflight_rejection_clears_previous_call_metadata() -> None:
    fake = FakeClient()
    client = adapter.AnthropicLlmClient(SONNET_5_5, "PRIVATE", client=fake)
    client.complete_json("system", "user", SCHEMA)
    assert client.last_call is not None
    with pytest.raises(LlmBudgetExceeded):
        client.complete_json("system", "user", SCHEMA, max_cost_cents=0)
    assert client.last_call is None


@pytest.mark.parametrize("count", [5000, 15000])
def test_counted_input_can_prevent_generation(count: int) -> None:
    fake = FakeClient()
    fake.messages.count = count
    with pytest.raises(LlmBudgetExceeded):
        adapter.AnthropicLlmClient(SONNET_5_5, "PRIVATE", client=fake).complete_json(
            "system",
            "user",
            SCHEMA,
            max_cost_cents=2,
        )
    assert len(fake.messages.count_calls) == 1
    assert fake.messages.calls == []


@pytest.mark.parametrize(
    "schema",
    [
        FINDINGS_SCHEMA,
        LINK_ASSESSMENTS_SCHEMA,
        ISSUER_ASSESSMENT_SCHEMA,
        {"type": "object", "properties": {"x": {}}},
        {"type": ["string", "null"]},
    ],
)
def test_unsupported_schema_uses_instruction_json(schema: dict[str, JsonValue]) -> None:
    fake = FakeClient()
    adapter.AnthropicLlmClient(SONNET_5_5, "PRIVATE", client=fake).complete_json(
        "system",
        "user",
        schema,
    )
    request = fake.messages.calls[0]
    assert request["output_config"] == {"effort": "medium"}
    assert request["messages"][0]["content"] == (
        "user\nReply with JSON only matching this schema: " + schema_json(schema)
    )


def test_minimax_instruction_only_and_uncapped_and_capped() -> None:
    fake = FakeClient()
    config = minimax_config(SONNET_5_5.pricing)
    client = adapter.AnthropicLlmClient(config, "PRIVATE", client=fake)

    # Uncapped MiniMax call does not invoke count_tokens
    client.complete_json("sysé", "user🙂", SCHEMA)
    assert fake.messages.count_calls == []
    request = fake.messages.calls[0]
    assert request["output_config"] == {"effort": "medium"}

    # Capped MiniMax call uses count_tokens and effort=medium
    fake.messages.calls.clear()
    fake.messages.count = 600
    client.complete_json("sysé", "user🙂", SCHEMA, max_cost_cents=3)
    assert len(fake.messages.count_calls) == 1
    assert fake.messages.count_calls[0]["output_config"] == {"effort": "medium"}
    request2 = fake.messages.calls[0]
    assert request2["max_tokens"] == output_cap_for(config, 1100, 3)
    assert client.last_call is not None
    assert client.last_call.pricing_source == "caller_supplied_unverified"


def test_minimax_default_pricing_metadata() -> None:
    fake = FakeClient()
    config = minimax_config()
    client = adapter.AnthropicLlmClient(config, "PRIVATE", client=fake)
    client.complete_json("sys", "user", SCHEMA)
    assert client.last_call is not None
    assert client.last_call.provider == "minimax"
    assert client.last_call.pricing_source == "https://platform.minimax.io/docs/pricing/overview"
    assert client.last_call.pricing_version == "2026-10-19"
    assert client.last_call.currency == "USD"


def test_explicit_minimax_price_override_is_unverified_even_if_values_match() -> None:
    fake = FakeClient()
    client = adapter.AnthropicLlmClient(minimax_config(MINIMAX_PRICING), "PRIVATE", client=fake)
    client.complete_json("sys", "user", SCHEMA)
    assert client.last_call is not None
    assert client.last_call.pricing_source == "caller_supplied_unverified"
    assert client.last_call.pricing_version == "caller_supplied_unverified"


def test_minimax_count_failure_raises_provider_error() -> None:
    fake = FakeClient()
    fake.messages.count_error = RuntimeError("count endpoint unavailable")
    config = minimax_config()
    client = adapter.AnthropicLlmClient(config, "PRIVATE", client=fake)
    with pytest.raises(LlmProviderError, match="MiniMax count_tokens failed"):
        client.complete_json("sys", "user", SCHEMA, max_cost_cents=3)
    assert fake.messages.calls == []


@pytest.mark.parametrize("bad_count", [-1, "100", True, None])
def test_minimax_invalid_count_raises_provider_error(bad_count: object) -> None:
    fake = FakeClient()
    fake.messages.count = bad_count  # type: ignore[assignment]
    config = minimax_config()
    client = adapter.AnthropicLlmClient(config, "PRIVATE", client=fake)
    with pytest.raises(LlmProviderError, match="MiniMax count_tokens failed"):
        client.complete_json("sys", "user", SCHEMA, max_cost_cents=3)
    assert fake.messages.calls == []


def test_count_failure_uses_conservative_estimate_for_sonnet() -> None:
    fake = FakeClient()
    fake.messages.count_error = RuntimeError("count unsupported")
    adapter.AnthropicLlmClient(SONNET_5_5, "PRIVATE", client=fake).complete_json(
        "system",
        "user",
        SCHEMA,
        max_cost_cents=3,
    )
    estimate = conservative_input_tokens("system", "user", schema_json(SCHEMA))
    assert fake.messages.calls[0]["max_tokens"] == output_cap_for(SONNET_5_5, estimate, 3)


def test_fallback_large_input_refused_without_generation() -> None:
    fake = FakeClient()
    fake.messages.count_error = RuntimeError("count unsupported")
    with pytest.raises(LlmBudgetExceeded):
        adapter.AnthropicLlmClient(SONNET_5_5, "PRIVATE", client=fake).complete_json(
            "system",
            "x" * 100000,
            SCHEMA,
            max_cost_cents=3,
        )
    assert fake.messages.calls == []


@pytest.mark.parametrize("stop", ["refusal", "max_tokens", "pause_turn", "tool_use"])
def test_refusal_and_incomplete_turns_drop_json(stop: str) -> None:
    fake = FakeClient()
    fake.messages.response.stop_reason = stop
    client = adapter.AnthropicLlmClient(SONNET_5_5, "PRIVATE", client=fake)
    res, usage = client.complete_json(
        "system",
        "user",
        SCHEMA,
    )
    assert res == {}
    assert usage == Usage(115000, 2000, 7)
    assert client.last_call is not None
    assert client.last_call.outcome == "incomplete"


@pytest.mark.parametrize(
    ("text", "result"),
    [
        ("not json", {}),
        ('{"ok":', {}),
        ("NaN", {}),
        ('  ```json\n{"ok": true}\n```  ', {"ok": True}),
    ],
)
def test_parsing_drops_invalid_json_and_strips_fence(text: str, result: JsonValue) -> None:
    fake = FakeClient()
    fake.messages.response.content = [SimpleNamespace(type="text", text=text)]
    assert adapter.AnthropicLlmClient(SONNET_5_5, "PRIVATE", client=fake).complete_json(
        "system",
        "user",
        SCHEMA,
    ) == (result, Usage(115000, 2000, 7))
    assert len(fake.messages.calls) == 1


def test_oversized_text_block_dropped() -> None:
    fake = FakeClient()
    fake.messages.response.content = [
        SimpleNamespace(type="text", text='{"ok": true}'),
        SimpleNamespace(type="text", text="é" * 524_288),
    ]
    client = adapter.AnthropicLlmClient(SONNET_5_5, "PRIVATE", client=fake)
    result, usage = client.complete_json("system", "user", SCHEMA)
    assert result == {}
    assert usage == Usage(115000, 2000, 7)
    assert client.last_call is not None
    assert client.last_call.outcome == "invalid_response"
    assert client.last_call.cost_status == "estimated"


@pytest.mark.parametrize("bad_input", [-1, "100", True, None])
def test_invalid_response_usage_raises_provider_error(bad_input: object) -> None:
    fake = FakeClient()
    fake.messages.response.usage = SimpleNamespace(
        input_tokens=bad_input, output_tokens=100, cache_read_input_tokens=0
    )  # type: ignore[assignment]
    client = adapter.AnthropicLlmClient(SONNET_5_5, "PRIVATE", client=fake)
    with pytest.raises(LlmProviderError, match="usage"):
        client.complete_json("system", "user", SCHEMA)
    assert client.last_call is not None
    assert client.last_call.outcome == "invalid_response"
    assert client.last_call.cost_status == "pending"


def test_only_first_text_block_is_parsed() -> None:
    fake = FakeClient()
    fake.messages.response.content = [
        SimpleNamespace(type="thinking"),
        SimpleNamespace(type="text", text="invalid"),
        SimpleNamespace(type="text", text='{"ok": true}'),
    ]
    client = adapter.AnthropicLlmClient(SONNET_5_5, "PRIVATE", client=fake)
    assert client.complete_json("system", "user", SCHEMA)[0] == {}
    assert client.last_call is not None
    assert client.last_call.outcome == "invalid_response"
    fake.messages.response.content = []
    assert client.complete_json("system", "user", SCHEMA)[0] == {}
    assert client.last_call is not None
    assert client.last_call.outcome == "invalid_response"


@pytest.mark.parametrize(
    ("name", "status", "retryable"),
    [
        ("AuthenticationError", 401, False),
        ("PermissionDeniedError", 403, False),
        ("BadRequestError", 400, False),
        ("NotFoundError", 404, False),
        ("RateLimitError", 429, True),
        ("APIStatusError", 503, True),
        ("APIStatusError", 409, False),
        ("APIConnectionError", 0, True),
    ],
)
def test_provider_errors_sanitized_and_never_retried(
    name: str,
    status: int,
    retryable: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeClient()
    request = httpx2.Request("POST", "https://offline.invalid")
    if status:
        cls = getattr(anthropic, name)
        response = httpx2.Response(status, request=request)
        fake.messages.error = cls("PRIVATE", response=response, body={"secret": "PRIVATE"})
    else:
        fake.messages.error = anthropic.APIConnectionError(message="PRIVATE", request=request)
    client = adapter.AnthropicLlmClient(SONNET_5_5, "PRIVATE", client=fake)
    with pytest.raises(LlmProviderError) as failure:
        client.complete_json(
            "system",
            "user",
            SCHEMA,
        )
    assert failure.value.retryable is retryable
    assert "PRIVATE" not in str(failure.value)
    assert "PRIVATE" not in caplog.text
    assert len(fake.messages.calls) == 1
    assert client.last_call is not None
    assert client.last_call.cost_status == "pending"
    assert client.last_call.outcome == "pending"


def test_custom_sonnet_base_url_preserves_count_fallback() -> None:
    fake = FakeClient()
    config = replace(SONNET_5_5, base_url="https://offline.invalid")
    client = adapter.AnthropicLlmClient(config, "PRIVATE", client=fake)
    client.complete_json("system", "user", SCHEMA, max_cost_cents=3)
    estimate = conservative_input_tokens("system", "user", schema_json(SCHEMA))
    assert fake.messages.count_calls == []
    assert fake.messages.calls[0]["max_tokens"] == output_cap_for(config, estimate, 3)


@pytest.mark.parametrize("config", [SONNET_5_5, minimax_config(SONNET_5_5.pricing)])
def test_sdk_constructor_retries_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
    config: LlmModelConfig,
) -> None:
    fake = FakeClient()
    calls: list[dict[str, object]] = []

    def build(**kwargs: object) -> FakeClient:
        calls.append(kwargs)
        return fake

    monkeypatch.setattr(adapter.anthropic, "Anthropic", build)
    adapter.AnthropicLlmClient(config, "PRIVATE").complete_json("system", "user", SCHEMA)
    assert calls == [
        {
            "api_key": "PRIVATE",
            "base_url": config.base_url,
            "max_retries": 0,
            "timeout": 20.0,
        }
    ]


def test_no_effort_and_config_validation() -> None:
    fake = FakeClient()
    adapter.AnthropicLlmClient(
        replace(SONNET_5_5, effort=None), "PRIVATE", client=fake
    ).complete_json(
        "system",
        "user",
        SCHEMA,
    )
    assert "effort" not in fake.messages.calls[0]["output_config"]
    with pytest.raises(ValueError, match="config"):
        adapter.AnthropicLlmClient(replace(SONNET_5_5, provider="openai"), "PRIVATE", client=fake)
    with pytest.raises(ValueError, match="api_key"):
        adapter.AnthropicLlmClient(SONNET_5_5, " ", client=fake)
