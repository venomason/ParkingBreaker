"""The sole Anthropic SDK boundary, also used by MiniMax's compatible endpoint.

Empty dicts anywhere in a schema, or a list-valued ``type``, select instruction-only
JSON for that call. This deliberately conservative rule includes FINDINGS_SCHEMA
and both assessment schemas; no schema rewriting, thinking toggle or repair calls.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, NotRequired, Protocol, TypedDict, Unpack, cast

import anthropic as anthropic

from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import (
    ImageAttachment,
    LlmProviderError,
    Usage,
    conservative_input_tokens,
    parse_json_text,
    schema_json,
)
from parkingbreaker.llm_input_budget import image_attachment_tokens
from parkingbreaker.llm_models import (
    MINIMAX_BASE_URL,
    SONNET_5_5,
    LlmModelConfig,
    request_output_tokens,
    usage_cost_cents,
    validate_ceiling,
    worst_case_cents,
)
from parkingbreaker.native_search import (
    SEARCH_INSTRUCTIONS,
    SEARCH_MAX_COST_CENTS,
    field,
    items,
    search_query,
    search_reservation,
    url_hits,
)
from parkingbreaker.research.search import SearchError, SearchHit, SearchRequest

MAX_TEXT_BYTES = 1_048_576


@dataclass(frozen=True)
class LlmCallMetadata:
    provider: str
    model_id: str
    pricing_source: str
    pricing_version: str
    currency: str
    cost_status: Literal["estimated", "pending"]
    reserved_cost_cents: int
    usage: Usage | None
    outcome: str


class MessageInput(TypedDict):
    role: Literal["user"]
    content: str | list[dict[str, JsonValue]]


class CountRequest(TypedDict):
    model: str
    system: str
    messages: list[MessageInput]
    output_config: NotRequired[dict[str, JsonValue]]


class MessageRequest(CountRequest):
    max_tokens: int
    tools: NotRequired[list[dict[str, JsonValue]]]
    tool_choice: NotRequired[dict[str, str]]


class TokenCount(Protocol):
    @property
    def input_tokens(self) -> int: ...


class MessageUsage(TokenCount, Protocol):
    @property
    def output_tokens(self) -> int: ...

    @property
    def cache_read_input_tokens(self) -> int | None: ...

    @property
    def cache_creation_input_tokens(self) -> int | None: ...


class MessageResponse(Protocol):
    @property
    def usage(self) -> MessageUsage: ...

    @property
    def stop_reason(self) -> str | None: ...

    @property
    def content(self) -> Sequence[object]: ...


class MessagesApi(Protocol):
    def count_tokens(self, **kwargs: Unpack[CountRequest]) -> TokenCount: ...

    def create(self, **kwargs: Unpack[MessageRequest]) -> MessageResponse: ...


class AnthropicSdkClient(Protocol):
    @property
    def messages(self) -> MessagesApi: ...


def _supports_structured_output(value: JsonValue) -> bool:
    if isinstance(value, dict):
        return (
            bool(value)
            and not isinstance(value.get("type"), list)
            and all(_supports_structured_output(child) for child in value.values())
        )
    if isinstance(value, list):
        return all(_supports_structured_output(child) for child in value)
    return True


def _provider_error(
    message: str = "Anthropic provider request failed", *, retryable: bool = False
) -> LlmProviderError:
    return LlmProviderError(message, retryable=retryable)


def _convert_provider_error(exc: anthropic.APIError) -> LlmProviderError:
    status_code = getattr(exc, "status_code", None)
    retryable = isinstance(exc, anthropic.RateLimitError | anthropic.APIConnectionError) or (
        isinstance(exc, anthropic.APIStatusError) and status_code is not None and status_code >= 500
    )
    return _provider_error("Anthropic provider request failed", retryable=retryable)


def _validate_count(val: object) -> int:
    if type(val) is not int or val < 0:
        raise ValueError("Invalid token count")
    return val


def _validate_optional_cache_count(val: object) -> int:
    if val is None:
        return 0
    if type(val) is not int or val < 0:
        raise ValueError("Invalid cache token count")
    return val


class AnthropicLlmClient:
    last_call: LlmCallMetadata | None

    def __init__(
        self,
        config: LlmModelConfig,
        api_key: str,
        *,
        client: AnthropicSdkClient | None = None,
    ) -> None:
        if config.provider != "anthropic":
            raise ValueError("Anthropic client requires an anthropic config")
        if not api_key.strip():
            raise ValueError("api_key must be non-empty")
        self._config = config
        self.last_call = None
        # A single structural cast isolates the SDK overloads from the fake seam.
        self._client = (
            client
            if client is not None
            else cast(
                AnthropicSdkClient,
                anthropic.Anthropic(
                    api_key=api_key,
                    base_url=config.base_url,
                    max_retries=0,
                    timeout=20.0,
                ),
            )
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
        # Do not let a preflight/count failure expose metadata from an earlier call.
        self.last_call = None
        # Reject zero/negative/unusable ceilings before even the free token-count call.
        request_output_tokens(self._config, 0, max_cost_cents)
        schema_text = schema_json(schema)
        structured = self._config.structured_output and _supports_structured_output(dict(schema))
        if not structured:
            user += "\nReply with JSON only matching this schema: " + schema_text
        content: str | list[dict[str, JsonValue]] = user
        if images:
            if self._config.base_url != MINIMAX_BASE_URL:
                raise ValueError("image input is qualified only for the MiniMax boundary")
            image_attachment_tokens(images)
            content = [{"type": "text", "text": user}]
            content.extend(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.media_type,
                        "data": image.data_base64,
                    },
                }
                for image in images
            )
        request: MessageRequest = {
            "model": self._config.model_id,
            "system": system,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": self._config.max_output_tokens,
        }
        output_config: dict[str, JsonValue] = {}
        if structured:
            output_config["format"] = {"type": "json_schema", "schema": dict(schema)}
        if self._config.effort is not None:
            output_config["effort"] = self._config.effort
        if output_config:
            request["output_config"] = output_config

        estimate = conservative_input_tokens(system, user, schema_text)
        is_minimax = self._config.base_url == MINIMAX_BASE_URL
        count_request: CountRequest = {
            "model": request["model"],
            "system": system,
            "messages": request["messages"],
        }
        if output_config:
            count_request["output_config"] = output_config

        if is_minimax and (max_cost_cents is not None or images):
            try:
                raw_counted = self._client.messages.count_tokens(**count_request).input_tokens
                counted = _validate_count(raw_counted)
                estimate = counted + 500
            except Exception:
                raise _provider_error("MiniMax count_tokens failed") from None
        elif self._config.base_url is None:
            try:
                raw_counted = self._client.messages.count_tokens(**count_request).input_tokens
                counted = _validate_count(raw_counted)
                estimate = counted + 500
            except Exception:
                # Unsupported/unavailable count endpoint: use bytes/2 plus 500 instead.
                pass

        request["max_tokens"] = request_output_tokens(self._config, estimate, max_cost_cents)

        pricing_source = self._config.pricing_source or "caller_supplied_unverified"
        pricing_version = self._config.pricing_version or "caller_supplied_unverified"
        provider_name = "minimax" if is_minimax else "anthropic"

        reserved_cents = worst_case_cents(self._config, estimate, request["max_tokens"])

        self.last_call = LlmCallMetadata(
            provider=provider_name,
            model_id=self._config.model_id,
            pricing_source=pricing_source,
            pricing_version=pricing_version,
            currency="USD",
            cost_status="pending",
            reserved_cost_cents=reserved_cents,
            usage=None,
            outcome="pending",
        )

        try:
            response = self._client.messages.create(**request)
        except anthropic.APIError as exc:
            raise _convert_provider_error(exc) from None

        try:
            raw_usage = response.usage
            input_tokens = _validate_count(getattr(raw_usage, "input_tokens", None))
            output_tokens = _validate_count(getattr(raw_usage, "output_tokens", None))
            cache_read = _validate_optional_cache_count(
                getattr(raw_usage, "cache_read_input_tokens", None)
            )
            cache_creation = _validate_optional_cache_count(
                getattr(raw_usage, "cache_creation_input_tokens", None)
            )
        except Exception:
            self.last_call = LlmCallMetadata(
                provider=provider_name,
                model_id=self._config.model_id,
                pricing_source=pricing_source,
                pricing_version=pricing_version,
                currency="USD",
                cost_status="pending",
                reserved_cost_cents=reserved_cents,
                usage=None,
                outcome="invalid_response",
            )
            raise _provider_error("Invalid or missing usage in response") from None

        ordinary = input_tokens + cache_creation
        cost_cents = usage_cost_cents(self._config.pricing, ordinary, cache_read, output_tokens)
        usage = Usage(ordinary + cache_read, output_tokens, cost_cents)

        stop_reason = response.stop_reason
        if stop_reason != "end_turn":
            self.last_call = LlmCallMetadata(
                provider=provider_name,
                model_id=self._config.model_id,
                pricing_source=pricing_source,
                pricing_version=pricing_version,
                currency="USD",
                cost_status="estimated",
                reserved_cost_cents=reserved_cents,
                usage=usage,
                outcome="incomplete",
            )
            return {}, usage

        text_blocks = [
            getattr(block, "text", None)
            for block in response.content
            if getattr(block, "type", None) == "text"
        ]
        try:
            text_bytes = sum(
                len(text.encode("utf-8")) for text in text_blocks if isinstance(text, str)
            )
        except UnicodeEncodeError:
            text_bytes = MAX_TEXT_BYTES + 1
        text = next((value for value in text_blocks if isinstance(value, str)), None)
        parsed_json: JsonValue = {}
        if text_bytes > MAX_TEXT_BYTES or text is None:
            outcome = "invalid_response"
        else:
            parsed_json = parse_json_text(text)
            outcome = "invalid_response" if parsed_json == {} else "completed"
        self.last_call = LlmCallMetadata(
            provider=provider_name,
            model_id=self._config.model_id,
            pricing_source=pricing_source,
            pricing_version=pricing_version,
            currency="USD",
            cost_status="estimated",
            reserved_cost_cents=reserved_cents,
            usage=usage,
            outcome=outcome,
        )
        return parsed_json, usage


# ASSUMPTION: $10 / 1,000 searches = 1 cent each; confirm before paid use.
ANTHROPIC_SEARCH_FEE_CENTS = 1
ANTHROPIC_SEARCH_MAX_USES = 3


class AnthropicWebSearcher:
    """Optional Sonnet server-tool discovery; routine service search uses Brave."""

    brave_requests = 0

    def __init__(
        self,
        config: LlmModelConfig,
        api_key: str,
        *,
        client: AnthropicSdkClient | None = None,
        max_cost_cents: int | None = SEARCH_MAX_COST_CENTS,
    ) -> None:
        if config != SONNET_5_5:
            raise ValueError("Anthropic web search requires the Sonnet 5.5 preset")
        validate_ceiling(max_cost_cents)
        adapter = AnthropicLlmClient(config, api_key, client=client)
        self._config = config
        self._client = adapter._client
        self.max_cost_cents = max_cost_cents
        self.requests_made = 0
        self.usage = Usage()

    def search(self, request: SearchRequest) -> tuple[SearchHit, ...]:
        query = search_query(request)
        output, reserved = search_reservation(
            self._config,
            query,
            ANTHROPIC_SEARCH_FEE_CENTS * ANTHROPIC_SEARCH_MAX_USES,
            self.max_cost_cents,
        )
        params: MessageRequest = {
            "model": self._config.model_id,
            "system": SEARCH_INSTRUCTIONS,
            "messages": [{"role": "user", "content": query}],
            "max_tokens": output,
            "tools": [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}],
            "tool_choice": {"type": "tool", "name": "web_search"},
        }
        if self._config.effort is not None:
            params["output_config"] = {"effort": self._config.effort}
        self.requests_made += 1
        self.usage += reserved
        try:
            response = self._client.messages.create(**params)
            raw = response.usage
            cached = raw.cache_read_input_tokens or 0
            ordinary = raw.input_tokens + (raw.cache_creation_input_tokens or 0)
            searches = field(field(raw, "server_tool_use"), "web_search_requests")
            if type(searches) is not int or searches < 0:
                raise SearchError("Anthropic search response missing tool usage")
            usage = Usage(
                ordinary + cached,
                raw.output_tokens,
                usage_cost_cents(self._config.pricing, ordinary, cached, raw.output_tokens)
                + searches * ANTHROPIC_SEARCH_FEE_CENTS,
            )
        except Exception:
            raise SearchError("Anthropic search request failed or missing usage") from None
        self.usage = Usage(
            self.usage.input_tokens + usage.input_tokens,
            self.usage.output_tokens + usage.output_tokens,
            self.usage.cost_cents - reserved.cost_cents + usage.cost_cents,
        )
        if self.max_cost_cents is not None and usage.cost_cents > self.max_cost_cents:
            raise SearchError("Anthropic search exceeded reserved cost")
        if response.stop_reason in {"refusal", "max_tokens", "pause_turn"}:
            raise SearchError("Anthropic search incomplete response")
        urls: list[object] = []
        for block in response.content:
            if field(block, "type") == "web_search_tool_result":
                content = field(block, "content")
                if field(content, "type") == "web_search_tool_result_error":
                    raise SearchError("Anthropic search tool failed")
                urls.extend(
                    field(result, "url")
                    for result in items(content)
                    if field(result, "type") == "web_search_result"
                )
            elif field(block, "type") == "text":
                urls.extend(
                    field(citation, "url")
                    for citation in items(field(block, "citations"))
                    if field(citation, "type") == "web_search_result_location"
                )
        return url_hits(urls, request.limit)
