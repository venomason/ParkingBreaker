"""In-memory fakes for the research seams. For tests only; no network."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import LlmBudgetExceeded, Usage
from parkingbreaker.research.extractor import ExtractionResult
from parkingbreaker.research.findings import Finding
from parkingbreaker.research.questions import Question
from parkingbreaker.research.search import SearchHit, SearchRequest
from parkingbreaker.research.sources import FetchError, FetchResponse, SourceRecord


def html_response(url: str, html: str, status: int = 200) -> FetchResponse:
    return FetchResponse(
        url=url,
        status=status,
        headers={"content-type": "text/html; charset=utf-8"},
        body=html.encode("utf-8"),
    )


def redirect_response(url: str, location: str, status: int = 302) -> FetchResponse:
    return FetchResponse(url=url, status=status, headers={"location": location}, body=b"")


def _check_budget(cost_cents: int, max_cost_cents: int | None) -> None:
    if max_cost_cents is None:
        return
    if max_cost_cents < 0:
        raise ValueError("max_cost_cents must be nonnegative")
    if max_cost_cents == 0 or cost_cents > max_cost_cents:
        raise LlmBudgetExceeded("canned usage cannot fit the spending ceiling")


@dataclass
class FakeFetcher:
    """Serves canned responses by exact URL. Unknown URLs raise FetchError."""

    responses: Mapping[str, FetchResponse]
    calls: list[str] = field(default_factory=list)

    def get(self, url: str) -> FetchResponse:
        self.calls.append(url)
        try:
            return self.responses[url]
        except KeyError:
            raise FetchError(f"no canned response for {url}") from None


@dataclass
class FakeExtractor:
    """Returns findings from a function of the fetched sources (so tests can cite shas)."""

    produce: Callable[[Sequence[SourceRecord]], Sequence[Finding]]
    usage: Usage = field(default_factory=Usage)
    calls: int = 0

    def extract(
        self,
        questions: Sequence[Question],
        sources: Sequence[SourceRecord],
        *,
        max_cost_cents: int | None = None,
    ) -> ExtractionResult:
        try:
            _check_budget(self.usage.cost_cents, max_cost_cents)
        except LlmBudgetExceeded:
            return ExtractionResult(findings=(), usage=Usage(), budget_exhausted=True)
        self.calls += 1
        return ExtractionResult(findings=tuple(self.produce(sources)), usage=self.usage)


@dataclass
class FakeLlmClient:
    """Canned usage is the worst-case cost; calls records only accepted requests."""

    reply: JsonValue
    usage: Usage = field(default_factory=Usage)
    calls: list[tuple[str, str]] = field(default_factory=list)
    ceilings: list[int | None] = field(default_factory=list)

    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
    ) -> tuple[JsonValue, Usage]:
        self.ceilings.append(max_cost_cents)
        _check_budget(self.usage.cost_cents, max_cost_cents)
        self.calls.append((system, user))
        return self.reply, self.usage


def fixed_clock(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


@dataclass
class FakeSearcher:
    """Returns URLs for a request, recording both initial and broadened searches."""

    produce: Callable[[SearchRequest], Sequence[SearchHit]]
    calls: list[SearchRequest] = field(default_factory=list)

    def search(self, request: SearchRequest) -> Sequence[SearchHit]:
        self.calls.append(request)
        return self.produce(request)
