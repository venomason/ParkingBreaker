from __future__ import annotations

import hashlib
import json
import traceback
import urllib.error
from collections.abc import Callable, Mapping
from dataclasses import FrozenInstanceError
from email.message import EmailMessage
from urllib.parse import parse_qs, urlsplit

import pytest

from parkingbreaker.brave_search import (
    BRAVE_PRICING_SOURCE,
    BRAVE_PRICING_VERSION,
    MAX_RESPONSE_BYTES,
    BraveSearcher,
    SearchCallMetadata,
    SearchResponse,
)
from parkingbreaker.llm import Usage
from parkingbreaker.research.search import (
    MAX_SEARCH_HITS,
    Searcher,
    SearchError,
    SearchHit,
    SearchRequest,
)

KEY = "test-subscription-secret"
UNTRUSTED = "untrusted body and snippet text"


class FakeTransport:
    def __init__(
        self,
        body: bytes = b'{"web":{"results":[]}}',
        *,
        status: int = 200,
        error: Exception | None = None,
        observer: Callable[[], None] | None = None,
    ) -> None:
        self.response = SearchResponse(status, body)
        self.error = error
        self.observer = observer
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> SearchResponse:
        self.calls.append((url, dict(headers), timeout))
        if self.observer is not None:
            self.observer()
        if self.error is not None:
            raise self.error
        return self.response


def request(domains: tuple[str, ...] = (), *, limit: int = MAX_SEARCH_HITS) -> SearchRequest:
    return SearchRequest("San José parking & mail? #appeal", domains, frozenset(), False, limit)


def searcher(transport: FakeTransport) -> BraveSearcher:
    return BraveSearcher({"BRAVE_SEARCH_API_KEY": KEY}, transport=transport)


@pytest.mark.parametrize("domains", [(), ("a.gov",), ("a.gov", "b.gov")])
@pytest.mark.parametrize("limit", [1, MAX_SEARCH_HITS, 20])
def test_encoded_query_filters_count_and_header_only_key(
    domains: tuple[str, ...], limit: int
) -> None:
    transport = FakeTransport()
    adapter: Searcher = searcher(transport)
    assert adapter.search(request(domains, limit=limit)) == ()
    [(url, headers, timeout)] = transport.calls
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == (
        "https",
        "api.search.brave.com",
        "/res/v1/web/search",
    )
    query = request().query
    if domains:
        query += " (" + " OR ".join(f"site:{domain}" for domain in domains) + ")"
    assert parse_qs(parts.query) == {"q": [query], "count": [str(limit)]}
    assert "%C3%A9" in url
    assert "%26" in url
    assert "%3F" in url
    assert "%23" in url
    if domains:
        assert "%28site%3A" in url
        assert "%29" in url
    assert KEY not in url
    assert parts.fragment == ""
    assert headers == {"Accept": "application/json", "X-Subscription-Token": KEY}
    assert timeout == 10.0


def test_returns_only_urls_deduplicated_in_order_and_drops_unsafe_schemes() -> None:
    urls = [
        "https://a.gov/appeal",
        "javascript:alert(1)",
        "https://a.gov/appeal",
        "http://b.gov/mail",
        "file:///etc/passwd",
        "ftp://a.gov/appeal",
        "/relative",
        "https:///missing-host",
        "https://[broken",
        "data:text/plain,untrusted",
        "https://c.gov/contest",
    ]
    body = json.dumps(
        {
            "web": {
                "results": [
                    {
                        "url": url,
                        "title": UNTRUSTED,
                        "description": UNTRUSTED,
                        "snippets": [UNTRUSTED],
                    }
                    for url in urls
                ]
            }
        }
    ).encode()
    hits = searcher(FakeTransport(body)).search(request())
    assert hits == (
        SearchHit("https://a.gov/appeal"),
        SearchHit("http://b.gov/mail"),
        SearchHit("https://c.gov/contest"),
    )
    assert UNTRUSTED not in repr(hits)
    assert all(vars(hit).keys() == {"url"} for hit in hits)


def test_metadata_hashes_request_response_and_results_without_retaining_content() -> None:
    body = json.dumps(
        {
            "web": {
                "results": [
                    {"url": "https://a.gov/appeal", "title": UNTRUSTED, "snippet": UNTRUSTED}
                ]
            }
        }
    ).encode()
    holder: dict[str, BraveSearcher] = {}
    observed: list[tuple[SearchCallMetadata | None, Usage]] = []

    def observe() -> None:
        adapter = holder["adapter"]
        observed.append((adapter.last_call, adapter.usage))

    transport = FakeTransport(body, observer=observe)
    adapter = searcher(transport)
    holder["adapter"] = adapter
    hits = adapter.search(request())

    [(pending, usage_at_attempt)] = observed
    assert pending is not None
    assert pending.cost_status == "pending"
    assert pending.outcome == "pending"
    assert pending.usage == Usage(0, 0, 1)
    assert usage_at_attempt == Usage(0, 0, 1)

    metadata = adapter.last_call
    assert metadata is not None
    assert metadata.provider == "brave"
    assert metadata.pricing_source == BRAVE_PRICING_SOURCE
    assert metadata.pricing_version == BRAVE_PRICING_VERSION
    assert metadata.currency == "USD"
    assert metadata.cost_status == "estimated"
    assert metadata.reserved_cost_cents == metadata.request_count == 1
    assert metadata.request_sha256 == hashlib.sha256(transport.calls[0][0].encode()).hexdigest()
    assert metadata.response_sha256 == hashlib.sha256(body).hexdigest()
    assert metadata.result_sha256 == hashlib.sha256(b'["https://a.gov/appeal"]').hexdigest()
    assert metadata.usage == adapter.usage == Usage(0, 0, 1)
    assert metadata.outcome == "completed"
    assert hits == (SearchHit("https://a.gov/appeal"),)
    assert KEY not in repr(metadata)
    assert UNTRUSTED not in repr(metadata)
    assert "San José" not in repr(metadata)
    with pytest.raises(FrozenInstanceError):
        metadata.cost_status = "pending"  # type: ignore[misc]


def test_returned_hits_cannot_exceed_requested_limit() -> None:
    body = json.dumps({"web": {"results": [{"url": f"https://a.gov/{i}"} for i in range(5)]}})
    hits = searcher(FakeTransport(body.encode())).search(request(limit=2))
    assert hits == (SearchHit("https://a.gov/0"), SearchHit("https://a.gov/1"))


@pytest.mark.parametrize("status", [301, 302, 400, 401, 403, 404, 429, 500, 502, 503])
def test_http_failures_are_sanitised_and_never_retried(status: int) -> None:
    body = f"{KEY} {UNTRUSTED}".encode()
    transport = FakeTransport(body, status=status)
    adapter = searcher(transport)
    with pytest.raises(SearchError) as caught:
        adapter.search(request())
    assert str(caught.value) == f"Brave search HTTP {status}"
    assert KEY not in "".join(traceback.format_exception(caught.value))
    assert UNTRUSTED not in "".join(traceback.format_exception(caught.value))
    assert adapter.requests_made == len(transport.calls) == 1
    assert adapter.usage == Usage(0, 0, 1)
    assert adapter.last_call is not None
    assert adapter.last_call.cost_status == "pending"
    assert adapter.last_call.outcome == "provider_error"
    assert adapter.last_call.response_sha256 == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize("body", [f"{KEY} {UNTRUSTED}".encode(), b'{"web":', b"\xff"])
def test_invalid_json_is_sanitised(body: bytes) -> None:
    transport = FakeTransport(body)
    adapter = searcher(transport)
    with pytest.raises(SearchError, match="invalid JSON") as caught:
        adapter.search(request())
    formatted = "".join(traceback.format_exception(caught.value))
    assert KEY not in formatted
    assert UNTRUSTED not in formatted
    assert adapter.requests_made == len(transport.calls) == 1
    assert adapter.usage == Usage(0, 0, 1)
    assert adapter.last_call is not None
    assert adapter.last_call.cost_status == "pending"
    assert adapter.last_call.outcome == "invalid_response"
    assert adapter.last_call.response_sha256 == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"web": []},
        {"web": {}},
        {"web": {"results": None}},
        {"web": {"results": {}}},
        {"web": {"results": [None]}},
        {"web": {"results": [{}]}},
        {"web": {"results": [{"url": 42}]}},
        {"web": {"results": [{"url": None}]}},
    ],
)
def test_wrong_shape_is_search_error(payload: object) -> None:
    body = json.dumps(payload).encode()
    adapter = searcher(FakeTransport(body))
    with pytest.raises(SearchError, match="invalid response shape"):
        adapter.search(request())
    assert adapter.last_call is not None
    assert adapter.last_call.outcome == "invalid_response"
    assert adapter.last_call.cost_status == "pending"
    assert adapter.last_call.response_sha256 == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize(
    "environ", [{}, {"BRAVE_SEARCH_API_KEY": ""}, {"BRAVE_SEARCH_API_KEY": " \t\n"}]
)
def test_missing_or_blank_key_fails_at_construction(environ: dict[str, str]) -> None:
    transport = FakeTransport()
    with pytest.raises(ValueError, match="Missing BRAVE_SEARCH_API_KEY"):
        BraveSearcher(environ, transport=transport)
    assert transport.calls == []


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(f"{KEY} {UNTRUSTED}"),
        OSError(f"{KEY} {UNTRUSTED}"),
        urllib.error.URLError(f"{KEY} {UNTRUSTED}"),
        ValueError(f"{KEY} {UNTRUSTED}"),
        RuntimeError(f"{KEY} {UNTRUSTED}"),
    ],
)
def test_transport_errors_hide_exception_text_and_count_attempts(error: Exception) -> None:
    transport = FakeTransport(error=error)
    adapter = searcher(transport)
    with pytest.raises(SearchError, match="request failed") as caught:
        adapter.search(request())
    formatted = "".join(traceback.format_exception(caught.value))
    assert KEY not in formatted
    assert UNTRUSTED not in formatted
    assert adapter.requests_made == len(transport.calls) == 1
    assert adapter.usage == Usage(0, 0, 1)
    assert adapter.last_call is not None
    assert adapter.last_call.cost_status == "pending"
    assert adapter.last_call.outcome == "provider_error"
    assert adapter.last_call.response_sha256 is None


def test_transport_http_exception_is_sanitised() -> None:
    transport = FakeTransport(
        error=urllib.error.HTTPError(
            "https://example.test/" + KEY, 429, UNTRUSTED, EmailMessage(), None
        )
    )
    adapter = searcher(transport)
    with pytest.raises(SearchError, match="HTTP 429") as caught:
        adapter.search(request())
    formatted = "".join(traceback.format_exception(caught.value))
    assert KEY not in formatted
    assert UNTRUSTED not in formatted
    assert len(transport.calls) == 1
    assert adapter.usage == Usage(0, 0, 1)
    assert adapter.last_call is not None
    assert adapter.last_call.cost_status == "pending"
    assert adapter.last_call.outcome == "provider_error"


def test_counter_increments_for_each_search_including_failed_attempts() -> None:
    transport = FakeTransport()
    adapter = searcher(transport)
    assert adapter.requests_made == 0
    adapter.search(request())
    adapter.search(request())
    transport.error = TimeoutError()
    with pytest.raises(SearchError):
        adapter.search(request())
    assert adapter.requests_made == len(transport.calls) == 3
    assert adapter.usage == Usage(0, 0, 3)


@pytest.mark.parametrize("limit", [0, -1, 21])
def test_invalid_limit_does_not_make_request(limit: int) -> None:
    transport = FakeTransport()
    adapter = searcher(transport)
    with pytest.raises(ValueError, match="limit"):
        adapter.search(request(limit=limit))
    assert adapter.requests_made == 0
    assert transport.calls == []
    assert adapter.usage == Usage()
    assert adapter.last_call is None


def test_oversized_response_is_hashed_and_remains_pending() -> None:
    body = b"x" * (MAX_RESPONSE_BYTES + 1)
    adapter = searcher(FakeTransport(body))
    with pytest.raises(SearchError, match="too large"):
        adapter.search(request())
    assert adapter.usage == Usage(0, 0, 1)
    assert adapter.last_call is not None
    assert adapter.last_call.cost_status == "pending"
    assert adapter.last_call.outcome == "invalid_response"
    assert adapter.last_call.response_sha256 == hashlib.sha256(body).hexdigest()
