"""The sole Brave HTTP boundary: URL-only discovery, one attempt per search (bench T2).

Search results are untrusted leads. Callers still authenticate and allowlist URLs;
provider titles, descriptions and snippets never become research evidence.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlencode, urlsplit

from parkingbreaker.llm import Usage
from parkingbreaker.provider_credentials import valid_header_credential
from parkingbreaker.research.search import SearchError, SearchHit, SearchRequest

SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
TIMEOUT_S = 10.0
MAX_COUNT = 20
MAX_RESPONSE_BYTES = 1_048_576
BRAVE_PRICING_SOURCE = "https://api-dashboard.search.brave.com/documentation/pricing"
BRAVE_PRICING_VERSION = "2026-10-19"
BRAVE_REQUEST_RESERVE_CENTS = 1


@dataclass(frozen=True)
class SearchResponse:
    status: int
    body: bytes


@dataclass(frozen=True)
class SearchCallMetadata:
    provider: Literal["brave"]
    pricing_source: str
    pricing_version: str
    currency: Literal["USD"]
    cost_status: Literal["estimated", "pending"]
    reserved_cost_cents: int
    request_count: int
    request_sha256: str
    response_sha256: str | None
    result_sha256: str | None
    usage: Usage
    outcome: Literal["pending", "completed", "provider_error", "invalid_response"]


class Transport(Protocol):
    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> SearchResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def urllib_transport(url: str, headers: Mapping[str, str], timeout: float) -> SearchResponse:
    """One GET, no redirects or retries; HTTP errors expose only their status."""
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise SearchError("Brave search response too large")
            return SearchResponse(int(response.status), body)
    except urllib.error.HTTPError as exc:
        exc.close()
        return SearchResponse(int(exc.code), b"")
    except (urllib.error.URLError, OSError, ValueError):
        raise SearchError("Brave search request failed") from None


def _hits(body: bytes, limit: int) -> tuple[SearchHit, ...]:
    if len(body) > MAX_RESPONSE_BYTES:
        raise SearchError("Brave search response too large")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise SearchError("Brave search returned invalid JSON") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("web"), dict):
        raise SearchError("Brave search returned an invalid response shape")
    results = payload["web"].get("results")
    if not isinstance(results, list):
        raise SearchError("Brave search returned an invalid response shape")

    hits: list[SearchHit] = []
    seen: set[str] = set()
    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get("url"), str):
            raise SearchError("Brave search returned an invalid response shape")
        url = result["url"]
        try:
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                continue
        except ValueError:
            continue
        if url not in seen:
            seen.add(url)
            hits.append(SearchHit(url))
    return tuple(hits[:limit])


class BraveSearcher:
    """`research.search.Searcher` with injected credentials and HTTP transport.

    `requests_made` counts transport attempts, including failed attempts; invalid
    local requests do not count. Per-call provenance is hashed and never retains the
    query, response body, snippets, or credential. The one-cent amount is a
    conservative upward-rounded list-price estimate, not a reconciled invoice.
    """

    def __init__(
        self,
        environ: Mapping[str, str],
        *,
        transport: Transport = urllib_transport,
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        key = environ.get("BRAVE_SEARCH_API_KEY", "")
        if not key.strip():
            raise ValueError("Missing BRAVE_SEARCH_API_KEY")
        if not valid_header_credential(key):
            raise ValueError("Invalid BRAVE_SEARCH_API_KEY")
        self._key = key
        self._transport = transport
        self._timeout_s = timeout_s
        self.requests_made = 0
        self.usage = Usage()
        self.last_call: SearchCallMetadata | None = None

    def _metadata(
        self,
        *,
        request_sha256: str,
        response_sha256: str | None,
        result_sha256: str | None,
        cost_status: Literal["estimated", "pending"],
        outcome: Literal["pending", "completed", "provider_error", "invalid_response"],
    ) -> None:
        self.last_call = SearchCallMetadata(
            provider="brave",
            pricing_source=BRAVE_PRICING_SOURCE,
            pricing_version=BRAVE_PRICING_VERSION,
            currency="USD",
            cost_status=cost_status,
            reserved_cost_cents=BRAVE_REQUEST_RESERVE_CENTS,
            request_count=1,
            request_sha256=request_sha256,
            response_sha256=response_sha256,
            result_sha256=result_sha256,
            usage=Usage(0, 0, BRAVE_REQUEST_RESERVE_CENTS),
            outcome=outcome,
        )

    def search(self, request: SearchRequest) -> tuple[SearchHit, ...]:
        self.last_call = None
        if not 1 <= request.limit <= MAX_COUNT:
            raise ValueError(f"search limit must be in 1..{MAX_COUNT}")
        query = request.query
        if request.official_domains:
            # CONFIRM AT BUILD: grouped Brave site: filters; URL allowlisting remains mandatory.
            query += " (" + " OR ".join(f"site:{d}" for d in request.official_domains) + ")"
        url = SEARCH_URL + "?" + urlencode({"q": query, "count": request.limit})
        headers = {"Accept": "application/json", "X-Subscription-Token": self._key}
        self.requests_made += 1
        request_sha256 = hashlib.sha256(url.encode("utf-8")).hexdigest()
        reserved = Usage(0, 0, BRAVE_REQUEST_RESERVE_CENTS)
        self.usage += reserved
        self._metadata(
            request_sha256=request_sha256,
            response_sha256=None,
            result_sha256=None,
            cost_status="pending",
            outcome="pending",
        )
        try:
            response = self._transport(url, headers, self._timeout_s)
        except urllib.error.HTTPError as exc:
            self._metadata(
                request_sha256=request_sha256,
                response_sha256=None,
                result_sha256=None,
                cost_status="pending",
                outcome="provider_error",
            )
            raise SearchError(f"Brave search HTTP {exc.code}") from None
        except SearchError:
            self._metadata(
                request_sha256=request_sha256,
                response_sha256=None,
                result_sha256=None,
                cost_status="pending",
                outcome="invalid_response",
            )
            raise
        except Exception:
            self._metadata(
                request_sha256=request_sha256,
                response_sha256=None,
                result_sha256=None,
                cost_status="pending",
                outcome="provider_error",
            )
            raise SearchError("Brave search request failed") from None
        if type(response.status) is not int or not isinstance(response.body, bytes):
            self._metadata(
                request_sha256=request_sha256,
                response_sha256=None,
                result_sha256=None,
                cost_status="pending",
                outcome="invalid_response",
            )
            raise SearchError("Brave search returned an invalid response")
        response_sha256 = hashlib.sha256(response.body).hexdigest()
        if response.status != 200:
            self._metadata(
                request_sha256=request_sha256,
                response_sha256=response_sha256,
                result_sha256=None,
                cost_status="pending",
                outcome="provider_error",
            )
            raise SearchError(f"Brave search HTTP {response.status}")
        try:
            hits = _hits(response.body, request.limit)
        except SearchError:
            self._metadata(
                request_sha256=request_sha256,
                response_sha256=response_sha256,
                result_sha256=None,
                cost_status="pending",
                outcome="invalid_response",
            )
            raise
        result_payload = json.dumps(
            [hit.url for hit in hits], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self._metadata(
            request_sha256=request_sha256,
            response_sha256=response_sha256,
            result_sha256=hashlib.sha256(result_payload).hexdigest(),
            cost_status="estimated",
            outcome="completed",
        )
        return hits
