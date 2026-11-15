"""MiniMax subscription URL-only discovery; search results are untrusted leads.

CONFIRM AT BUILD: coding_plan/search endpoint and response schema are documented
only by third parties. ASSUMPTION: subscription search has no incremental fee.
"""

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Protocol

from parkingbreaker.brave_search import MAX_RESPONSE_BYTES, SearchResponse, _NoRedirect
from parkingbreaker.llm import Usage
from parkingbreaker.native_search import items, search_query, url_hits
from parkingbreaker.research.search import SearchError, SearchHit, SearchRequest

SEARCH_URL = "https://api.minimax.io/v1/coding_plan/search"
TIMEOUT_S = 10.0
_OPENER = urllib.request.build_opener(_NoRedirect())


class Transport(Protocol):
    def __call__(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> SearchResponse: ...


def urllib_transport(
    url: str, headers: Mapping[str, str], body: bytes, timeout: float
) -> SearchResponse:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise SearchError("MiniMax search response too large")
            return SearchResponse(int(response.status), data)
    except urllib.error.HTTPError as exc:
        exc.close()
        return SearchResponse(int(exc.code), b"")
    except (urllib.error.URLError, OSError, ValueError):
        raise SearchError("MiniMax search request failed") from None


def _result_urls(payload: object) -> list[str] | None:
    # Only result lists of URL-bearing objects qualify, never strings/snippets.
    if isinstance(payload, list):
        urls: list[str] = []
        for result in items(payload):
            if not isinstance(result, dict):
                return None
            url = result.get("link")
            if not isinstance(url, str):
                url = result.get("url")
            if not isinstance(url, str):
                return None
            urls.append(url)
        return urls
    if isinstance(payload, dict):
        for name in ("results", "organic", "organic_results", "data", "web"):
            if name in payload:
                nested_urls = _result_urls(payload[name])
                if nested_urls is not None:
                    return nested_urls
    return None


class MiniMaxSearcher:
    brave_requests = 0

    def __init__(
        self,
        environ: Mapping[str, str],
        *,
        transport: Transport = urllib_transport,
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        key = environ.get("MINIMAX_API_KEY", "")
        if not key.strip():
            raise ValueError("Missing MINIMAX_API_KEY")
        self._key = key
        self._transport = transport
        self._timeout_s = timeout_s
        self.requests_made = 0
        self.usage = Usage()

    def search(self, request: SearchRequest) -> tuple[SearchHit, ...]:
        query = search_query(request)
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        self.requests_made += 1
        try:
            response = self._transport(
                SEARCH_URL, headers, json.dumps({"q": query}).encode(), self._timeout_s
            )
        except urllib.error.HTTPError as exc:
            raise SearchError(f"MiniMax search HTTP {exc.code}") from None
        except Exception:
            raise SearchError("MiniMax search request failed") from None
        if response.status != 200:
            raise SearchError(f"MiniMax search HTTP {response.status}")
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise SearchError("MiniMax search response too large")
        try:
            payload = json.loads(response.body)
        except (ValueError, UnicodeError, RecursionError):
            raise SearchError("MiniMax search unexpected response shape") from None
        if isinstance(payload, dict):
            base_resp = payload.get("base_resp")
            if isinstance(base_resp, dict) and "status_code" in base_resp:
                code = base_resp["status_code"]
                if not isinstance(code, int) or isinstance(code, bool):
                    raise SearchError("MiniMax search unexpected response shape")
                if code != 0:
                    message = base_resp.get("status_msg")
                    if code == 2067 or (
                        isinstance(message, str) and "usage limit" in message.lower()
                    ):
                        raise SearchError("MiniMax search usage limit reached")
                    raise SearchError(f"MiniMax search error {code}")
        try:
            urls = _result_urls(payload)
        except RecursionError:
            raise SearchError("MiniMax search unexpected response shape") from None
        if urls is None:
            raise SearchError("MiniMax search unexpected response shape")
        return url_hits(urls, request.limit)
