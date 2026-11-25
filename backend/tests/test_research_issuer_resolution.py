from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import date, timedelta

import pytest

from parkingbreaker.agencies import SourceScope
from parkingbreaker.case_state import HardStop, StopFamily
from parkingbreaker.llm import Usage
from parkingbreaker.research.delegation import DelegationOutcome
from parkingbreaker.research.fakes import (
    FakeFetcher,
    FakeLlmClient,
    FakeSearcher,
    fixed_clock,
    html_response,
    redirect_response,
)
from parkingbreaker.research.issuer_assessment import IssuerOutcome, IssuerQuote, LlmIssuerAssessor
from parkingbreaker.research.issuer_resolution import (
    MAX_ISSUER_FETCH_ATTEMPTS,
    IssuerResolutionRun,
    resolve_ticket_issuer,
)
from parkingbreaker.research.jurisdiction import (
    DotGovRecord,
    DotGovSnapshot,
    IssuerQuery,
    MatchOutcome,
    issuer_candidates,
    match_outcome,
)
from parkingbreaker.research.link_assessment import LlmLinkAssessor
from parkingbreaker.research.search import SearchError, SearchHit, SearchRequest
from parkingbreaker.research.sources import (
    AuthorityClass,
    FetchResponse,
    SourceRecord,
    fetch_source,
)
from parkingbreaker.research.veto_codes import VetoCode
from parkingbreaker.research_records import decode_research, research_json
from samples import RETRIEVED_AT
from test_research_issuer_assessment import AI, BODY_QUOTE, ISSUER_QUOTE, NAME, TYPE_QUOTE, reply
from test_research_records import result_fixture

TODAY = date(2026, 10, 13)
ROOT = "https://sfmta.gov/"
CONTEST = "https://sfmta.gov/contest"
PORTAL = "https://portal.example.com/SF/contest"
ANCHOR = "Contest this parking citation"
HTML = (
    f"<main><p>{ISSUER_QUOTE}</p><p>{BODY_QUOTE}</p><p>{TYPE_QUOTE}</p>"
    f'<p><a href="{PORTAL}">{ANCHOR}</a></p></main>'
)
RECORD = DotGovRecord("sfmta.gov", "City", NAME, None, "San Francisco", "CA")
ISSUER_COST = Usage(100, 20, 5)
LINK_COST = Usage(50, 10, 2)


def issuer_reply(html: str = HTML, index: int = 1) -> FakeLlmClient:
    sha = hashlib.sha256(html.encode()).hexdigest()
    return FakeLlmClient(
        reply(
            replace(
                AI,
                candidate_index=index,
                issuer_evidence=IssuerQuote(ISSUER_QUOTE, sha),
                deciding_body_evidence=IssuerQuote(BODY_QUOTE, sha),
                issuer_type_evidence=IssuerQuote(TYPE_QUOTE, sha),
            )
        ),
        ISSUER_COST,
    )


@dataclass
class Harness:
    snapshot: DotGovSnapshot
    query: IssuerQuery
    fetcher: FakeFetcher
    searcher: FakeSearcher | None
    issuer: FakeLlmClient
    link: FakeLlmClient

    def run(
        self,
        budget: int = 25,
        max_candidates: int = 3,
        max_pages: int = 3,
        leads: tuple[str, ...] = (),
    ) -> IssuerResolutionRun:
        return resolve_ticket_issuer(
            self.query,
            "Initial review",
            self.snapshot,
            TODAY,
            self.searcher,
            self.fetcher,
            LlmIssuerAssessor(self.issuer),
            LlmLinkAssessor(self.link),
            fixed_clock(RETRIEVED_AT),
            budget,
            max_candidates,
            max_pages,
            candidate_urls=leads,
        )


@pytest.fixture
def harness() -> Harness:
    return Harness(
        DotGovSnapshot(TODAY, (RECORD,), {}, "a" * 64),
        IssuerQuery(NAME, "San Francisco", "CA"),
        FakeFetcher({ROOT: html_response(ROOT, HTML)}),
        FakeSearcher(lambda _: ()),
        issuer_reply(),
        FakeLlmClient(
            {
                "links": [
                    {
                        "index": 1,
                        "purpose": "CONTEST",
                        "belongs_to_issuer": True,
                        "stage": "Initial review",
                        "verbatim_quote": ANCHOR,
                    }
                ]
            },
            LINK_COST,
        ),
    )


def assert_unverified(result: IssuerResolutionRun, stop: HardStop = HardStop.S2) -> None:
    assert result.outcome is IssuerOutcome.NOT_YET_VERIFIED
    assert result.stop is stop
    assert result.stop_family is StopFamily.NOT_YET_VERIFIED
    assert result.agency is None
    assert isinstance(result.code, VetoCode)


@pytest.mark.parametrize("age", [32, -1])
def test_stale_snapshot_stops_before_any_external_call(harness: Harness, age: int) -> None:
    harness.snapshot = replace(harness.snapshot, snapshot_date=TODAY - timedelta(days=age))
    result = harness.run()
    assert_unverified(result)
    assert result.reason == "DotGov snapshot is stale"
    assert result.code is VetoCode.SNAPSHOT_STALE
    assert result.candidates == ()
    assert result.pages == ()
    assert result.delegations == ()
    assert result.usage == Usage()
    assert result.attempts[0].error == result.reason
    assert harness.fetcher.calls == []
    assert harness.issuer.calls == harness.link.calls == []
    assert harness.searcher is not None
    assert harness.searcher.calls == []


def test_single_sfmta_resolves_and_delegates_with_saved_html(harness: Harness) -> None:
    result = harness.run()
    assert result.outcome is IssuerOutcome.RESOLVED
    assert result.stop is None
    assert result.stop_family is None
    assert result.agency is not None
    assert result.agency.official_domains == ("sfmta.gov",)
    assert result.agency.delegated_scopes == (SourceScope("portal.example.com", "/SF/contest"),)
    assert result.pages[0].html == HTML
    assert result.pages[0].authority_class is AuthorityClass.OFFICIAL_PROCEDURE
    assert result.issuer_evidence.assessment is not None
    assert result.delegations[0].evidence.sha256 == result.pages[0].sha256
    assert result.delegations[0].evidence.root_host == "sfmta.gov"
    assert result.usage == ISSUER_COST + LINK_COST
    assert len(harness.issuer.calls) == len(harness.link.calls) == 1
    assert harness.fetcher.calls == [ROOT]
    assert harness.searcher is not None
    request = harness.searcher.calls[0]
    assert request.official_domains == ("sfmta.gov",)
    assert request.query == f"{NAME} parking citation contest"
    assert [attempt.stage for attempt in result.attempts] == [
        "snapshot",
        "candidates",
        "candidate_search",
        "fetch",
        "issuer_assessment",
        "issuer_resolution",
        "link_assessment",
        "delegation",
    ]
    attribute = "reason"
    with pytest.raises(FrozenInstanceError):
        setattr(result, attribute, "changed")


def test_ambiguous_candidates_both_fetched_one_issuer_call(harness: Harness) -> None:
    other = replace(RECORD, domain="transport.gov")
    harness.query = replace(harness.query, printed_issuer="San Francisco Municipal Transportation")
    # Different organizations with equal distinctive-token scores; not one group.
    harness.snapshot = replace(
        harness.snapshot,
        records=(RECORD, replace(other, organization=NAME + " Department")),
    )
    assert (
        match_outcome(issuer_candidates(harness.query, harness.snapshot)) is MatchOutcome.AMBIGUOUS
    )
    other_url = "https://transport.gov/"
    harness.fetcher = FakeFetcher(
        {ROOT: html_response(ROOT, HTML), other_url: html_response(other_url, "Other candidate")}
    )
    result = harness.run()
    assert result.agency is not None
    assert len(result.candidates) == len(result.pages) == 2
    assert harness.fetcher.calls == [ROOT, other_url]
    assert len(harness.issuer.calls) == len(harness.link.calls) == 1
    assert "CANDIDATES (2)" in harness.issuer.calls[0][1]
    assert "PAGES (2)" in harness.issuer.calls[0][1]


def test_none_search_lead_groups_all_organization_domains(harness: Harness) -> None:
    harness.query = replace(harness.query, printed_issuer="Unfamiliar printed agency")
    harness.snapshot = replace(
        harness.snapshot, records=(RECORD, replace(RECORD, domain="transport.gov"))
    )
    assert match_outcome(issuer_candidates(harness.query, harness.snapshot)) is MatchOutcome.NONE
    harness.searcher = FakeSearcher(
        lambda request: (
            (SearchHit("https://www.transport.gov/citations"),)
            if not request.official_domains
            else ()
        )
    )
    result = harness.run()
    assert result.agency is not None
    assert result.agency.official_domains == ("sfmta.gov", "transport.gov")
    assert len(result.candidates) == 1
    assert result.candidates[0].score == 0
    assert harness.searcher.calls[0].official_domains == ()
    assert harness.searcher.calls[0].query == "Unfamiliar printed agency parking citation"
    assert len(harness.searcher.calls) == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://philapark.org/",
        "https://sfmta.gov.evil.org/",
        "https://evilsfmta.gov/",
        "https://[",
    ],
)
def test_none_with_only_non_snapshot_or_invalid_hits_stops(harness: Harness, url: str) -> None:
    harness.query = replace(harness.query, printed_issuer="Unfamiliar agency")
    harness.searcher = FakeSearcher(lambda _: (SearchHit(url),))
    result = harness.run()
    assert_unverified(result)
    assert result.candidates == ()
    assert result.code is VetoCode.NO_CANDIDATE
    assert len(harness.searcher.calls) == 1
    assert harness.fetcher.calls == []
    assert harness.issuer.calls == harness.link.calls == []


def test_issuer_veto_propagates_stop_and_evidence(harness: Harness) -> None:
    assert isinstance(harness.issuer.reply, dict)
    harness.issuer.reply["conflicted"] = True
    result = harness.run()
    assert_unverified(result, HardStop.S7)
    assert "conflicting" in result.reason
    assert result.code is VetoCode.AI_CONFLICT
    assert result.issuer_evidence.assessment is not None
    assert result.issuer_evidence.assessment.conflicted
    assert result.usage == ISSUER_COST
    assert harness.link.calls == []


def test_payment_link_cannot_delegate_even_if_ai_calls_it_contest(harness: Harness) -> None:
    html = HTML.replace(ANCHOR, "Pay this parking citation")
    harness.fetcher = FakeFetcher({ROOT: html_response(ROOT, html)})
    harness.issuer = issuer_reply(html)
    assert isinstance(harness.link.reply, dict)
    links = harness.link.reply["links"]
    assert isinstance(links, list)
    assert isinstance(links[0], dict)
    links[0]["verbatim_quote"] = "Pay this parking citation"
    result = harness.run()
    assert result.agency is not None
    assert result.agency.delegated_scopes == ()
    assert result.delegations[0].outcome is DelegationOutcome.NOT_YET_VERIFIED
    assert "payment" in result.delegations[0].reason.lower()


def test_zero_budget_skips_all_ai(harness: Harness) -> None:
    result = harness.run(budget=0)
    assert_unverified(result)
    assert result.reason == "AI budget exhausted"
    assert result.code is VetoCode.BUDGET_EXHAUSTED
    assert result.attempts[-1].error == "budget exhausted"
    assert harness.issuer.calls == harness.link.calls == []
    assert result.usage == Usage()


def test_issuer_spend_exhausts_budget_without_removing_resolution(
    harness: Harness,
) -> None:
    result = harness.run(budget=5)
    assert result.agency is not None
    assert result.agency.delegated_scopes == ()
    assert result.outcome is IssuerOutcome.RESOLVED
    assert result.usage == ISSUER_COST
    assert len(harness.issuer.calls) == 1
    assert harness.link.calls == []
    assert result.attempts[-1].stage == "link_assessment"
    assert result.attempts[-1].error == "budget exhausted"
    assert harness.issuer.ceilings == [5]
    assert harness.link.ceilings == []


def test_over_ceiling_issuer_call_is_refused_without_resolution_or_spend(
    harness: Harness,
) -> None:
    result = harness.run(budget=1)
    assert_unverified(result)
    assert result.reason == "AI budget exhausted"
    assert result.code is VetoCode.BUDGET_EXHAUSTED
    assert result.attempts[-1].stage == "issuer_assessment"
    assert result.attempts[-1].error == "budget exhausted"
    assert result.usage == Usage()
    assert result.issuer_evidence.assessment is None
    assert result.delegations == ()
    assert harness.issuer.calls == harness.link.calls == []
    assert harness.issuer.ceilings == [1]
    assert harness.link.ceilings == []


@pytest.mark.parametrize("link_cost", [7, 8])
def test_remaining_budget_is_a_hard_ceiling_on_second_call(
    harness: Harness, link_cost: int
) -> None:
    harness.issuer.usage = Usage(100, 20, 18)
    harness.link.usage = Usage(50, 10, link_cost)
    result = harness.run(budget=25)
    assert result.agency is not None
    assert harness.issuer.ceilings == [25]
    assert harness.link.ceilings == [7]
    assert result.usage.cost_cents <= 25
    if link_cost == 7:
        assert result.usage == harness.issuer.usage + harness.link.usage
        assert len(harness.link.calls) == 1
        assert len(result.delegations) == 1
    else:
        assert result.usage == harness.issuer.usage
        assert harness.link.calls == []
        assert result.delegations == ()
        assert result.agency.delegated_scopes == ()
        assert result.attempts[-1].stage == "link_assessment"
        assert result.attempts[-1].error == "budget exhausted"


def test_empty_zero_cost_reply_is_not_misreported_as_budget_exhaustion(
    harness: Harness,
) -> None:
    harness.issuer.reply = {}
    harness.issuer.usage = Usage()
    result = harness.run()
    assert_unverified(result)
    assert result.usage == Usage()
    assert all(attempt.error != "budget exhausted" for attempt in result.attempts)


def test_page_without_links_is_not_misreported_as_budget_exhaustion(harness: Harness) -> None:
    html = HTML.replace(f'<p><a href="{PORTAL}">{ANCHOR}</a></p>', "")
    harness.issuer = issuer_reply(html)
    harness.fetcher = FakeFetcher({ROOT: html_response(ROOT, html)})
    result = harness.run()
    assert result.agency is not None
    assert result.usage == ISSUER_COST
    assert harness.link.ceilings == []
    assert all(attempt.error != "budget exhausted" for attempt in result.attempts)


def test_fetch_failure_is_recorded_and_successful_search_page_resolves(harness: Harness) -> None:
    harness.fetcher = FakeFetcher({CONTEST: html_response(CONTEST, HTML)})
    harness.searcher = FakeSearcher(lambda _: (SearchHit(CONTEST),))
    result = harness.run()
    assert result.agency is not None
    failures = [
        attempt for attempt in result.attempts if attempt.stage == "fetch" and attempt.error
    ]
    assert len(failures) == 1
    assert failures[0].url == ROOT
    assert result.pages[0].url == CONTEST


def test_no_pages_skips_ai(harness: Harness) -> None:
    harness.fetcher = FakeFetcher({})
    result = harness.run()
    assert_unverified(result)
    assert result.attempts[-1].error == "no fetched pages"
    assert result.code is VetoCode.NO_PAGES
    assert harness.issuer.calls == harness.link.calls == []


def test_off_domain_search_hits_and_redirects_never_fetched(harness: Harness) -> None:
    hostile = "https://sfmta.gov.evil.org/contest"
    harness.searcher = FakeSearcher(lambda _: (SearchHit(hostile), SearchHit(CONTEST)))
    harness.fetcher = FakeFetcher(
        {ROOT: html_response(ROOT, HTML), CONTEST: redirect_response(CONTEST, hostile)}
    )
    result = harness.run()
    assert result.agency is not None
    assert harness.fetcher.calls == [ROOT, CONTEST]
    assert any(attempt.error and "allowlist" in attempt.error for attempt in result.attempts)


@pytest.mark.parametrize("max_pages", [1, 2, 3])
def test_page_cap_reserves_root_deduplicates_urls_and_failures_keep_page_slots(
    harness: Harness, max_pages: int
) -> None:
    # Only ROOT is fetchable. A failed fetch (T21, 2026-10-21) no longer consumes a saved-page
    # slot; failures are bounded by MAX_ISSUER_FETCH_ATTEMPTS instead.
    harness.searcher = FakeSearcher(
        lambda _: tuple(
            SearchHit(url) for url in (CONTEST, CONTEST + "#fragment", ROOT, ROOT + "other")
        )
    )
    result = harness.run(max_pages=max_pages)
    assert result.agency is not None
    expected = [ROOT] if max_pages == 1 else [ROOT, CONTEST, ROOT + "other"]
    assert harness.fetcher.calls == expected
    assert len(expected) <= MAX_ISSUER_FETCH_ATTEMPTS
    assert len([attempt for attempt in result.attempts if attempt.stage == "fetch"]) == len(
        expected
    )


@pytest.mark.parametrize("from_search", [False, True])
def test_candidate_cap_and_first_search_hit_order(harness: Harness, from_search: bool) -> None:
    records = tuple(
        replace(RECORD, domain=f"candidate{index}.gov", organization=f"{NAME} Department {index}")
        for index in range(4)
    )
    harness.snapshot = replace(harness.snapshot, records=records)
    if from_search:
        harness.query = replace(harness.query, printed_issuer="Unknown agency")
        harness.searcher = FakeSearcher(
            lambda request: (
                tuple(SearchHit(f"https://{record.domain}/") for record in reversed(records))
                if not request.official_domains
                else ()
            )
        )
    else:
        assert (
            match_outcome(issuer_candidates(harness.query, harness.snapshot))
            is MatchOutcome.AMBIGUOUS
        )
    result = harness.run(max_candidates=2)
    expected = (records[3], records[2]) if from_search else records[:2]
    assert tuple(candidate.record for candidate in result.candidates) == expected
    assert harness.fetcher.calls == [f"https://{record.domain}/" for record in expected]
    assert_unverified(result)


def test_search_failure_preserves_root_fallback(harness: Harness) -> None:
    def fail(_: SearchRequest) -> tuple[SearchHit, ...]:
        raise SearchError("search failed")

    harness.searcher = FakeSearcher(fail)
    result = harness.run()
    assert result.agency is not None
    assert result.attempts[2].error == "search failed"
    harness.query = replace(harness.query, printed_issuer="Unknown agency")
    assert_unverified(harness.run())


def test_missing_searcher_uses_root_for_single_but_cannot_discover_none(harness: Harness) -> None:
    harness.searcher = None
    assert harness.run().agency is not None
    harness.query = replace(harness.query, printed_issuer="Unknown agency")
    assert_unverified(harness.run())


def test_authenticated_root_and_delegation_are_deduplicated_without_following(
    harness: Harness,
) -> None:
    anchor = f"{NAME} official website"
    html = HTML.replace("</main>", f'<p><a href="https://sfmta.com/">{anchor}</a></p></main>')
    harness.issuer = issuer_reply(html)
    harness.fetcher = FakeFetcher(
        {ROOT: html_response(ROOT, html), CONTEST: html_response(CONTEST, html)}
    )
    harness.searcher = FakeSearcher(lambda _: (SearchHit(CONTEST),))
    assert isinstance(harness.link.reply, dict)
    links = harness.link.reply["links"]
    assert isinstance(links, list)
    links.append(
        {
            "index": 2,
            "purpose": "OFFICIAL_SITE",
            "belongs_to_issuer": True,
            "stage": None,
            "verbatim_quote": anchor,
        }
    )
    result = harness.run()
    assert result.agency is not None
    assert result.agency.official_domains == ("sfmta.gov", "sfmta.com")
    assert result.agency.delegated_scopes == (SourceScope("portal.example.com", "/SF/contest"),)
    assert [item.outcome for item in result.delegations] == [
        DelegationOutcome.DELEGATED_CONTEST,
        DelegationOutcome.AUTHENTICATED_ROOT,
        DelegationOutcome.DELEGATED_CONTEST,
        DelegationOutcome.AUTHENTICATED_ROOT,
    ]
    assert harness.fetcher.calls == [ROOT, CONTEST]
    assert result.usage == ISSUER_COST + LINK_COST + LINK_COST
    assert harness.issuer.ceilings == [25]
    assert harness.link.ceilings == [20, 18]


def test_link_budget_checked_before_each_page(harness: Harness) -> None:
    extra = HTML.replace("</main>", "<p>Another saved page</p></main>")
    harness.fetcher = FakeFetcher(
        {ROOT: html_response(ROOT, HTML), CONTEST: html_response(CONTEST, extra)}
    )
    harness.searcher = FakeSearcher(lambda _: (SearchHit(CONTEST),))
    result = harness.run(budget=7)
    assert result.agency is not None
    assert len(harness.link.calls) == 1
    assert result.usage.cost_cents == 7
    assert result.attempts[-1].url == CONTEST
    assert result.attempts[-1].error == "budget exhausted"
    assert harness.issuer.ceilings == [7]
    assert harness.link.ceilings == [2]


def test_redirect_mirrors_use_final_url_for_links_and_only_assess_once(harness: Harness) -> None:
    html = HTML.replace(PORTAL, "/SF/contest")
    harness.issuer = issuer_reply(html)
    harness.fetcher = FakeFetcher(
        {ROOT: redirect_response(ROOT, CONTEST), CONTEST: html_response(CONTEST, html)}
    )
    harness.searcher = FakeSearcher(lambda _: (SearchHit(CONTEST),))
    result = harness.run()
    assert result.agency is not None
    assert len(result.pages) == len(harness.link.calls) == 1
    assert result.delegations[0].evidence.href == "https://sfmta.gov/SF/contest"
    assert result.delegations[0].outcome is DelegationOutcome.ALREADY_OFFICIAL
    assert result.agency.delegated_scopes == ()


@pytest.mark.parametrize(
    "media", ["text/html", "text/plain", "application/xhtml+xml", "application/pdf"]
)
def test_html_only_populated_for_html_and_other_formats_skip_links(
    harness: Harness, media: str
) -> None:
    if media == "application/pdf":
        from fpdf import FPDF

        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=10)
        for quote_text in (ISSUER_QUOTE, BODY_QUOTE, TYPE_QUOTE):
            pdf.cell(0, 10, quote_text, new_x="LMARGIN", new_y="NEXT")
        body = bytes(pdf.output())
    else:
        body = (
            HTML.encode()
            if media != "text/plain"
            else "\n".join((ISSUER_QUOTE, BODY_QUOTE, TYPE_QUOTE)).encode()
        )
    harness.fetcher = FakeFetcher({ROOT: FetchResponse(ROOT, 200, {"content-type": media}, body)})
    fetched = fetch_source(
        ROOT,
        AuthorityClass.OFFICIAL_PROCEDURE,
        (SourceScope("sfmta.gov"),),
        harness.fetcher,
        fixed_clock(RETRIEVED_AT),
    )
    assert isinstance(fetched, SourceRecord)
    assert fetched.html == (HTML if media == "text/html" else None)
    assert isinstance(harness.issuer.reply, dict)
    for field in ("issuer_evidence", "deciding_body_evidence", "issuer_type_evidence"):
        quote = harness.issuer.reply[field]
        assert isinstance(quote, dict)
        quote["source_sha256"] = fetched.sha256
    result = harness.run()
    assert result.agency is not None
    assert len(harness.link.calls) == (1 if media == "text/html" else 0)


def test_html_is_ignored_by_existing_research_codec() -> None:
    original = result_fixture()
    with_html = replace(
        original, sources=tuple(replace(page, html=HTML) for page in original.sources)
    )
    assert research_json(with_html) == research_json(original)
    decoded = decode_research(research_json(with_html))
    assert decoded == with_html
    assert all(page.html is None for page in decoded.sources)


@pytest.mark.parametrize(
    ("budget", "max_candidates", "max_pages"), [(-1, 3, 3), (25, 0, 3), (25, 3, 0)]
)
def test_invalid_limits_rejected_before_external_calls(
    harness: Harness, budget: int, max_candidates: int, max_pages: int
) -> None:
    with pytest.raises(ValueError, match="must be"):
        harness.run(budget, max_candidates, max_pages)
    assert harness.fetcher.calls == []
    assert harness.issuer.calls == harness.link.calls == []


def test_curated_lead_recovers_only_on_the_registry_candidate_host(harness: Harness) -> None:
    harness.searcher = None
    harness.fetcher = FakeFetcher(
        {
            ROOT: redirect_response(ROOT, "https://www.sfmta.com/"),
            CONTEST: html_response(CONTEST, HTML),
        }
    )
    result = harness.run(leads=("https://www.sfmta.com/", CONTEST, CONTEST))
    assert harness.fetcher.calls == [ROOT, CONTEST]
    assert result.agency is not None
    assert result.agency.official_domains == ("sfmta.gov",)
    assert [page.final_url for page in result.pages] == [CONTEST]


def test_legacy_leads_do_not_displace_current_search_evidence(harness: Harness) -> None:
    harness.searcher = FakeSearcher(lambda _: (SearchHit(CONTEST),))
    harness.fetcher = FakeFetcher(
        {url: html_response(url, HTML) for url in (ROOT, ROOT + "legacy", CONTEST)}
    )
    result = harness.run(max_pages=2, leads=(ROOT + "legacy",))
    assert result.agency is not None
    assert harness.fetcher.calls == [ROOT, CONTEST]
