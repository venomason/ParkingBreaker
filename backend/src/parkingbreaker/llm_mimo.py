"""Offline Xiaomi MiMo Chat Completions boundary: one attempt, no tools or repairs.

The ordinary API key endpoint is fixed by the model config. This adapter never uses
Responses, retries, streaming, tools, web search, multimodal input or schema repair.
Cached input is charged at the cache rate; a missing cache breakdown is charged
conservatively at the higher of the ordinary and cached input rates.
"""

from collections.abc import Mapping
from typing import Protocol, cast

import openai as openai

from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import LlmProviderError, Usage, parse_json_text, schema_json
from parkingbreaker.llm_models import (
    MIMO_BASE_URL,
    MIMO_CONTEXT_TOKENS,
    LlmModelConfig,
    ModelPricing,
    request_output_tokens,
    usage_cost_cents,
)
from parkingbreaker.provider_credentials import valid_header_credential

MAX_TEXT_BYTES = 1_048_576


class ChatTransport(Protocol):
    """Injected seam receiving the exact Chat Completions wire payload."""

    def create(self, payload: Mapping[str, object]) -> object: ...


class _ChatCompletionsApi(Protocol):
    def create(self, **kwargs: object) -> object: ...


class _SdkChatTransport:
    """Ordinary SDK construction; ``thinking`` travels via ``extra_body``."""

    def __init__(self, api_key: str) -> None:
        self._client = openai.OpenAI(
            api_key=api_key,
            base_url=MIMO_BASE_URL,
            max_retries=0,
            timeout=20.0,
        )

    def create(self, payload: Mapping[str, object]) -> object:
        body = dict(payload)
        thinking = body.pop("thinking", None)
        extra_body = None if thinking is None else {"thinking": thinking}
        api = cast(_ChatCompletionsApi, self._client.chat.completions)
        return api.create(**body, extra_body=extra_body)


def _provider_error(exc: openai.APIError) -> LlmProviderError:
    status = getattr(exc, "status_code", None)
    retryable = (
        isinstance(exc, openai.APIConnectionError)
        or status == 429
        or (isinstance(status, int) and status >= 500)
    )
    return LlmProviderError("MiMo provider request failed", retryable=retryable)


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise LlmProviderError("MiMo response has invalid usage")
    return value


def _optional_count(holder: object, attribute: str) -> int | None:
    if holder is None:
        return None
    value = getattr(holder, attribute, None)
    if value is None:
        return None
    return _count(value)


def _read_usage(raw: object, pricing: ModelPricing) -> Usage:
    prompt = _count(getattr(raw, "prompt_tokens", None))
    completion = _count(getattr(raw, "completion_tokens", None))
    total = getattr(raw, "total_tokens", None)
    if total is not None and _count(total) != prompt + completion:
        raise LlmProviderError("MiMo response has invalid usage")
    cached = _optional_count(getattr(raw, "prompt_tokens_details", None), "cached_tokens")
    reasoning = _optional_count(getattr(raw, "completion_tokens_details", None), "reasoning_tokens")
    if cached is not None and cached > prompt:
        raise LlmProviderError("MiMo response has invalid usage")
    if reasoning is not None and reasoning > completion:
        raise LlmProviderError("MiMo response has invalid usage")
    if cached is None:
        # No authoritative cache count: charge every prompt token at the higher of the
        # ordinary and cache input rates, still with one upward rounding.
        if pricing.cached_input_cents_per_mtok > pricing.input_cents_per_mtok:
            ordinary, charged_cached = 0, prompt
        else:
            ordinary, charged_cached = prompt, 0
    else:
        ordinary, charged_cached = prompt - cached, cached
    cost_cents = usage_cost_cents(pricing, ordinary, charged_cached, completion)
    return Usage(prompt, completion, cost_cents)


def _unusable_contents(message: object) -> str | None:
    """Return a bounded text body only for one well-formed assistant text choice."""
    text = getattr(message, "content", None)
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    return None if size > MAX_TEXT_BYTES else text


class MiMoLlmClient:
    """One MiMo Chat Completions JSON call; thinking is constructor-owned."""

    def __init__(
        self,
        config: LlmModelConfig,
        api_key: str,
        *,
        transport: ChatTransport | None = None,
        thinking: bool = False,
    ) -> None:
        if config.provider != "mimo" or config.base_url != MIMO_BASE_URL:
            raise ValueError("MiMo client requires an explicit mimo config")
        if not api_key.strip() or not valid_header_credential(api_key):
            raise ValueError("api_key must be a valid non-empty header credential")
        if type(thinking) is not bool:
            raise ValueError("thinking must be an explicit boolean")
        self._config = config
        self._thinking = thinking
        self._transport: ChatTransport = (
            transport if transport is not None else _SdkChatTransport(api_key)
        )

    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
    ) -> tuple[JsonValue, Usage]:
        # The full documented text context is the safe offline input reservation. No
        # bytes/2 heuristic is treated as a proved MiMo upper bound.
        output_tokens = request_output_tokens(self._config, MIMO_CONTEXT_TOKENS, max_cost_cents)
        schema_text = schema_json(schema)
        instructions = "Reply with JSON only matching this schema: " + schema_text
        payload: dict[str, object] = {
            "model": self._config.model_id,
            "messages": [
                {"role": "system", "content": system + "\n" + instructions},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": output_tokens,
            "stream": False,
            "thinking": {"type": "enabled" if self._thinking else "disabled"},
        }
        try:
            response = self._transport.create(payload)
        except openai.APIError as exc:
            raise _provider_error(exc) from None

        raw_usage = getattr(response, "usage", None)
        if raw_usage is None:
            raise LlmProviderError("MiMo response missing usage")
        try:
            usage = _read_usage(raw_usage, self._config.pricing)
        except LlmProviderError:
            raise
        except Exception:
            raise LlmProviderError("MiMo response has invalid usage") from None

        choices = getattr(response, "choices", None)
        if not isinstance(choices, list) or len(choices) != 1:
            return {}, usage
        choice = choices[0]
        if getattr(choice, "finish_reason", None) != "stop":
            return {}, usage
        message = getattr(choice, "message", None)
        if message is None or getattr(message, "role", None) != "assistant":
            return {}, usage
        if getattr(message, "tool_calls", None) not in (None, [], ()):
            return {}, usage
        if getattr(message, "refusal", None) is not None:
            return {}, usage
        text = _unusable_contents(message)
        if text is None:
            return {}, usage
        return parse_json_text(text), usage
