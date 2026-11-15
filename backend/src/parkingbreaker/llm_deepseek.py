"""Offline DeepSeek Chat Completions boundary: one attempt, no tools or repairs.

The API endpoint is fixed by the model config (https://api.deepseek.com).
This adapter uses OpenAI SDK Chat Completions with json_object response format.
Cached input is charged at the cache rate; uncached input at the regular rate.
Customer text is neutralized to prevent delimiter breakouts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, cast

import openai

from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import (
    ImageAttachment,
    LlmProviderError,
    Usage,
    neutralise,
    parse_json_text,
    schema_json,
)
from parkingbreaker.llm_input_budget import (
    SCHEMA_INSTRUCTION,
    ensure_request_fits_context,
    text_request_tokens,
)
from parkingbreaker.llm_models import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_CONTEXT_TOKENS,
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
    """Standard OpenAI SDK client targeting DeepSeek."""

    def __init__(self, api_key: str) -> None:
        self._client = openai.OpenAI(
            api_key=api_key,
            base_url=DEEPSEEK_BASE_URL,
            max_retries=0,
            timeout=20.0,
        )

    def create(self, payload: Mapping[str, object]) -> object:
        api = cast(_ChatCompletionsApi, self._client.chat.completions)
        return api.create(**payload)


def _provider_error(exc: openai.APIError) -> LlmProviderError:
    status = getattr(exc, "status_code", None)
    retryable = (
        isinstance(exc, openai.APIConnectionError)
        or status == 429
        or (isinstance(status, int) and status >= 500)
    )
    return LlmProviderError("DeepSeek provider request failed", retryable=retryable)


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise LlmProviderError("DeepSeek response has invalid usage")
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
        raise LlmProviderError("DeepSeek response has invalid usage")
    cached = _optional_count(getattr(raw, "prompt_tokens_details", None), "cached_tokens")
    reasoning = _optional_count(getattr(raw, "completion_tokens_details", None), "reasoning_tokens")
    if cached is not None and cached > prompt:
        raise LlmProviderError("DeepSeek response has invalid usage")
    if reasoning is not None and reasoning > completion:
        raise LlmProviderError("DeepSeek response has invalid usage")
    if cached is None:
        if pricing.cached_input_cents_per_mtok > pricing.input_cents_per_mtok:
            ordinary, charged_cached = 0, prompt
        else:
            ordinary, charged_cached = prompt, 0
    else:
        ordinary, charged_cached = prompt - cached, cached
    cost_cents = usage_cost_cents(pricing, ordinary, charged_cached, completion)
    return Usage(prompt, completion, cost_cents)


def _unusable_contents(message: object) -> str | None:
    text = getattr(message, "content", None)
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    return None if size > MAX_TEXT_BYTES else text


class DeepSeekLlmClient:
    """One DeepSeek Chat Completions JSON call; immutable configuration."""

    def __init__(
        self,
        config: LlmModelConfig,
        api_key: str,
        *,
        transport: ChatTransport | None = None,
    ) -> None:
        if config.provider != "deepseek" or config.base_url != DEEPSEEK_BASE_URL:
            raise ValueError("DeepSeek client requires an explicit deepseek config")
        if not api_key.strip() or not valid_header_credential(api_key):
            raise ValueError("api_key must be a valid non-empty header credential")
        self._config = config
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
        images: Sequence[ImageAttachment] | None = None,
    ) -> tuple[JsonValue, Usage]:
        if images:
            raise ValueError("DeepSeek client does not support image attachments; use Gemini")
        schema_text = schema_json(schema)
        input_tokens = text_request_tokens(system, user, schema_text)
        output_tokens = request_output_tokens(self._config, input_tokens, max_cost_cents)
        ensure_request_fits_context(input_tokens, output_tokens, DEEPSEEK_CONTEXT_TOKENS)
        instructions = SCHEMA_INSTRUCTION + schema_text
        payload: dict[str, object] = {
            "model": self._config.model_id,
            "messages": [
                {"role": "system", "content": system + "\n" + instructions},
                {"role": "user", "content": neutralise(user)},
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": output_tokens,
            "stream": False,
        }
        try:
            response = self._transport.create(payload)
        except openai.APIError as exc:
            raise _provider_error(exc) from None

        raw_usage = getattr(response, "usage", None)
        if raw_usage is None:
            raise LlmProviderError("DeepSeek response missing usage")
        try:
            usage = _read_usage(raw_usage, self._config.pricing)
        except LlmProviderError:
            raise
        except Exception:
            raise LlmProviderError("DeepSeek response has invalid usage") from None

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
