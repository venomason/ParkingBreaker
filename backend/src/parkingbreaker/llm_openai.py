"""The sole OpenAI SDK boundary: one Responses call, without retries or repairs."""

from collections.abc import Mapping, Sequence
from typing import NotRequired, Protocol, TypedDict, Unpack, cast

import openai as openai

from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import (
    LlmProviderError,
    Usage,
    conservative_input_tokens,
    parse_json_text,
    schema_json,
)
from parkingbreaker.llm_models import (
    LlmModelConfig,
    request_output_tokens,
    usage_cost_cents,
    validate_ceiling,
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


class ResponseRequest(TypedDict):
    model: str
    instructions: str
    input: str
    max_output_tokens: int
    text: NotRequired[dict[str, JsonValue]]
    reasoning: NotRequired[dict[str, str]]
    tools: NotRequired[list[dict[str, JsonValue]]]
    include: NotRequired[list[str]]
    max_tool_calls: NotRequired[int]
    tool_choice: NotRequired[str]


class InputTokensDetails(Protocol):
    @property
    def cached_tokens(self) -> int: ...


class ResponseUsage(Protocol):
    @property
    def input_tokens(self) -> int: ...

    @property
    def input_tokens_details(self) -> InputTokensDetails: ...

    @property
    def output_tokens(self) -> int: ...


class Response(Protocol):
    @property
    def usage(self) -> ResponseUsage | None: ...

    @property
    def status(self) -> str | None: ...

    @property
    def output_text(self) -> str: ...

    @property
    def output(self) -> Sequence[object]: ...


class ResponsesApi(Protocol):
    def create(self, **kwargs: Unpack[ResponseRequest]) -> Response: ...


class OpenAiSdkClient(Protocol):
    @property
    def responses(self) -> ResponsesApi: ...


def _unusable_response(response: Response) -> bool:
    """Refusals are content blocks, not a top-level Responses status.

    Keep union-dependent SDK attribute access here: only message output items have
    content/status. Never accept a partial message or a mixed refusal/text result.
    """
    if response.status != "completed":
        return True
    for item in response.output:
        if getattr(item, "type", None) == "refusal":
            return True
        if getattr(item, "type", None) == "message":
            if getattr(item, "status", None) != "completed":
                return True
            content = getattr(item, "content", ())
            if any(getattr(block, "type", None) == "refusal" for block in content):
                return True
    return False


def _provider_error(exc: openai.APIError) -> LlmProviderError:
    retryable = isinstance(exc, openai.RateLimitError | openai.APIConnectionError) or (
        isinstance(exc, openai.APIStatusError) and exc.status_code >= 500
    )
    return LlmProviderError("OpenAI provider request failed", retryable=retryable)


class OpenAiLlmClient:
    def __init__(
        self, config: LlmModelConfig, api_key: str, *, client: OpenAiSdkClient | None = None
    ) -> None:
        if config.provider != "openai" or config.base_url is not None:
            raise ValueError("OpenAI client requires a first-party openai config")
        if not api_key.strip():
            raise ValueError("api_key must be non-empty")
        self._config = config
        self._client = (
            client
            if client is not None
            else cast(
                OpenAiSdkClient,
                openai.OpenAI(api_key=api_key, max_retries=0),
            )
        )

    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
    ) -> tuple[JsonValue, Usage]:
        request_output_tokens(self._config, 0, max_cost_cents)
        schema_text = schema_json(schema)
        if not self._config.structured_output:
            user += "\nReply with JSON only matching this schema: " + schema_text
        estimate = conservative_input_tokens(system, user, schema_text)
        request: ResponseRequest = {
            "model": self._config.model_id,
            "instructions": system,
            "input": user,
            "max_output_tokens": request_output_tokens(self._config, estimate, max_cost_cents),
        }
        if self._config.structured_output:
            request["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "result",
                    "schema": dict(schema),
                    "strict": False,
                },
            }
        if self._config.effort is not None:
            request["reasoning"] = {"effort": self._config.effort}
        try:
            response = self._client.responses.create(**request)
        except openai.APIError as exc:
            raise _provider_error(exc) from None

        raw = response.usage
        if raw is None:
            # A successful synchronous billable call must have authoritative usage.
            raise LlmProviderError("OpenAI response missing usage")
        cached = raw.input_tokens_details.cached_tokens
        usage = Usage(
            raw.input_tokens,
            raw.output_tokens,
            usage_cost_cents(
                self._config.pricing,
                raw.input_tokens - cached,
                cached,
                raw.output_tokens,
            ),
        )
        if _unusable_response(response):
            return {}, usage
        return parse_json_text(response.output_text), usage


# ASSUMPTION: $10 / 1,000 web searches = 1 cent per search; confirm before paid use.
OPENAI_SEARCH_FEE_CENTS = 1


class OpenAIWebSearcher:
    """URL-only Responses discovery with cumulative usage, including search fees."""

    brave_requests = 0

    def __init__(
        self,
        config: LlmModelConfig,
        api_key: str,
        *,
        client: OpenAiSdkClient | None = None,
        max_cost_cents: int | None = SEARCH_MAX_COST_CENTS,
    ) -> None:
        validate_ceiling(max_cost_cents)
        adapter = OpenAiLlmClient(config, api_key, client=client)
        self._config = config
        self._client = adapter._client
        self.max_cost_cents = max_cost_cents
        self.requests_made = 0
        self.usage = Usage()

    def search(self, request: SearchRequest) -> tuple[SearchHit, ...]:
        query = search_query(request)
        output, reserved = search_reservation(
            self._config, query, OPENAI_SEARCH_FEE_CENTS, self.max_cost_cents
        )
        params: ResponseRequest = {
            "model": self._config.model_id,
            "instructions": SEARCH_INSTRUCTIONS,
            "input": query,
            "max_output_tokens": output,
            "tools": [{"type": "web_search"}],
            "include": ["web_search_call.action.sources"],
            "max_tool_calls": 1,
            "tool_choice": "required",
        }
        if self._config.effort is not None:
            params["reasoning"] = {"effort": self._config.effort}
        self.requests_made += 1
        self.usage += reserved
        try:
            response = self._client.responses.create(**params)
            raw = response.usage
            if raw is None:
                raise SearchError("OpenAI search response missing usage")
            cached = raw.input_tokens_details.cached_tokens
            calls = sum(field(item, "type") == "web_search_call" for item in response.output)
            usage = Usage(
                raw.input_tokens,
                raw.output_tokens,
                usage_cost_cents(
                    self._config.pricing, raw.input_tokens - cached, cached, raw.output_tokens
                )
                + OPENAI_SEARCH_FEE_CENTS * calls,
            )
        except Exception:
            raise SearchError("OpenAI search request failed or missing usage") from None
        self.usage = Usage(
            self.usage.input_tokens + usage.input_tokens,
            self.usage.output_tokens + usage.output_tokens,
            self.usage.cost_cents - reserved.cost_cents + usage.cost_cents,
        )
        if self.max_cost_cents is not None and usage.cost_cents > self.max_cost_cents:
            raise SearchError("OpenAI search exceeded reserved cost")
        if _unusable_response(response):
            raise SearchError("OpenAI search incomplete response")
        urls: list[object] = []
        for item in response.output:
            if field(item, "type") == "web_search_call":
                if field(item, "status") != "completed":
                    raise SearchError("OpenAI search tool failed")
                action = field(item, "action")
                urls.extend(field(source, "url") for source in items(field(action, "sources")))
                urls.extend(field(source, "url") for source in items(field(item, "results")))
                if field(action, "type") in {"open_page", "find_in_page"}:
                    urls.append(field(action, "url"))
            elif field(item, "type") == "message":
                for block in items(field(item, "content")):
                    urls.extend(
                        field(annotation, "url")
                        for annotation in items(field(block, "annotations"))
                        if field(annotation, "type") == "url_citation"
                    )
        return url_hits(urls, request.limit)
