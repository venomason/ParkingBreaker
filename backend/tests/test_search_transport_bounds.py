"""Actual search read boundaries are bounded before JSON; no sockets/providers."""

from __future__ import annotations

import io
from collections.abc import Mapping
from types import ModuleType

import pytest

from parkingbreaker import brave_search, minimax_search
from parkingbreaker.brave_search import MAX_RESPONSE_BYTES, SearchResponse
from parkingbreaker.research.search import SearchError, SearchRequest


class Response(io.BytesIO):
    status = 200

    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.reads: list[int | None] = []

    def read(self, size: int | None = -1) -> bytes:
        self.reads.append(size)
        return super().read(size)


class Opener:
    def __init__(self, response: Response) -> None:
        self.response = response

    def open(self, *args: object, **kwargs: object) -> Response:
        return self.response


@pytest.mark.parametrize("module", [brave_search, minimax_search])
@pytest.mark.parametrize("extra", [0, 1])
def test_real_transports_cap_reads_before_returning(
    module: ModuleType, extra: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = Response(b" " * (MAX_RESPONSE_BYTES + extra))
    monkeypatch.setattr(module, "_OPENER", Opener(response))

    def call() -> SearchResponse:
        if module is brave_search:
            return brave_search.urllib_transport("https://provider.example.test/", {}, 1)
        return minimax_search.urllib_transport("https://provider.example.test/", {}, b"{}", 1)

    if extra:
        with pytest.raises(SearchError, match="response too large"):
            call()
    else:
        assert len(call().body) == MAX_RESPONSE_BYTES
    assert response.reads == [MAX_RESPONSE_BYTES + 1]


@pytest.mark.parametrize("provider", ["brave", "minimax"])
def test_injected_oversize_cannot_reach_json_parsing(provider: str) -> None:
    body = b"[" * (MAX_RESPONSE_BYTES + 1)

    def brave(url: str, headers: Mapping[str, str], timeout: float) -> SearchResponse:
        return SearchResponse(200, body)

    def minimax(
        url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> SearchResponse:
        return SearchResponse(200, b"[" * (MAX_RESPONSE_BYTES + 1))

    adapter = (
        brave_search.BraveSearcher({"BRAVE_SEARCH_API_KEY": "synthetic"}, transport=brave)
        if provider == "brave"
        else minimax_search.MiniMaxSearcher({"MINIMAX_API_KEY": "synthetic"}, transport=minimax)
    )
    with pytest.raises(SearchError, match="response too large"):
        adapter.search(SearchRequest("City parking appeal", (), frozenset(), False))
