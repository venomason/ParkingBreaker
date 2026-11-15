"""Official-page discovery (architecture v6 sections 3.1, 5.3 and 13.1).

A search provider returns candidate URLs only. We ignore titles/snippets and assign
source authority ourselves after applying the same URL allowlist used by fetching.
This module's query and candidate selection functions are pure; Searcher is the I/O seam.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urldefrag

from parkingbreaker.agencies import SourceScope
from parkingbreaker.research.questions import CRITICAL_KEYS, FactKey, Ticket
from parkingbreaker.research.sources import (
    CODE_PUBLISHER_DOMAINS,
    AuthorityClass,
    is_allowed,
    is_code_publisher,
)

# One shared publisher allowlist, never city-specific seed URLs or legal content.
# California's official legislative site links to this current publisher:
# https://www.leginfo.ca.gov/ . Add other states only from their official publications.
SHARED_CODE_DOMAINS = ("leginfo.legislature.ca.gov",)
SHARED_CODE_SCOPES = tuple(SourceScope(host) for host in SHARED_CODE_DOMAINS)
MAX_SEARCH_HITS = 10


@dataclass(frozen=True)
class SearchRequest:
    """Empty official_domains means unrestricted search. Hits are LEADS only;
    callers must authenticate ownership and allowlist URLs before fetching evidence.
    """

    query: str
    official_domains: tuple[str, ...]
    keys: frozenset[FactKey]
    broadened: bool
    limit: int = MAX_SEARCH_HITS


@dataclass(frozen=True)
class SearchHit:
    url: str


class SearchError(Exception):
    """The provider could not complete a search. Recovery never loops on this error."""


class Searcher(Protocol):
    def search(self, request: SearchRequest) -> Sequence[SearchHit]: ...


@dataclass(frozen=True)
class DiscoveredPage:
    url: str
    authority_class: AuthorityClass
    covers: frozenset[FactKey]


def search_request(
    ticket: Ticket,
    official_domains: Sequence[str],
    *,
    keys: frozenset[FactKey] = CRITICAL_KEYS,
    broadened: bool = False,
) -> SearchRequest:
    domains = tuple(
        dict.fromkeys((*official_domains, *SHARED_CODE_DOMAINS, *CODE_PUBLISHER_DOMAINS))
    )
    # Only the resolved authority/place identifier and fixed clerical terms leave
    # this boundary. A free-form violation field may contain customer identifiers.
    # Filtering returned URLs in code remains mandatory even if the provider filters.
    query = f"{ticket.city_id} parking citation contest by mail"
    if broadened:
        query += " official procedure address deadline requirements " + " ".join(
            key.value for key in FactKey if key in keys
        )
    return SearchRequest(query, domains, keys, broadened)


def select_pages(
    request: SearchRequest, hits: Sequence[SearchHit], scopes: Sequence[SourceScope]
) -> tuple[DiscoveredPage, ...]:
    if request.limit < 1 or request.limit > MAX_SEARCH_HITS:
        raise ValueError(f"search limit must be in 1..{MAX_SEARCH_HITS}")
    selected: list[DiscoveredPage] = []
    seen: set[str] = set()
    # Bound inspection as well as output; a hostile result list cannot expand this run.
    for hit in hits[: request.limit]:
        if not is_allowed(hit.url, scopes):
            continue
        url, _ = urldefrag(hit.url)
        if url in seen:
            continue
        seen.add(url)
        authority = (
            AuthorityClass.STATUTE
            if is_allowed(url, SHARED_CODE_SCOPES) or is_code_publisher(url)
            else AuthorityClass.OFFICIAL_PROCEDURE
        )
        selected.append(DiscoveredPage(url, authority, request.keys))
    return tuple(selected)
