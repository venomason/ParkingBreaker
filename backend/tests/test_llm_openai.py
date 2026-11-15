from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Unpack

import httpx2
import pytest

from parkingbreaker import llm_openai as adapter
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import (
    LlmBudgetExceeded,
    LlmClient,
    LlmProviderError,
    Usage,
    conservative_input_tokens,
    schema_json,
)
from parkingbreaker.llm_models import GPT_6_LUNA, LlmModelConfig, output_cap_for
from parkingbreaker.research.extractor import FINDINGS_SCHEMA


@dataclass
class FakeDetails:
    cached_tokens: int = 50000


@dataclass
class FakeUsage:
    input_tokens: int = 100000
    input_tokens_details: FakeDetails = field(default_factory=FakeDetails)
    output_tokens: int = 10000
    # Already included in output_tokens, so it must never be added again.
    output_tokens_details: object = field(
        default_factory=lambda: SimpleNamespace(reasoning_tokens=9000),
    )


@dataclass
class FakeResponse:
    usage: FakeUsage | None = field(default_factory=FakeUsage)
    status: str | None = "completed"
    output_text: str = '{"ok": true}'
    output: list[object] = field(default_factory=list)


class FakeResponses:
    def __init__(self) -> None:
        self.calls: list[adapter.ResponseRequest] = []
        self.response = FakeResponse()
        self.error: Exception | None = None

    def create(self, **kwargs: Unpack[adapter.ResponseRequest]) -> adapter.Response:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


@dataclass
class FakeClient:
    responses: FakeResponses = field(default_factory=FakeResponses)


def test_responses_request_schema_effort_and_ceiling() -> None:
    fake = FakeClient()
    fake.responses.response.usage = FakeUsage(100, FakeDetails(50), 200)
    client: LlmClient = adapter.OpenAiLlmClient(GPT_6_LUNA, "PRIVATE", client=fake)
    assert client.complete_json("system", "user", FINDINGS_SCHEMA, max_cost_cents=1) == (
        {"ok": True},
        Usage(100, 200, 1),
    )
    estimate = conservative_input_tokens("system", "user", schema_json(FINDINGS_SCHEMA))
    cap = output_cap_for(GPT_6_LUNA, estimate, 1)
    assert cap is not None
    assert fake.responses.calls == [
        {
            "model": GPT_6_LUNA.model_id,
            "instructions": "system",
            "input": "user",
            "max_output_tokens": min(cap, GPT_6_LUNA.max_output_tokens),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "result",
                    "schema": FINDINGS_SCHEMA,
                    "strict": False,
                }
            },
            "reasoning": {"effort": "medium"},
        }
    ]


def test_ceiling_reduces_output_tokens_and_no_effort_when_unset() -> None:
    fake = FakeClient()
    # Large prompt costs 0.95 cents, leaving 1000 tokens: too few for the minimum.
    with pytest.raises(LlmBudgetExceeded):
        adapter.OpenAiLlmClient(GPT_6_LUNA, "PRIVATE", client=fake).complete_json(
            "",
            "x" * 189000,
            {},
            max_cost_cents=1,
        )
    assert fake.responses.calls == []
    config = replace(GPT_6_LUNA, effort=None)
    adapter.OpenAiLlmClient(config, "PRIVATE", client=fake).complete_json(
        "",
        "x" * 180000,
        {},
        max_cost_cents=1,
    )
    request = fake.responses.calls[0]
    estimate = conservative_input_tokens("", "x" * 180000, "{}")
    assert request["max_output_tokens"] == output_cap_for(config, estimate, 1)
    assert request["max_output_tokens"] < config.max_output_tokens
    assert "reasoning" not in request


def test_usage_cost_cache_discount_and_inclusive_reasoning_output() -> None:
    fake = FakeClient()
    assert adapter.OpenAiLlmClient(GPT_6_LUNA, "PRIVATE", client=fake).complete_json(
        "system",
        "user",
        FINDINGS_SCHEMA,
    ) == ({"ok": True}, Usage(100000, 10000, 2))
    assert fake.responses.calls[0]["max_output_tokens"] == GPT_6_LUNA.max_output_tokens


@pytest.mark.parametrize(("ceiling", "expected"), [(0, LlmBudgetExceeded), (-1, ValueError)])
def test_invalid_ceiling_refused_before_any_request(
    ceiling: int, expected: type[Exception]
) -> None:
    fake = FakeClient()
    with pytest.raises(expected):
        adapter.OpenAiLlmClient(GPT_6_LUNA, "PRIVATE", client=fake).complete_json(
            "system",
            "user",
            FINDINGS_SCHEMA,
            max_cost_cents=ceiling,
        )
    assert fake.responses.calls == []


def test_small_cap_below_minimum_and_input_exceeding_ceiling_refused() -> None:
    fake = FakeClient()
    client = adapter.OpenAiLlmClient(GPT_6_LUNA, "PRIVATE", client=fake)
    for text in ("x" * 189000, "x" * 300000):
        with pytest.raises(LlmBudgetExceeded):
            client.complete_json("system", text, FINDINGS_SCHEMA, max_cost_cents=1)
    assert fake.responses.calls == []


@pytest.mark.parametrize("status", ["incomplete", "failed", "refused", "in_progress", None])
def test_incomplete_or_failed_response_discards_valid_json(status: str | None) -> None:
    fake = FakeClient()
    fake.responses.response.status = status
    assert adapter.OpenAiLlmClient(GPT_6_LUNA, "PRIVATE", client=fake).complete_json(
        "system",
        "user",
        FINDINGS_SCHEMA,
    ) == ({}, Usage(100000, 10000, 2))
    assert len(fake.responses.calls) == 1


@pytest.mark.parametrize("refused", [True, False])
def test_message_refusal_or_incomplete_message_discards_aggregated_json(refused: bool) -> None:
    fake = FakeClient()
    fake.responses.response.output = [
        SimpleNamespace(type="reasoning"),  # No message content/status attributes.
        SimpleNamespace(
            type="message",
            status="completed" if refused else "incomplete",
            content=[SimpleNamespace(type="refusal" if refused else "output_text")],
        ),
    ]
    assert adapter.OpenAiLlmClient(GPT_6_LUNA, "PRIVATE", client=fake).complete_json(
        "system",
        "user",
        FINDINGS_SCHEMA,
    ) == ({}, Usage(100000, 10000, 2))
    assert len(fake.responses.calls) == 1


def test_completed_text_message_with_reasoning_item_is_usable() -> None:
    fake = FakeClient()
    fake.responses.response.output = [
        SimpleNamespace(type="reasoning"),
        SimpleNamespace(
            type="message", status="completed", content=[SimpleNamespace(type="output_text")]
        ),
    ]
    assert adapter.OpenAiLlmClient(GPT_6_LUNA, "PRIVATE", client=fake).complete_json(
        "system",
        "user",
        FINDINGS_SCHEMA,
    )[0] == {"ok": True}


@pytest.mark.parametrize(
    ("text", "result"),
    [("invalid", {}), ('{"ok":', {}), ("NaN", {}), ('  ```json\n{"ok": true}\n``` ', {"ok": True})],
)
def test_invalid_and_fenced_json(text: str, result: JsonValue) -> None:
    fake = FakeClient()
    fake.responses.response.output_text = text
    assert adapter.OpenAiLlmClient(GPT_6_LUNA, "PRIVATE", client=fake).complete_json(
        "system",
        "user",
        FINDINGS_SCHEMA,
    ) == (result, Usage(100000, 10000, 2))
    assert len(fake.responses.calls) == 1


def test_instruction_only_config() -> None:
    fake = FakeClient()
    adapter.OpenAiLlmClient(
        replace(GPT_6_LUNA, structured_output=False),
        "PRIVATE",
        client=fake,
    ).complete_json("system", "user", FINDINGS_SCHEMA)
    request = fake.responses.calls[0]
    assert "text" not in request
    assert request["input"] == (
        "user\nReply with JSON only matching this schema: " + schema_json(FINDINGS_SCHEMA)
    )


@pytest.mark.parametrize(
    ("name", "status", "retryable"),
    [
        ("AuthenticationError", 401, False),
        ("PermissionDeniedError", 403, False),
        ("BadRequestError", 400, False),
        ("NotFoundError", 404, False),
        ("RateLimitError", 429, True),
        ("APIStatusError", 500, True),
        ("APIStatusError", 422, False),
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
        cls = getattr(adapter.openai, name)
        fake.responses.error = cls(
            "PRIVATE", response=httpx2.Response(status, request=request), body={"secret": "PRIVATE"}
        )
    else:
        fake.responses.error = adapter.openai.APIConnectionError(message="PRIVATE", request=request)
    with pytest.raises(LlmProviderError) as failure:
        adapter.OpenAiLlmClient(GPT_6_LUNA, "PRIVATE", client=fake).complete_json(
            "system",
            "user",
            FINDINGS_SCHEMA,
        )
    assert failure.value.retryable is retryable
    assert "PRIVATE" not in str(failure.value)
    assert "PRIVATE" not in caplog.text
    assert len(fake.responses.calls) == 1


def test_missing_usage_is_provider_failure_without_retry() -> None:
    fake = FakeClient()
    fake.responses.response.usage = None
    with pytest.raises(LlmProviderError, match="missing usage"):
        adapter.OpenAiLlmClient(GPT_6_LUNA, "PRIVATE", client=fake).complete_json(
            "system",
            "user",
            FINDINGS_SCHEMA,
        )
    assert len(fake.responses.calls) == 1


def test_sdk_constructor_disables_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient()
    calls: list[dict[str, object]] = []

    def build(**kwargs: object) -> FakeClient:
        calls.append(kwargs)
        return fake

    monkeypatch.setattr(adapter.openai, "OpenAI", build)
    adapter.OpenAiLlmClient(GPT_6_LUNA, "PRIVATE").complete_json("system", "user", FINDINGS_SCHEMA)
    assert calls == [{"api_key": "PRIVATE", "max_retries": 0}]


@pytest.mark.parametrize(
    "config",
    [
        replace(GPT_6_LUNA, provider="anthropic"),
        replace(GPT_6_LUNA, base_url="https://offline.invalid"),
    ],
)
def test_invalid_config(config: LlmModelConfig) -> None:
    with pytest.raises(ValueError, match="config"):
        adapter.OpenAiLlmClient(config, "PRIVATE", client=FakeClient())


def test_empty_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        adapter.OpenAiLlmClient(GPT_6_LUNA, " ", client=FakeClient())
