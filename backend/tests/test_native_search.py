"""All provider discovery tests use injected clients/transports; no network."""

import json
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace as Obj
from typing import Unpack, cast

import pytest

from parkingbreaker import llm_anthropic as anthropic_adapter
from parkingbreaker import llm_openai as openai_adapter
from parkingbreaker.brave_search import SearchResponse
from parkingbreaker.llm import LlmBudgetExceeded, Usage
from parkingbreaker.llm_models import GPT_6_LUNA, SONNET_5_5, usage_cost_cents
from parkingbreaker.minimax_search import SEARCH_URL, MiniMaxSearcher
from parkingbreaker.native_search import SEARCH_INPUT_RESERVE, search_reservation
from parkingbreaker.research.search import SearchError, SearchHit, SearchRequest

SECRET = "PRIVATE-KEY-AND-RESPONSE-BODY"
A = "https://city.gov/contest"
B = "http://city.gov/procedure"
C = "https://state.gov/code"


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(*args: object, **kwargs: object) -> None:
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket.socket, "connect", refused)


def request(limit: int = 10) -> SearchRequest:
    return SearchRequest("parking appeal", ("city.gov", "state.gov"), frozenset(), False, limit)


@dataclass
class Transport:
    payload: object = field(default_factory=lambda: {"results": []})
    status: int = 200
    error: Exception | None = None
    calls: list[tuple[str, Mapping[str, str], bytes, float]] = field(default_factory=list)

    def __call__(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> SearchResponse:
        self.calls.append((url, headers, body, timeout))
        if self.error:
            raise self.error
        return SearchResponse(self.status, json.dumps(self.payload).encode())


@pytest.mark.parametrize("wrapper", ["results", "organic", "organic_results", "data", "web"])
def test_minimax_post_and_url_only_hits(wrapper: str) -> None:
    transport = Transport(
        {
            wrapper: [
                {"link": A, "snippet": SECRET},
                {"url": A},
                {"url": "file:///etc/passwd"},
                {"url": "https://[broken"},
                {"link": B},
                {"url": C},
            ]
        }
    )
    searcher = MiniMaxSearcher({"MINIMAX_API_KEY": SECRET}, transport=transport)
    assert searcher.search(request(2)) == (SearchHit(A), SearchHit(B))
    url, headers, body, timeout = transport.calls[0]
    assert url == SEARCH_URL
    assert headers == {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}
    assert json.loads(body) == {"q": "parking appeal (site:city.gov OR site:state.gov)"}
    assert timeout == 10
    assert searcher.requests_made == 1
    assert searcher.usage == Usage()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "text",
        {"results": [SECRET]},
        {"results": [{}]},
        {"results": {"url": A}},
        {"message": SECRET},
    ],
)
def test_minimax_bad_shapes_are_sanitised(payload: object) -> None:
    searcher = MiniMaxSearcher({"MINIMAX_API_KEY": SECRET}, transport=Transport(payload))
    with pytest.raises(SearchError, match="unexpected response shape") as error:
        searcher.search(request())
    assert SECRET not in str(error.value)
    assert error.value.__cause__ is None
    assert searcher.requests_made == 1


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        (2067, SECRET, "MiniMax search usage limit reached"),
        (2067, None, "MiniMax search usage limit reached"),
        (
            1001,
            f"The Token Plan USAGE LIMIT has been reached. {SECRET}",
            "MiniMax search usage limit reached",
        ),
        (1001, SECRET, "MiniMax search error 1001"),
        (1001, None, "MiniMax search error 1001"),
        (SECRET, SECRET, "MiniMax search unexpected response shape"),
    ],
)
@pytest.mark.parametrize("with_results", [False, True])
def test_minimax_base_resp_errors_are_sanitised(
    code: int | str, message: str | None, expected: str, with_results: bool
) -> None:
    payload: dict[str, object] = {"base_resp": {"status_code": code, "status_msg": message}}
    if with_results:
        payload["results"] = [{"link": A}]
    searcher = MiniMaxSearcher({"MINIMAX_API_KEY": SECRET}, transport=Transport(payload))
    with pytest.raises(SearchError) as error:
        searcher.search(request())
    assert str(error.value) == expected
    assert SECRET not in str(error.value)
    assert error.value.__cause__ is None
    assert searcher.requests_made == 1


def test_minimax_base_resp_zero_parses_results() -> None:
    transport = Transport(
        {
            "base_resp": {"status_code": 0, "status_msg": f"usage limit {SECRET}"},
            "results": [{"link": A}, {"url": B}],
        }
    )
    searcher = MiniMaxSearcher({"MINIMAX_API_KEY": SECRET}, transport=transport)
    assert searcher.search(request()) == (SearchHit(A), SearchHit(B))
    assert searcher.requests_made == 1


@pytest.mark.parametrize("status", [401, 429, 500])
def test_minimax_http_errors_are_sanitised(status: int) -> None:
    searcher = MiniMaxSearcher({"MINIMAX_API_KEY": SECRET}, transport=Transport(SECRET, status))
    with pytest.raises(SearchError, match=f"HTTP {status}") as error:
        searcher.search(request())
    assert SECRET not in str(error.value)


def test_minimax_transport_exception_and_validation() -> None:
    transport = Transport(error=RuntimeError(SECRET))
    searcher = MiniMaxSearcher({"MINIMAX_API_KEY": SECRET}, transport=transport)
    with pytest.raises(SearchError, match="request failed") as error:
        searcher.search(request())
    assert SECRET not in str(error.value)
    for limit in (0, -1, 21):
        with pytest.raises(ValueError, match="limit"):
            searcher.search(request(limit))
    assert searcher.requests_made == len(transport.calls) == 1
    with pytest.raises(ValueError, match="Missing MINIMAX_API_KEY"):
        MiniMaxSearcher({})


class Responses:
    def __init__(self) -> None:
        self.calls: list[openai_adapter.ResponseRequest] = []
        self.error: Exception | None = None
        self.response = Obj(
            status="completed",
            output_text=SECRET,
            usage=Obj(
                input_tokens=1000, input_tokens_details=Obj(cached_tokens=200), output_tokens=100
            ),
            output=[
                Obj(
                    type="web_search_call",
                    status="completed",
                    action=Obj(
                        type="search",
                        sources=[
                            Obj(type="url", url=A),
                            Obj(type="url", url=A),
                            Obj(type="url", url="javascript:bad"),
                        ],
                    ),
                    results=[Obj(url=B, snippet=SECRET)],
                ),
                Obj(
                    type="message",
                    status="completed",
                    content=[
                        Obj(
                            type="output_text",
                            text=SECRET,
                            annotations=[
                                Obj(type="url_citation", url=C, title=SECRET),
                                Obj(type="url_citation", url="https://[broken"),
                                Obj(type="other", url="https://ignored.gov"),
                            ],
                        )
                    ],
                ),
            ],
        )

    def create(self, **kwargs: Unpack[openai_adapter.ResponseRequest]) -> openai_adapter.Response:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return cast(openai_adapter.Response, self.response)


@dataclass
class OpenAIClient:
    responses: Responses = field(default_factory=Responses)


class Messages:
    def __init__(self) -> None:
        self.calls: list[anthropic_adapter.MessageRequest] = []
        self.error: Exception | None = None
        self.response = Obj(
            stop_reason="end_turn",
            usage=Obj(
                input_tokens=1000,
                cache_read_input_tokens=200,
                cache_creation_input_tokens=50,
                output_tokens=100,
                server_tool_use=Obj(web_search_requests=2),
            ),
            content=[
                Obj(
                    type="web_search_tool_result",
                    content=[
                        Obj(
                            type="web_search_result", url=A, title=SECRET, encrypted_content=SECRET
                        ),
                        Obj(type="web_search_result", url=A),
                        Obj(type="web_search_result", url=B),
                        Obj(type="web_search_result", url="file:///etc/passwd"),
                        Obj(type="other", url="https://ignored.gov"),
                    ],
                ),
                Obj(
                    type="text",
                    text=SECRET,
                    citations=[
                        Obj(type="web_search_result_location", url=C, cited_text=SECRET),
                        Obj(type="other", url="https://ignored.gov"),
                    ],
                ),
            ],
        )

    def count_tokens(
        self, **kwargs: Unpack[anthropic_adapter.CountRequest]
    ) -> anthropic_adapter.TokenCount:
        raise AssertionError("no count call required for reserved search context")

    def create(
        self, **kwargs: Unpack[anthropic_adapter.MessageRequest]
    ) -> anthropic_adapter.MessageResponse:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return cast(anthropic_adapter.MessageResponse, self.response)


@dataclass
class AnthropicClient:
    messages: Messages = field(default_factory=Messages)


def test_openai_native_search_urls_usage_and_request() -> None:
    client = OpenAIClient()
    searcher = openai_adapter.OpenAIWebSearcher(GPT_6_LUNA, SECRET, client=client)
    assert searcher.search(request()) == tuple(SearchHit(url) for url in (A, B, C))
    assert searcher.usage == Usage(
        1000, 100, usage_cost_cents(GPT_6_LUNA.pricing, 800, 200, 100) + 1
    )
    assert searcher.search(request(1)) == (SearchHit(A),)
    assert searcher.usage.cost_cents == 4
    assert searcher.requests_made == 2
    params = client.responses.calls[0]
    assert params["tools"] == [{"type": "web_search"}]
    assert params["include"] == ["web_search_call.action.sources"]
    assert params["max_tool_calls"] == 1
    assert params["tool_choice"] == "required"
    assert params["model"] == GPT_6_LUNA.model_id
    assert params["max_output_tokens"] == 2048
    assert "site:city.gov" in params["input"]
    assert SECRET not in repr(searcher.search(request()))


def test_anthropic_native_search_urls_usage_and_request() -> None:
    client = AnthropicClient()
    searcher = anthropic_adapter.AnthropicWebSearcher(SONNET_5_5, SECRET, client=client)
    assert searcher.search(request()) == tuple(SearchHit(url) for url in (A, B, C))
    expected = Usage(1250, 100, usage_cost_cents(SONNET_5_5.pricing, 1050, 200, 100) + 2)
    assert searcher.usage == expected
    assert searcher.search(request(1)) == (SearchHit(A),)
    assert searcher.usage == expected + expected
    params = client.messages.calls[0]
    assert params["tools"] == [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
    assert params["model"] == SONNET_5_5.model_id
    assert params["max_tokens"] == 2048
    assert "site:city.gov" in params["messages"][0]["content"]
    assert SECRET not in repr(searcher.search(request()))


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("ceiling", [0, 1, 2])
def test_native_search_ceiling_refuses_before_call(provider: str, ceiling: int) -> None:
    searcher: openai_adapter.OpenAIWebSearcher | anthropic_adapter.AnthropicWebSearcher
    calls: list[openai_adapter.ResponseRequest] | list[anthropic_adapter.MessageRequest]
    if provider == "openai":
        client = OpenAIClient()
        searcher = openai_adapter.OpenAIWebSearcher(
            GPT_6_LUNA, SECRET, client=client, max_cost_cents=ceiling
        )
        calls = client.responses.calls
    else:
        anth_client = AnthropicClient()
        searcher = anthropic_adapter.AnthropicWebSearcher(
            SONNET_5_5, SECRET, client=anth_client, max_cost_cents=ceiling
        )
        calls = anth_client.messages.calls
    with pytest.raises(LlmBudgetExceeded):
        searcher.search(request())
    assert not calls
    assert searcher.requests_made == 0
    assert searcher.usage == Usage()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("failure", ["exception", "missing_usage", "tool_error", "truncated"])
def test_native_search_errors_are_sanitised_and_cost_survives(provider: str, failure: str) -> None:
    api: Responses | Messages
    searcher: openai_adapter.OpenAIWebSearcher | anthropic_adapter.AnthropicWebSearcher
    if provider == "openai":
        client = OpenAIClient()
        searcher = openai_adapter.OpenAIWebSearcher(GPT_6_LUNA, SECRET, client=client)
        api = client.responses
        if failure == "tool_error":
            api.response.output[0].status = "failed"
        elif failure == "truncated":
            api.response.status = "incomplete"
    else:
        anth_client = AnthropicClient()
        searcher = anthropic_adapter.AnthropicWebSearcher(SONNET_5_5, SECRET, client=anth_client)
        api = anth_client.messages
        if failure == "tool_error":
            api.response.content[0].content = Obj(
                type="web_search_tool_result_error", error_code=SECRET
            )
        elif failure == "truncated":
            api.response.stop_reason = "max_tokens"
    if failure == "exception":
        api.error = RuntimeError(SECRET)
    elif failure == "missing_usage":
        api.response.usage = None
    with pytest.raises(SearchError) as error:
        searcher.search(request())
    assert SECRET not in str(error.value)
    assert error.value.__cause__ is None
    assert 0 < searcher.usage.cost_cents <= 50
    assert searcher.requests_made == 1


def test_reservation_bounds_output_and_rejects_oversized_query() -> None:
    output, reserved = search_reservation(SONNET_5_5, "query", 3, 45)
    assert 1024 <= output < 2048
    assert reserved.cost_cents <= 45
    with pytest.raises(LlmBudgetExceeded):
        search_reservation(SONNET_5_5, "x" * (SEARCH_INPUT_RESERVE * 2), 3, 50)
    with pytest.raises(ValueError, match="max_cost_cents"):
        search_reservation(SONNET_5_5, "query", 3, -1)
