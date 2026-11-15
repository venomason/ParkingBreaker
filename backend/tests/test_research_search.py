from __future__ import annotations

from dataclasses import replace

import pytest

from parkingbreaker.agencies import SourceScope
from parkingbreaker.research.questions import CRITICAL_KEYS, FactKey
from parkingbreaker.research.search import (
    MAX_SEARCH_HITS,
    SHARED_CODE_DOMAINS,
    SHARED_CODE_SCOPES,
    SearchHit,
    SearchRequest,
    search_request,
    select_pages,
)
from parkingbreaker.research.sources import CODE_PUBLISHER_DOMAINS, AuthorityClass
from test_research_run import TICKET

SCOPES = (SourceScope("sfmta.com"), *SHARED_CODE_SCOPES)


def test_search_request_contains_only_authority_context_and_trusted_domains() -> None:
    request = search_request(TICKET, ["sfmta.com"])
    assert TICKET.city_id in request.query
    assert TICKET.violation_code not in request.query
    assert request.official_domains == ("sfmta.com", *SHARED_CODE_DOMAINS, *CODE_PUBLISHER_DOMAINS)
    assert request.keys == CRITICAL_KEYS
    assert not request.broadened
    assert request.limit == MAX_SEARCH_HITS


@pytest.mark.parametrize("broadened", [False, True])
def test_unconstrained_ticket_text_never_enters_external_query(broadened: bool) -> None:
    sensitive = "SYN-1234567 Synthetic Customer 123 Example Street"
    ticket = replace(TICKET, violation_code=sensitive, violation_description=sensitive)
    request = search_request(ticket, ["sfmta.com"], broadened=broadened)
    assert sensitive not in request.query
    assert "SYN-1234567" not in request.query
    assert ticket.city_id in request.query


def test_broader_query_targets_only_unresolved_keys() -> None:
    request = search_request(
        TICKET, ["sfmta.com"], keys=frozenset({FactKey.MAILING_ADDRESS}), broadened=True
    )
    assert request.broadened
    assert "MAILING_ADDRESS" in request.query
    assert "DEADLINE_DAYS" not in request.query


@pytest.mark.parametrize(
    "url",
    [
        "https://sfmta.com.evil.com/contest",
        "https://sfmta.com@evil.com/contest",
        "https://127.0.0.1/contest",
        "http://sfmta.com/contest",
        "https://sfmta.com:8080/contest",
        "https://evil.com/contest",
    ],
)
def test_search_provider_cannot_expand_the_allowlist(url: str) -> None:
    assert select_pages(search_request(TICKET, ["sfmta.com"]), [SearchHit(url)], SCOPES) == ()


def test_authority_is_assigned_from_trusted_domain_not_search_metadata() -> None:
    request = search_request(TICKET, ["sfmta.com"])
    selected = select_pages(
        request,
        [
            SearchHit("https://sfmta.com/contest"),
            SearchHit(f"https://{SHARED_CODE_DOMAINS[0]}/faces/codes.xhtml"),
        ],
        SCOPES,
    )
    assert [p.authority_class for p in selected] == [
        AuthorityClass.OFFICIAL_PROCEDURE,
        AuthorityClass.STATUTE,
    ]
    assert all(p.covers == CRITICAL_KEYS for p in selected)


def test_fragments_do_not_create_additional_fetch_candidates() -> None:
    request = search_request(TICKET, ["sfmta.com"])
    selected = select_pages(
        request,
        [
            SearchHit("https://sfmta.com/contest#mail"),
            SearchHit("https://sfmta.com/contest#deadline"),
            SearchHit("https://sfmta.com/contest"),
        ],
        SCOPES,
    )
    assert [p.url for p in selected] == ["https://sfmta.com/contest"]


def test_inspected_hit_count_is_bounded() -> None:
    request = search_request(TICKET, ["sfmta.com"])
    hits = [SearchHit(f"https://sfmta.com/page{i}") for i in range(MAX_SEARCH_HITS + 20)]
    assert len(select_pages(request, hits, SCOPES)) == MAX_SEARCH_HITS


@pytest.mark.parametrize("limit", [0, -1, MAX_SEARCH_HITS + 1])
def test_invalid_search_limits_are_rejected(limit: int) -> None:
    request = SearchRequest("q", ("sfmta.com",), CRITICAL_KEYS, False, limit)
    with pytest.raises(ValueError, match="limit"):
        select_pages(request, [], SCOPES)


def test_search_hosts_do_not_expand_scoped_candidate_permissions() -> None:
    scope = SourceScope("portal.com", "/sf")
    request = search_request(TICKET, ["sfmta.com", scope.host])
    selected = select_pages(
        request,
        [
            SearchHit("https://portal.com/sf"),
            SearchHit("https://portal.com/sf/contest"),
            SearchHit("https://portal.com/sfx"),
            SearchHit("https://portal.com/sf-other"),
            SearchHit("https://sub.portal.com/sf"),
            SearchHit("https://portal.com/sf/%2e/contest"),
            SearchHit("https://portal.com/SF"),
        ],
        (*SCOPES, scope),
    )
    assert [page.url for page in selected] == [
        "https://portal.com/sf",
        "https://portal.com/sf/contest",
    ]
