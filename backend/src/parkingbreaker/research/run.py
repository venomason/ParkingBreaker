"""Research pass 1 for one ticket: fetch, extract, guard, decide.

Every effect goes through the injected Fetcher, Extractor and clock, so this module is
deterministic given its inputs. The I/O layer maps the decision onto the case machine:
ok -> drafting continues (DraftReady comes later, from pass 2); not ok -> the resolution
ladder (architecture-v6 5.3). There is no human hold in v6.

The same pass supports supplied pages or fresh official-domain discovery. Optional
RecoveryConfig enables transient fetch retries, one re-extraction and one broader search.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from parkingbreaker.agencies import ResolvedAgency, SourceScope
from parkingbreaker.llm import LlmBudgetExceeded, LlmProviderError, Usage
from parkingbreaker.research.archive import blocked_site
from parkingbreaker.research.extractor import Extractor
from parkingbreaker.research.findings import (
    EvidenceBasis,
    Finding,
    FindingStatus,
    ReleaseDecision,
    UnknownReason,
    apply_quote_guard,
    apply_value_bounds,
    complete_findings,
    release_decision,
    verify_quote,
)
from parkingbreaker.research.ladder import (
    CriticalFact,
    Gap,
    GapKind,
    Recover,
    optional_recovery,
)
from parkingbreaker.research.payment_effects import (
    PaymentRule,
    PaymentScope,
    payment_rule_problem,
    retain_unresolved_payment_claim,
    uncovered_payment_claims,
    unresolved_payment_rule,
)
from parkingbreaker.research.procedure import (
    jurisdiction_in_text,
    quote_jurisdiction_matches,
    quote_procedure_section,
    quote_procedure_stage,
    same_stage,
    unread_linked_artifacts,
)
from parkingbreaker.research.questions import (
    ANSWERABLE_KEYS,
    EMPTY_PACKET_PLAN,
    NO_POSITION,
    NO_REQUIREMENTS,
    RETIRED_KEYS,
    CustomerPosition,
    EffectiveTiers,
    FactKey,
    FactTier,
    PacketPlan,
    Question,
    Ticket,
    VerifiedRequirements,
    build_questions,
    effective_tiers,
)
from parkingbreaker.research.resolution import (
    PendingResearch,
    RecoveryProgress,
    ResearchAnswers,
    ResearchResolution,
    TicketNoticeFacts,
    resolution_steps,
    resolve_research,
)
from parkingbreaker.research.search import (
    SHARED_CODE_SCOPES,
    Searcher,
    SearchError,
    search_request,
    select_pages,
)
from parkingbreaker.research.sources import (
    CODE_PUBLISHER_DOMAINS,
    AuthorityClass,
    Clock,
    EvidenceManifest,
    Fetcher,
    FetchFailure,
    SourceRecord,
    evidence_manifest,
    fetch_source,
    is_allowed,
    is_code_publisher,
    manifest_unchanged,
)

RESEARCH_REUSE_WINDOW = timedelta(hours=24)

# Every answerable key: retired keys stay decodable but are never researched again.
_ANSWER_KEYS = ANSWERABLE_KEYS


@dataclass(frozen=True)
class RecoveryConfig:
    retry_delays: tuple[float, ...] = (0.5, 1.5)
    max_pages: int = 20

    def __post_init__(self) -> None:
        if len(self.retry_delays) > 3 or any(
            not math.isfinite(delay) or delay < 0 or delay > 10 for delay in self.retry_delays
        ):
            raise ValueError("at most three finite retry delays in 0..10 seconds")
        if not 1 <= self.max_pages <= 20:
            raise ValueError("max_pages must be in 1..20")


@dataclass(frozen=True)
class ResearchAttempt:
    stage: str
    url: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ResearchResult:
    ticket: Ticket
    questions: tuple[Question, ...]
    sources: tuple[SourceRecord, ...]
    failures: tuple[FetchFailure, ...]
    findings: tuple[Finding, ...]
    decision: ReleaseDecision
    cost: Usage
    started_at: datetime
    finished_at: datetime
    candidates: tuple[Finding, ...] = ()
    progress: RecoveryProgress = field(default_factory=RecoveryProgress)
    pending: tuple[PendingResearch, ...] = ()
    attempts: tuple[ResearchAttempt, ...] = ()
    # Legacy stored field, retained for byte-identical history; new research never compares.
    address_mismatch: bool = False
    verified_requirements: VerifiedRequirements = NO_REQUIREMENTS
    customer_position: CustomerPosition = NO_POSITION
    packet_plan: PacketPlan = EMPTY_PACKET_PLAN
    tier_evaluation: EffectiveTiers | None = None
    record_version: int = 1
    payment_rules: tuple[PaymentRule, ...] = ()
    payment_scope: PaymentScope | None = None

    @property
    def manifest(self) -> EvidenceManifest:
        return evidence_manifest(
            self.ticket.jurisdiction or self.ticket.city_id,
            self.ticket.stage or "first-level review",
            self.sources,
        )

    def evidence_current_at(self, now: datetime) -> bool:
        """Bound a new submission by the original research and fetched evidence ages.

        This is a same-case freshness ceiling, not cross-case reuse permission or a
        source refresh. Never renew it when an answer, approval or retry is recorded.
        Lookup of an earlier send intent does not require current procedural evidence.
        """
        times = (self.started_at, self.finished_at, now, *(s.retrieved_at for s in self.sources))
        return (
            bool(self.sources)
            and all(value.tzinfo is not None and value.utcoffset() is not None for value in times)
            and self.started_at <= self.finished_at <= now
            and now - self.started_at < RESEARCH_REUSE_WINDOW
            and all(
                timedelta(0) <= now - source.retrieved_at < RESEARCH_REUSE_WINDOW
                and source.retrieved_at <= self.finished_at
                for source in self.sources
            )
        )

    def reusable_for(self, ticket: Ticket, now: datetime, new_hashes: Mapping[str, str]) -> bool:
        """Reuse a saved result only for the exact ticket that produced its conclusions.

        Page hashes, issuer and stage alone cannot establish that violation-specific or
        date-specific findings apply to a different ticket.
        """
        issuer = ticket.jurisdiction or ticket.city_id
        stage = ticket.stage or "first-level review"
        return (
            ticket == self.ticket
            and issuer == self.manifest.issuer
            and same_stage(stage, self.manifest.stage)
            and timedelta(0) <= now - self.started_at < RESEARCH_REUSE_WINDOW
            and not unread_linked_artifacts(self.sources)
            and manifest_unchanged(self.manifest, new_hashes)
        )

    def reusable_across(self, ticket: Ticket, now: datetime) -> bool:
        """Whether this saved research may serve another customer's ticket (v8 20.18).

        Only successful research for the same issuer and procedural stage, within 24 hours
        of its first run. Violation-specific conclusions are carried only for the same
        violation code, description and issue date. These inputs condition the original
        questions, so a change requires fresh extraction even when the pages are unchanged.
        Page hashes are checked separately.
        """
        issuer = ticket.jurisdiction or ticket.city_id
        stage = ticket.stage or "first-level review"
        return (
            self.decision.ok
            and RETIRED_KEYS.isdisjoint(q.key for q in self.questions)
            and RETIRED_KEYS.isdisjoint(f.key for f in self.findings)
            and issuer == self.manifest.issuer
            and same_stage(stage, self.manifest.stage)
            and _violation_key(ticket.violation_code) == _violation_key(self.ticket.violation_code)
            and ticket.violation_description == self.ticket.violation_description
            # Upgrade candidate: when DEADLINE_ABSOLUTE.applicable is False and re-fetched .gov
            # SHA-256s match, scope reuse by (issuer, stage, violation_code) without requiring
            # identical issue_date, while resolving each driver's deadline from their own ticket.
            and ticket.issue_date == self.ticket.issue_date
            and timedelta(0) <= now - self.started_at < RESEARCH_REUSE_WINDOW
            and not unread_linked_artifacts(self.sources)
        )

    def reused_for(
        self,
        ticket: Ticket,
        now: datetime,
        new_hashes: Mapping[str, str],
        *,
        verified_requirements: VerifiedRequirements = NO_REQUIREMENTS,
        customer_position: CustomerPosition = NO_POSITION,
        packet_plan: PacketPlan = EMPTY_PACKET_PLAN,
    ) -> ResearchResult | None:
        """This research rebuilt for another customer's ticket, or None to research afresh.

        Only guarded official findings and source-matched form PDFs survive. Customer
        inputs and unrestricted extractor/execution metadata never cross case boundaries.
        """
        if not (
            self.reusable_across(ticket, now) and manifest_unchanged(self.manifest, new_hashes)
        ):
            return None
        issuer = ticket.jurisdiction or ticket.city_id
        current = replace(ticket, jurisdiction=issuer, stage=ticket.stage or "first-level review")
        if uncovered_payment_claims(self.payment_rules, self.sources, current.stage):
            return None
        if self.payment_rules and (
            self.payment_scope is None
            or self.payment_scope.issuer != issuer
            or not same_stage(self.payment_scope.stage, current.stage or "")
            or any(
                payment_rule_problem(rule, self.sources, self.payment_scope) is not None
                for rule in self.payment_rules
            )
            or any(
                len({other.effect for other in self.payment_rules if other.action == rule.action})
                > 1
                for rule in self.payment_rules
            )
        ):
            # Invalid/unscoped text is not public evidence for another customer.
            # Refuse reuse rather than dropping its unresolved procedural gate.
            return None
        plan = replace(
            packet_plan,
            form_templates=(
                *packet_plan.form_templates,
                *(
                    b
                    for b in self.packet_plan.form_templates
                    if b not in packet_plan.form_templates
                    and any(
                        source.authority_class is AuthorityClass.OFFICIAL_FORM
                        and source.content_type == "application/pdf"
                        and source.sha256 == hashlib.sha256(b).hexdigest()
                        for source in self.sources
                    )
                ),
            ),
        )
        guarded = complete_findings(
            apply_value_bounds(
                apply_quote_guard(self.findings, self.sources, ticket_context=self.ticket)
            ),
            _ANSWER_KEYS,
        )
        findings = tuple(_public_finding(f, current, self.sources) for f in guarded)
        rebuilt = ResearchResult(
            ticket=current,
            questions=build_questions(current, issuer, current.stage),
            sources=self.sources,
            findings=findings,
            candidates=findings,
            failures=(),
            decision=ReleaseDecision(False, ()),
            cost=Usage(),
            started_at=self.started_at,
            finished_at=now,
            record_version=self.record_version,
            payment_rules=self.payment_rules,
            payment_scope=self.payment_scope,
        ).reassess(
            verified_requirements=verified_requirements,
            customer_position=customer_position,
            packet_plan=plan,
        )
        return rebuilt if rebuilt.decision.ok else None

    def reassess(
        self,
        *,
        verified_requirements: VerifiedRequirements | None = None,
        customer_position: CustomerPosition | None = None,
        packet_plan: PacketPlan | None = None,
    ) -> ResearchResult:
        """Re-evaluate saved inputs after edits or packet freeze, without new I/O.

        Persist the returned record to retain promotion triggers and their evidence.
        Legacy records keep their bytes until explicitly reassessed.
        """
        requirements = (
            verified_requirements
            if verified_requirements is not None
            else self.verified_requirements
        )
        position = customer_position if customer_position is not None else self.customer_position
        plan = packet_plan if packet_plan is not None else self.packet_plan
        tiers = effective_tiers(self.ticket, requirements, position, plan)
        findings = complete_findings(
            apply_value_bounds(
                apply_quote_guard(self.findings, self.sources, ticket_context=self.ticket)
            ),
            _ANSWER_KEYS,
        )
        rules = self.payment_rules
        if uncovered_payment_claims(rules, self.sources, self.ticket.stage):
            rules = retain_unresolved_payment_claim(rules)
        return replace(
            self,
            findings=findings,
            verified_requirements=requirements,
            customer_position=position,
            packet_plan=plan,
            tier_evaluation=tiers,
            decision=release_decision(
                findings, self.sources, tiers=tiers, ticket_context=self.ticket, packet_plan=plan
            ),
            pending=resolution_steps(
                findings,
                self.candidates,
                self.sources,
                self.progress,
                tiers=tiers,
                ticket_context=self.ticket,
                packet_plan=plan,
            ),
            record_version=5,
            payment_rules=rules,
        )

    def resolve(
        self,
        answers: ResearchAnswers | None = None,
        *,
        customer_position: CustomerPosition | None = None,
        packet_plan: PacketPlan | None = None,
        ticket_notice_facts: TicketNoticeFacts | None = None,
    ) -> ResearchResolution:
        current = self.reassess(customer_position=customer_position, packet_plan=packet_plan)
        return resolve_research(
            current.ticket,
            current.findings,
            current.candidates,
            self.sources,
            self.progress,
            answers if answers is not None else ResearchAnswers(),
            verified_requirements=current.verified_requirements,
            customer_position=current.customer_position,
            packet_plan=current.packet_plan,
            ticket_notice_facts=ticket_notice_facts,
            payment_rules=current.payment_rules,
            payment_scope=current.payment_scope,
        )


def _violation_key(code: str) -> str:
    return " ".join(code.split()).casefold()


def _public_finding(finding: Finding, ticket: Ticket, sources: tuple[SourceRecord, ...]) -> Finding:
    """Copy evidence, excluding unrestricted text from another case's extraction."""
    if finding.status is not FindingStatus.CONFIRMED:
        return Finding(
            finding.key,
            None,
            FindingStatus.UNKNOWN,
            None,
            (),
            unknown_reason=UnknownReason.MISSING_ANSWER,
        )
    issuer = ticket.jurisdiction or ticket.city_id
    stage = ticket.stage or "first-level review"
    assessment = (
        finding.procedure_assessment
        if finding.evidence_basis is EvidenceBasis.NONE_STATED_IN_OFFICIAL_PROCEDURE
        else None
    )
    if assessment is not None:
        page = next(source for source in sources if source.sha256 == assessment.source_sha256)
        assessment = replace(
            assessment,
            jurisdiction=issuer,
            stage=stage,
            ticket_type="parking citation",
            procedure_text=page.text,
            judgment="Saved procedure assessment; official bytes reverified.",
        )
    value = finding.value
    if finding.key is FactKey.MAILING_ADDRESS and isinstance(value, dict):
        value = {
            key: value.get(key)
            for key in ("recipient", "attention", "line1", "line2", "city", "state", "zip_code")
        }
    scope_quote = finding.scope_quote
    if scope_quote is not None and not any(
        source.sha256 in finding.source_sha256s and verify_quote(scope_quote, source.text)
        for source in sources
    ):
        scope_quote = None
    return Finding(
        key=finding.key,
        value=value,
        status=FindingStatus.CONFIRMED,
        verbatim_quote=finding.verbatim_quote,
        source_sha256s=finding.source_sha256s,
        jurisdiction=issuer,
        stage=stage if finding.stage is not None else None,
        evidence=tuple(_public_finding(item, ticket, sources) for item in finding.evidence),
        evidence_basis=finding.evidence_basis,
        procedure_assessment=assessment,
        scope_quote=scope_quote,
    )


@dataclass(frozen=True)
class SeedUrl:
    """A page research fetches for this ticket, with its authority set by us.

    Callers supply pages for this ticket.
    """

    url: str
    authority_class: AuthorityClass
    covers: frozenset[FactKey]


def _agency_scopes(agency: ResolvedAgency) -> tuple[SourceScope, ...]:
    # A commercial publisher is official only inside this issuer's delegated tenant.
    return tuple(
        scope
        for scope in agency.source_scopes()
        if scope.host not in CODE_PUBLISHER_DOMAINS or scope.path_prefix is not None
    )


def _same_research_source(saved: SourceRecord, fetched: SourceRecord) -> bool:
    if (
        saved.final_url != fetched.final_url
        or saved.content_type != fetched.content_type
        or saved.authority_class is not fetched.authority_class
        or saved.jurisdiction != fetched.jurisdiction
    ):
        return False
    return saved.sha256 == fetched.sha256 or (
        saved.content_type == "text/html"
        and saved.aspnet_state_sha256 is not None
        and saved.aspnet_state_sha256 == fetched.aspnet_state_sha256
        and saved.text == fetched.text
    )


def _rebind_finding(finding: Finding, hashes: Mapping[str, str]) -> Finding:
    assessment = finding.procedure_assessment
    if assessment is not None:
        assessment = replace(
            assessment, source_sha256=hashes.get(assessment.source_sha256, assessment.source_sha256)
        )
    return replace(
        finding,
        source_sha256s=tuple(hashes.get(sha, sha) for sha in finding.source_sha256s),
        evidence=tuple(_rebind_finding(item, hashes) for item in finding.evidence),
        procedure_assessment=assessment,
    )


def reuse_research(
    candidates: Iterable[ResearchResult],
    ticket: Ticket,
    agency: ResolvedAgency,
    fetcher: Fetcher,
    clock: Clock,
    now: datetime,
    *,
    verified_requirements: VerifiedRequirements = NO_REQUIREMENTS,
    customer_position: CustomerPosition = NO_POSITION,
    packet_plan: PacketPlan = EMPTY_PACKET_PLAN,
    remaining_time: timedelta = timedelta(minutes=30),
) -> ResearchResult | None:
    """Saved research for another customer's ticket, rebuilt for this one, or None.

    Candidates are supplied newest first. Before a candidate is accepted, every page, form
    and PDF it relied on is fetched again through the same allowlist. Only known ASP.NET
    hidden-state values may change, and only when extracted text also matches exactly.
    Every reused citation is checked again against the freshly fetched sources.
    """
    scopes = _agency_scopes(agency) + SHARED_CODE_SCOPES
    ticket = replace(ticket, jurisdiction=agency.id, stage=ticket.stage or "first-level review")
    time_limit = now + min(remaining_time, timedelta(minutes=30))
    for candidate in candidates:
        if not candidate.reusable_across(ticket, now):
            continue
        hashes: dict[str, str] = {}
        rebound_hashes: dict[str, str] = {}
        sources: list[SourceRecord] = []
        for source in candidate.sources:
            if time_limit - clock() <= timedelta(minutes=5, seconds=60):
                return None
            fetched = fetch_source(source.url, source.authority_class, scopes, fetcher, clock)
            if isinstance(fetched, FetchFailure):
                return None
            if is_code_publisher(fetched.final_url):
                fetched = replace(
                    fetched, authority_class=AuthorityClass.STATUTE, jurisdiction=agency.id
                )
            if not _same_research_source(source, fetched):
                return None
            # A hash used by two source URLs must have one unambiguous replacement.
            if rebound_hashes.setdefault(source.sha256, fetched.sha256) != fetched.sha256:
                return None
            sources.append(fetched)
            hashes[source.url] = fetched.sha256
        finished = clock()
        if finished < now or time_limit - finished <= timedelta(minutes=5):
            return None
        if any(old != fresh for old, fresh in rebound_hashes.items()):
            candidate = replace(
                candidate,
                findings=tuple(_rebind_finding(f, rebound_hashes) for f in candidate.findings),
                candidates=tuple(_rebind_finding(f, rebound_hashes) for f in candidate.candidates),
                payment_rules=tuple(
                    replace(
                        rule,
                        source_sha256s=tuple(
                            rebound_hashes.get(sha, sha) for sha in rule.source_sha256s
                        ),
                    )
                    for rule in candidate.payment_rules
                ),
            )
        reused = replace(candidate, sources=tuple(sources)).reused_for(
            ticket,
            finished,
            hashes,
            verified_requirements=verified_requirements,
            customer_position=customer_position,
            packet_plan=packet_plan,
        )
        if reused is not None:
            return reused
    return None


def run_research(
    ticket: Ticket,
    agency: ResolvedAgency,
    seeds: Sequence[SeedUrl],
    fetcher: Fetcher,
    extractor: Extractor,
    clock: Clock,
    *,
    searcher: Searcher | None = None,
    recovery: RecoveryConfig | None = None,
    wait: Callable[[float], None],
    max_cost_cents: int | None = None,
    stage: str | None = None,
    verified_requirements: VerifiedRequirements = NO_REQUIREMENTS,
    customer_position: CustomerPosition = NO_POSITION,
    packet_plan: PacketPlan = EMPTY_PACKET_PLAN,
    remaining_time: timedelta = timedelta(minutes=30),
) -> ResearchResult:
    """One research path, optionally with v6's bounded automatic recovery.

    With recovery enabled, exhausted gaps return ladder actions for the customer, a
    disclosed timing default or a stop. Those are not official CONFIRMED findings.
    The caller persists this result while awaiting the customer; answers are resolved
    against these saved pages rather than starting another automatic recovery budget.

    max_cost_cents caps AI spend for the whole run: each extraction gets the unspent
    balance, and once it is spent no further AI call is made ("budget exhausted").
    remaining_time is supplied from the original authorization, never renewed here.
    Optional work reserves five minutes for review/adoption and one minute per operation.
    """
    if max_cost_cents is not None and (type(max_cost_cents) is not int or max_cost_cents < 0):
        raise ValueError("max_cost_cents must be a nonnegative integer")
    if remaining_time < timedelta(0):
        raise ValueError("remaining_time must be nonnegative")
    started = clock()
    # Never renew the caller's authorization window. Pre-hold callers use the same cap.
    time_limit = started + min(remaining_time, timedelta(minutes=30))
    ticket = replace(
        ticket, jurisdiction=agency.id, stage=stage or ticket.stage or "first-level review"
    )
    questions = build_questions(ticket, agency.id, ticket.stage)
    tiers = effective_tiers(ticket, verified_requirements, customer_position, packet_plan)
    sources: list[SourceRecord] = []
    failures: list[FetchFailure] = []
    attempts: list[ResearchAttempt] = []
    candidates: list[Finding] = []
    payment_rules: list[PaymentRule] = []
    cost = Usage()
    optional_extraction_failed = False
    progress = RecoveryProgress()
    agency_scopes = _agency_scopes(agency)
    scopes = agency_scopes + SHARED_CODE_SCOPES
    domains = tuple(scope.host for scope in agency_scopes)
    pages = [seed for seed in seeds if not seed.covers or seed.covers - RETIRED_KEYS]
    seen: set[str] = set()
    max_pages = recovery.max_pages if recovery else 20
    if len(pages) > max_pages:
        raise ValueError(f"at most {max_pages} seed pages per research run")

    def discover(broadened: bool, keys: frozenset[FactKey]) -> None:
        request = search_request(ticket, domains, keys=keys, broadened=broadened)
        if not request.keys:
            return
        stage = "broadened_search" if broadened else "initial_search"
        if searcher is None:
            attempts.append(ResearchAttempt(stage, error="search provider unavailable"))
            return
        try:
            hits = searcher.search(request)
        except SearchError as exc:
            attempts.append(ResearchAttempt(stage, error=str(exc)))
            return
        attempts.append(ResearchAttempt(stage))
        for page in select_pages(request, hits, scopes):
            if page.url not in seen and len(pages) < max_pages:
                pages.append(SeedUrl(page.url, page.authority_class, page.covers))

    def time_fits(delay: float = 0) -> bool:
        return time_limit - clock() > timedelta(seconds=60 + delay)

    def optional_fits(delay: float = 0) -> bool:
        return (
            optional_recovery(
                Gap(CriticalFact.REQUIRED_ITEMS, GapKind.UNCONFIRMED),
                time_limit - clock(),
                operation_time=timedelta(seconds=60 + delay),
            )
            is not None
        )

    def seed_is_optional(seed: SeedUrl) -> bool:
        return bool(seed.covers) and all(
            tiers.tiers[key] is not FactTier.MUST_HAVE for key in seed.covers
        )

    def fetch_pages(*, optional: bool = False) -> None:
        nonlocal packet_plan
        # Spend the existing run allowance on required pages before optional pages.
        queue = sorted(pages, key=seed_is_optional)
        index = 0
        while index < len(queue):
            seed = queue[index]
            index += 1
            if seed.url in seen:
                continue
            seed_optional = optional or seed_is_optional(seed)
            if seed_optional and not optional_fits():
                attempts.append(ResearchAttempt("fetch", seed.url, "review time reserved"))
                seen.add(seed.url)
                continue
            if not time_fits():
                attempts.append(ResearchAttempt("fetch", seed.url, "time exhausted"))
                break
            seen.add(seed.url)
            fetched = fetch_source(seed.url, seed.authority_class, scopes, fetcher, clock)
            attempts.append(
                ResearchAttempt(
                    "fetch", seed.url, fetched.reason if isinstance(fetched, FetchFailure) else None
                )
            )
            # Transient network/HTTP failures only. Allowlist, document format and
            # definitive 4xx failures never consume retries or cause unsafe fetches.
            for delay in recovery.retry_delays if recovery else ():
                if not isinstance(fetched, FetchFailure) or not fetched.retryable:
                    break
                if seed_optional and not optional_fits(delay):
                    attempts.append(
                        ResearchAttempt("fetch_retry", seed.url, "review time reserved")
                    )
                    break
                if not time_fits(delay):
                    attempts.append(ResearchAttempt("fetch_retry", seed.url, "time exhausted"))
                    break
                wait(delay)
                fetched = fetch_source(seed.url, seed.authority_class, scopes, fetcher, clock)
                attempts.append(
                    ResearchAttempt(
                        "fetch_retry",
                        seed.url,
                        fetched.reason if isinstance(fetched, FetchFailure) else None,
                    )
                )
            if isinstance(fetched, SourceRecord):
                if is_code_publisher(fetched.final_url):
                    fetched = replace(
                        fetched, authority_class=AuthorityClass.STATUTE, jurisdiction=agency.id
                    )
                sources.append(fetched)
                if (
                    fetched.form_pdf is not None
                    and fetched.form_pdf not in packet_plan.form_templates
                ):
                    packet_plan = replace(
                        packet_plan,
                        form_templates=(*packet_plan.form_templates, fetched.form_pdf),
                    )
                if fetched.authority_class is not AuthorityClass.SECONDARY:
                    for url in (*fetched.linked_urls, *fetched.off_scope_linked_urls):
                        if not is_allowed(url, scopes):
                            attempts.append(ResearchAttempt("linked_artifact", url, "off scope"))
                            continue
                        if url in seen or any(page.url == url for page in pages):
                            continue
                        if len(pages) >= max_pages:
                            attempts.append(
                                ResearchAttempt("linked_artifact", url, "page budget exhausted")
                            )
                            continue
                        # fetch_source rechecks the allowlist and every redirect as usual.
                        authority = (
                            AuthorityClass.STATUTE
                            if is_code_publisher(url)
                            else AuthorityClass.OFFICIAL_FORM
                            if ".pdf" in url.lower()
                            else AuthorityClass.OFFICIAL_PROCEDURE
                        )
                        linked = SeedUrl(url, authority, frozenset())
                        pages.append(linked)
                        queue.append(linked)
            else:
                # A bot-blocked official site is an access condition, never a
                # process verdict and never a serviceability block: the archive
                # fallback (research/archive.py) is what may still verify it.
                block = blocked_site(fetched)
                if block is not None:
                    attempts.append(ResearchAttempt("live_site_block", block.site))
                failures.append(fetched)

    def extract(*, optional: bool = False) -> tuple[Finding, ...]:
        nonlocal cost, optional_extraction_failed
        if optional_extraction_failed:
            attempts.append(ResearchAttempt("extract", error="optional extraction unavailable"))
            return complete_findings(candidates, _ANSWER_KEYS, UnknownReason.UNANSWERABLE)
        if not time_fits():
            attempts.append(ResearchAttempt("extract", error="time exhausted"))
            return complete_findings(candidates, _ANSWER_KEYS, UnknownReason.TIME_EXHAUSTED)
        remaining = None if max_cost_cents is None else max_cost_cents - cost.cost_cents
        if remaining is not None and remaining <= 0:
            attempts.append(ResearchAttempt("extract", error="budget exhausted"))
            return complete_findings(candidates, _ANSWER_KEYS, UnknownReason.BUDGET_EXHAUSTED)
        try:
            extraction = extractor.extract(questions, tuple(sources), max_cost_cents=remaining)
        except (LlmBudgetExceeded, LlmProviderError) as exc:
            if not optional:
                raise
            # Preserve required evidence. A failed call has no reported usage; never
            # assume a fresh spend allowance or keep a valid appeal waiting for it.
            optional_extraction_failed = True
            attempts.append(ResearchAttempt("extract", error=str(exc)))
            return complete_findings(candidates, _ANSWER_KEYS, UnknownReason.UNANSWERABLE)
        if extraction.budget_exhausted:
            attempts.append(ResearchAttempt("extract", error="budget exhausted"))
        for payment_rule in extraction.payment_rules:
            if payment_rule not in payment_rules:
                if len(payment_rules) >= 32:
                    payment_rules[-1] = unresolved_payment_rule()
                    break
                payment_rules.append(payment_rule)
        if uncovered_payment_claims(payment_rules, sources, ticket.stage):
            payment_rules[:] = retain_unresolved_payment_claim(payment_rules)
        cost += extraction.usage
        reason = (
            UnknownReason.BUDGET_EXHAUSTED
            if extraction.budget_exhausted
            else UnknownReason.NO_SOURCES
            if not sources
            else UnknownReason.MISSING_ANSWER
        )
        explicit = complete_findings(extraction.findings, (q.key for q in questions), reason)

        def scoped_finding(f: Finding) -> Finding:
            # Jurisdiction comes from the agency allowlist, and stage from saved page
            # text. Neither missing field is filled from the requested ticket scope.
            cited = [s for s in sources if s.sha256 in f.source_sha256s]
            scoped_quote = (
                f.procedure_assessment.scope_quote
                if f.procedure_assessment is not None
                else f.verbatim_quote or ""
            )
            local = bool(cited) and all(
                is_allowed(s.final_url, agency_scopes)
                and (
                    s.authority_class in {AuthorityClass.STATUTE, AuthorityClass.REGULATION}
                    or (
                        jurisdiction_in_text(agency.id, s.text)
                        and quote_jurisdiction_matches(s.text, scoped_quote, agency.id)
                    )
                )
                for s in cited
            )
            stages = {quote_procedure_stage(s.text, f.verbatim_quote or "") for s in cited}
            stages.discard(None)
            source_stage = next(iter(stages)) if len(stages) == 1 else None
            return replace(
                f,
                jurisdiction=(f.jurisdiction or agency.id) if local else None,
                stage=f.stage or source_stage,
                scope_quote=f.scope_quote
                or (
                    section.text
                    if local
                    and source_stage is not None
                    and len(cited) == 1
                    and (section := quote_procedure_section(cited[0].text, f.verbatim_quote or ""))
                    is not None
                    else None
                ),
                evidence=tuple(scoped_finding(e) for e in f.evidence),
            )

        scoped = tuple(scoped_finding(f) for f in explicit)
        guarded = apply_value_bounds(apply_quote_guard(scoped, sources, ticket_context=ticket))
        candidates.extend(guarded)
        # Keep previously verified evidence; re-extraction cannot silently erase a
        # disagreement by changing its answer. New unsupported claims remain noise.
        return complete_findings(
            apply_value_bounds(apply_quote_guard(candidates, sources, ticket_context=ticket)),
            _ANSWER_KEYS,
        )

    if not pages and searcher is not None and time_fits():
        discover(False, frozenset(k for k in FactKey if tiers.tiers[k] is FactTier.MUST_HAVE))
    fetch_pages()
    findings = extract()
    if recovery is not None:

        def required_recovery_keys() -> frozenset[FactKey]:
            return frozenset(
                key
                for gap in resolution_steps(
                    findings,
                    candidates,
                    sources,
                    progress,
                    tiers=tiers,
                    ticket_context=ticket,
                    packet_plan=packet_plan,
                )
                if isinstance(gap.step, Recover)
                for key in gap.keys
                if key not in RETIRED_KEYS
            )

        def important_gaps() -> frozenset[FactKey]:
            # Retired keys never drive a recovery search, even when an old record still
            # carries an UNKNOWN or CONFLICTED finding for one.
            return frozenset(
                f.key
                for f in findings
                if f.key not in RETIRED_KEYS
                and tiers.tiers[f.key] is FactTier.IMPORTANT
                and f.status in {FindingStatus.UNKNOWN, FindingStatus.CONFLICTED}
            )

        def re_extract() -> None:
            nonlocal findings, progress
            required = required_recovery_keys()
            optional = important_gaps()
            quote_keys = {f.key for f in candidates if "quote guard:" in f.notes}
            if progress.re_extraction_done or not quote_keys & (required | optional):
                return
            mandatory = bool(quote_keys & required)
            if (mandatory and time_fits()) or (not mandatory and optional_fits()):
                attempts.append(ResearchAttempt("re_extract"))
                findings = extract(optional=not mandatory)
            else:
                attempts.append(ResearchAttempt("re_extract", error="review time reserved"))
            progress = replace(progress, re_extraction_done=True)

        re_extract()
        required = required_recovery_keys()
        optional = important_gaps()
        # A single search serves the required gaps first and optional gaps only if time fits.
        optional_allowed = bool(optional) and optional_fits()
        keys = required | (optional if optional_allowed else frozenset())
        if keys and time_fits():
            before = len(sources)
            discover(True, keys)
            fetch_pages(optional=not bool(required))
            if len(sources) > before and (required or optional_fits()):
                findings = extract(optional=not bool(required))
            progress = replace(progress, broadened_search_done=True)
            re_extract()
        elif optional or required:
            attempts.append(ResearchAttempt("broadened_search", error="review time reserved"))
            progress = replace(progress, broadened_search_done=True)

    decision = release_decision(
        findings, sources, tiers=tiers, ticket_context=ticket, packet_plan=packet_plan
    )
    reasons = list(decision.reasons)
    if not pages:
        reasons.insert(0, "no seed URLs")
    pending = resolution_steps(
        findings,
        candidates,
        sources,
        progress,
        tiers=tiers,
        ticket_context=ticket,
        packet_plan=packet_plan,
    )
    return ResearchResult(
        ticket=ticket,
        questions=questions,
        sources=tuple(sources),
        failures=tuple(failures),
        findings=findings,
        decision=ReleaseDecision(ok=not reasons, reasons=tuple(reasons)),
        cost=cost,
        started_at=started,
        finished_at=clock(),
        candidates=tuple(candidates),
        progress=progress,
        pending=pending,
        verified_requirements=verified_requirements,
        customer_position=customer_position,
        packet_plan=packet_plan,
        tier_evaluation=tiers,
        record_version=5,
        attempts=tuple(attempts),
        payment_rules=tuple(payment_rules),
        payment_scope=PaymentScope(
            agency.id, agency.deciding_body, ticket.stage or "first-level review"
        )
        if payment_rules and agency.deciding_body
        else None,
    )
