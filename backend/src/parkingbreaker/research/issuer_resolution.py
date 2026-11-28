"""The issuer-resolution step shared by research and intake, with injected effects.

Search hits are leads only. Directory ownership supplies candidate scopes; the
existing AI assessors and saved-evidence guards establish identity and delegation.
No provider, clock, filesystem or network is accessed directly here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from itertools import chain
from urllib.parse import urldefrag, urlsplit

from parkingbreaker.agencies import ResolvedAgency, SourceScope
from parkingbreaker.case_state import HardStop, StopFamily
from parkingbreaker.llm import BudgetExhaustedResult, Usage
from parkingbreaker.research.delegation import (
    DelegationOutcome,
    DelegationResult,
    LinkPurpose,
    extract_links,
    has_purpose_term,
)
from parkingbreaker.research.issuer_assessment import (
    IssuerAssessor,
    IssuerEvidence,
    IssuerOutcome,
    IssuerResolution,
    resolve_issuer,
)
from parkingbreaker.research.jurisdiction import (
    DotGovSnapshot,
    IssuerCandidate,
    IssuerQuery,
    MatchOutcome,
    group_dotgov_records,
    issuer_candidates,
    match_outcome,
    snapshot_is_current,
)
from parkingbreaker.research.link_assessment import LinkAssessor, assess_delegations
from parkingbreaker.research.search import Searcher, SearchError, SearchHit, SearchRequest
from parkingbreaker.research.sources import (
    AuthorityClass,
    Clock,
    Fetcher,
    FetchFailure,
    SourceRecord,
    fetch_source,
    is_allowed,
)
from parkingbreaker.research.veto_codes import VetoCode

# Six lead fetches across all candidates allow recovery without unbounded retries.
# Each fetch_source call retains its own existing allowlisted redirect-hop bound.
MAX_ISSUER_FETCH_ATTEMPTS = 6


def _parking_navigation(page: SourceRecord, scopes: tuple[SourceScope, ...]) -> list[str]:
    """Same-authority discovery leads only. A link cannot verify the issuer or filing route."""
    contest: list[str] = []
    parking: list[str] = []
    for link in extract_links(page.html or "", page.final_url):
        label = link.anchor_text.casefold()
        url = link.href
        if url == page.final_url or not is_allowed(url, scopes):
            continue
        if re.search(r"\b(?:parking|tickets?|citations?)\b", label) and has_purpose_term(
            label, LinkPurpose.CONTEST
        ):
            contest.append(url)
        elif label in {
            "parking",
            "parking information",
            "parking services",
            "parking tickets",
            "parking citations",
            "parking enforcement",
            "parking division",
            "parking violations",
        }:
            parking.append(url)
    return list(dict.fromkeys(contest + parking))


@dataclass(frozen=True)
class IssuerResolutionAttempt:
    stage: str
    url: str | None = None
    detail: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class IssuerResolutionRun:
    outcome: IssuerOutcome
    agency: ResolvedAgency | None
    stop: HardStop | None
    reason: str
    candidates: tuple[IssuerCandidate, ...]
    attempts: tuple[IssuerResolutionAttempt, ...]
    pages: tuple[SourceRecord, ...]
    issuer_evidence: IssuerEvidence
    delegations: tuple[DelegationResult, ...]
    usage: Usage
    code: VetoCode | None = None

    @property
    def stop_family(self) -> StopFamily | None:
        return StopFamily.NOT_YET_VERIFIED if self.stop is not None else None


def _search_candidates(
    snapshot: DotGovSnapshot, hits: tuple[SearchHit, ...], limit: int
) -> tuple[IssuerCandidate, ...]:
    groups = tuple(
        IssuerCandidate(
            records[0],
            kind,
            0,
            ("snapshot domain search lead",),
            tuple(sorted({record.domain for record in records})),
        )
        for kind, records in group_dotgov_records(snapshot)
    )
    selected: list[IssuerCandidate] = []
    for hit in hits:
        try:
            host = (urlsplit(hit.url).hostname or "").removesuffix(".")
        except ValueError:
            continue
        for candidate in groups:
            if candidate not in selected and any(
                host == domain or host.endswith("." + domain) for domain in candidate.domains
            ):
                selected.append(candidate)
                if len(selected) == limit:
                    return tuple(selected)
    return tuple(selected)


def resolve_ticket_issuer(
    query: IssuerQuery,
    printed_stage: str | None,
    snapshot: DotGovSnapshot,
    today: date,
    searcher: Searcher | None,
    fetcher: Fetcher,
    issuer_assessor: IssuerAssessor,
    link_assessor: LinkAssessor,
    clock: Clock,
    budget_cents: int,
    max_candidates: int = 3,
    max_pages_per_candidate: int = 3,
    *,
    candidate_urls: tuple[str, ...] = (),
) -> IssuerResolutionRun:
    """Resolve once, then assess one-hop delegations on the resolved issuer's pages.

    The caller supplies its remaining shared pre-hold AI budget. Every AI call
    receives the unspent balance as a hard ceiling; refusals spend nothing and
    record budget exhaustion. Reaching the cap suppresses subsequent calls.
    Root fetches come first so even a one-page cap tries the required fallback.
    Only saved, distinct evidence pages consume page slots. Lead fetch attempts
    across all candidates are capped at MAX_ISSUER_FETCH_ATTEMPTS, including failures;
    provider searches are never retried here. For a single directory match without
    a specific lead, use the remaining slots for navigation on its own official sites.
    """
    if type(budget_cents) is not int or budget_cents < 0:
        raise ValueError("budget_cents must be a nonnegative integer")
    if type(max_candidates) is not int or max_candidates < 1:
        raise ValueError("max_candidates must be a positive integer")
    if type(max_pages_per_candidate) is not int or max_pages_per_candidate < 1:
        raise ValueError("max_pages_per_candidate must be a positive integer")
    attempts: list[IssuerResolutionAttempt] = []
    candidates: tuple[IssuerCandidate, ...] = ()
    pages: list[SourceRecord] = []
    delegations: list[DelegationResult] = []
    usage = Usage()

    def finish(resolution: IssuerResolution) -> IssuerResolutionRun:
        return IssuerResolutionRun(
            resolution.outcome,
            resolution.agency,
            resolution.stop,
            resolution.reason,
            candidates,
            tuple(attempts),
            tuple(pages),
            resolution.evidence,
            tuple(delegations),
            usage,
            resolution.code,
        )

    def unverified(reason: str, code: VetoCode) -> IssuerResolutionRun:
        return finish(
            IssuerResolution(
                IssuerOutcome.NOT_YET_VERIFIED,
                None,
                HardStop.S2,
                reason,
                IssuerEvidence(None, (), ()),
                code,
            )
        )

    def search(stage: str, request: SearchRequest) -> tuple[SearchHit, ...]:
        if searcher is None:
            attempts.append(
                IssuerResolutionAttempt(
                    stage, detail=request.query, error="search provider unavailable"
                )
            )
            return ()
        try:
            hits = searcher.search(request)
        except SearchError as exc:
            attempts.append(IssuerResolutionAttempt(stage, detail=request.query, error=str(exc)))
            return ()
        attempts.append(IssuerResolutionAttempt(stage, detail=request.query))
        return tuple(hits[: request.limit])

    current = snapshot_is_current(snapshot, today)
    attempts.append(
        IssuerResolutionAttempt("snapshot", error=None if current else "DotGov snapshot is stale")
    )
    if not current:
        return unverified("DotGov snapshot is stale", VetoCode.SNAPSHOT_STALE)

    candidates = issuer_candidates(query, snapshot)
    outcome = match_outcome(candidates)
    attempts.append(IssuerResolutionAttempt("candidates", detail=outcome.value))
    if outcome is MatchOutcome.NONE:
        # Empty domains means unrestricted. Hits nominate directory organizations
        # only; their snippets and pages never establish issuer identity.
        hits = search(
            "issuer_search",
            SearchRequest(f"{query.printed_issuer} parking citation", (), frozenset(), False),
        )
        candidates = _search_candidates(snapshot, hits, max_candidates)
        attempts.append(IssuerResolutionAttempt("search_candidates", detail=str(len(candidates))))
        if not candidates:
            return unverified("No search leads on DotGov snapshot domains", VetoCode.NO_CANDIDATE)
    else:
        candidates = (
            candidates[:1] if outcome is MatchOutcome.SINGLE else candidates[:max_candidates]
        )

    # Keep the original candidate scopes for link checks; newly authenticated roots
    # must not expand this run into a second delegation hop.
    fetched_scopes: list[tuple[SourceRecord, SourceScope]] = []
    fetch_attempts = 0
    for candidate in candidates:
        if fetch_attempts >= MAX_ISSUER_FETCH_ATTEMPTS:
            break
        scopes = tuple(SourceScope(domain) for domain in candidate.domains)
        hits = search(
            "candidate_search",
            SearchRequest(
                f"{query.printed_issuer} parking citation contest",
                candidate.domains,
                frozenset(),
                False,
            ),
        )
        directory_root = f"https://{candidate.domains[0]}/"
        urls = [directory_root]
        # A record may nominate a page on this candidate's already verified host.
        # Neither a legacy record nor a redirect authenticates another domain.
        for lead in chain((hit.url for hit in hits), candidate_urls):
            if is_allowed(lead, scopes):
                url, _ = urldefrag(lead)
                if url not in urls:
                    urls.append(url)
        use_navigation = outcome is MatchOutcome.SINGLE and len(urls) == 1
        candidate_pages = 0
        for url in urls:
            if (
                candidate_pages >= max_pages_per_candidate
                or fetch_attempts >= MAX_ISSUER_FETCH_ATTEMPTS
            ):
                break
            fetch_attempts += 1
            fetched = fetch_source(url, AuthorityClass.OFFICIAL_PROCEDURE, scopes, fetcher, clock)
            attempts.append(
                IssuerResolutionAttempt(
                    "fetch",
                    url=url,
                    error=fetched.reason if isinstance(fetched, FetchFailure) else None,
                )
            )
            if isinstance(fetched, FetchFailure) and url == directory_root:
                # Some directory roots advertise their own www site over HTTP.
                # Build the new HTTPS lead from the directory name.
                www_host = f"www.{candidate.domains[0]}"
                if fetched.blocked_first_redirect in (f"http://{www_host}", f"http://{www_host}/"):
                    secure_root = f"https://{www_host}/"
                    if secure_root not in urls:
                        urls.append(secure_root)
            if isinstance(fetched, SourceRecord):
                if any(page.final_url == fetched.final_url for page in pages):
                    continue
                pages.append(fetched)
                candidate_pages += 1
                root = next(scope for scope in scopes if is_allowed(fetched.final_url, (scope,)))
                fetched_scopes.append((fetched, root))
                if use_navigation and candidate_pages < max_pages_per_candidate:
                    for lead in _parking_navigation(fetched, scopes):
                        if len(urls) >= MAX_ISSUER_FETCH_ATTEMPTS:
                            break
                        if lead not in urls:
                            urls.append(lead)

    if usage.cost_cents >= budget_cents:
        attempts.append(IssuerResolutionAttempt("issuer_assessment", error="budget exhausted"))
        return unverified("AI budget exhausted", VetoCode.BUDGET_EXHAUSTED)
    if not pages:
        attempts.append(IssuerResolutionAttempt("issuer_assessment", error="no fetched pages"))
        return unverified("No official issuer pages fetched", VetoCode.NO_PAGES)
    assessed = issuer_assessor.assess(
        query,
        printed_stage,
        candidates,
        tuple(pages),
        max_cost_cents=budget_cents - usage.cost_cents,
    )
    if isinstance(assessed, BudgetExhaustedResult):
        attempts.append(IssuerResolutionAttempt("issuer_assessment", error="budget exhausted"))
        return unverified("AI budget exhausted", VetoCode.BUDGET_EXHAUSTED)
    assessment, cost = assessed
    usage += cost
    attempts.append(IssuerResolutionAttempt("issuer_assessment"))
    resolution = resolve_issuer(query, candidates, tuple(pages), assessment, printed_stage)
    attempts.append(IssuerResolutionAttempt("issuer_resolution", detail=resolution.reason))
    if resolution.outcome is IssuerOutcome.NOT_YET_VERIFIED:
        return finish(resolution)

    agency = resolution.agency
    assert agency is not None
    original_scopes = agency.source_scopes()
    domains = list(agency.official_domains)
    delegated = list(agency.delegated_scopes)
    for page, root in fetched_scopes:
        if (
            page.content_type != "text/html"
            or page.html is None
            or not is_allowed(page.final_url, original_scopes)
        ):
            continue
        links = extract_links(page.html, page.final_url)
        if usage.cost_cents >= budget_cents:
            attempts.append(
                IssuerResolutionAttempt(
                    "link_assessment", url=page.final_url, error="budget exhausted"
                )
            )
            continue
        assessed_links = link_assessor.assess(
            page,
            links,
            agency.name,
            printed_stage,
            max_cost_cents=budget_cents - usage.cost_cents,
        )
        if isinstance(assessed_links, BudgetExhaustedResult):
            attempts.append(
                IssuerResolutionAttempt(
                    "link_assessment", url=page.final_url, error="budget exhausted"
                )
            )
            continue
        assessments, cost = assessed_links
        usage += cost
        attempts.append(IssuerResolutionAttempt("link_assessment", url=page.final_url))
        results = assess_delegations(page, root, agency.name, links, printed_stage, assessments)
        delegations.extend(results)
        for result in results:
            attempts.append(
                IssuerResolutionAttempt(
                    "delegation",
                    url=result.evidence.href,
                    detail=result.outcome.value,
                    error=result.reason if result.stop is not None else None,
                )
            )
            if result.outcome is DelegationOutcome.DELEGATED_CONTEST and result.scope is not None:
                if result.scope not in delegated:
                    delegated.append(result.scope)
            elif (
                result.outcome is DelegationOutcome.AUTHENTICATED_ROOT
                and result.scope is not None
                and result.scope.host not in domains
            ):
                domains.append(result.scope.host)
    agency = replace(agency, official_domains=tuple(domains), delegated_scopes=tuple(delegated))
    return finish(replace(resolution, agency=agency))
