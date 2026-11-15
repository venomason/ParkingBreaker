from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date

import pytest

from parkingbreaker.case_state import HardStop
from parkingbreaker.research.fakes import (
    FakeExtractor,
    FakeFetcher,
    FakeSearcher,
    html_response,
    redirect_response,
)
from parkingbreaker.research.findings import Finding, FindingStatus
from parkingbreaker.research.ladder import (
    Ask,
    CriticalFact,
    CustomerQuestion,
    Stop,
    StopReason,
)
from parkingbreaker.research.questions import (
    ANSWERABLE_KEYS,
    CustomerPosition,
    FactKey,
    PacketPlan,
)
from parkingbreaker.research.resolution import ResearchAnswers, answer_question
from parkingbreaker.research.run import RecoveryConfig, SeedUrl, run_research
from parkingbreaker.research.search import SearchError, SearchHit, SearchRequest
from parkingbreaker.research.sources import AuthorityClass, FetchError, FetchResponse, SourceRecord
from samples import fixture
from test_research_run import (
    CODE_URL,
    CONTEST_URL,
    SEEDS,
    TEST_AGENCY,
    TICKET,
    TickingClock,
    answers,
    fetcher,
)

RECOVERY = RecoveryConfig(retry_delays=(0.5, 1.5))


@dataclass
class ScriptedFetcher:
    contest_responses: Sequence[FetchResponse | FetchError]
    calls: list[str] = field(default_factory=list)
    contest_calls: int = 0

    def get(self, url: str) -> FetchResponse:
        self.calls.append(url)
        if url == CODE_URL:
            return html_response(url, fixture("sfmta_code.html"))
        index = min(self.contest_calls, len(self.contest_responses) - 1)
        self.contest_calls += 1
        reply = self.contest_responses[index]
        if isinstance(reply, FetchError):
            raise reply
        return reply


def test_transient_fetch_recovers_before_extracting_or_searching() -> None:
    failed = html_response(CONTEST_URL, "unavailable", 503)
    transport = ScriptedFetcher(
        [failed, FetchError("timeout"), html_response(CONTEST_URL, fixture("sfmta_contest.html"))]
    )
    delays: list[float] = []
    search = FakeSearcher(lambda request: [])
    extractor = answers()
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        transport,
        extractor,
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=delays.append,
    )
    assert result.decision.ok
    assert result.pending == ()
    assert result.failures == ()
    assert extractor.calls == 1
    assert transport.contest_calls == 3
    assert delays == [0.5, 1.5]
    assert search.calls == []
    assert [a.stage for a in result.attempts].count("fetch_retry") == 2


def test_definitive_failure_is_not_retried_and_broader_search_runs_once() -> None:
    transport = ScriptedFetcher([html_response(CONTEST_URL, "gone", 404)])
    delays: list[float] = []
    search = FakeSearcher(lambda request: [])
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        transport,
        FakeExtractor(lambda sources: []),
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=delays.append,
    )
    assert transport.contest_calls == 1
    assert delays == []
    assert len(search.calls) == 1
    assert search.calls[0].broadened
    assert result.progress.broadened_search_done
    assert any(
        p.step == Stop(StopReason.MAIL_IN_NOT_CONFIRMED, HardStop.S1) for p in result.pending
    )


def test_retry_budget_is_finite_when_the_provider_stays_down() -> None:
    transport = ScriptedFetcher([html_response(CONTEST_URL, "unavailable", 503)])
    delays: list[float] = []
    search = FakeSearcher(lambda request: [SearchHit(CONTEST_URL)])
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        transport,
        FakeExtractor(lambda sources: []),
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=delays.append,
    )
    assert transport.contest_calls == 1 + len(RECOVERY.retry_delays)
    assert len(search.calls) == 1
    assert len(result.failures) == 1
    assert not result.decision.ok


def test_initial_search_finds_pages_without_registry_seed_urls() -> None:
    waits: list[float] = []
    search = FakeSearcher(lambda request: [SearchHit(CONTEST_URL), SearchHit(CODE_URL)])
    transport = fetcher()
    result = run_research(
        TICKET,
        TEST_AGENCY,
        (),
        transport,
        answers(),
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=waits.append,
    )
    assert result.decision.ok
    assert len(search.calls) == 1
    assert not search.calls[0].broadened
    assert transport.calls == [CONTEST_URL, CODE_URL]


def test_broader_search_recovers_missing_required_fact() -> None:
    waits: list[float] = []
    additional = "https://sfmta.com/requirements"
    transport = fetcher()
    transport.responses = {
        **transport.responses,
        additional: html_response(
            additional,
            "<h1>San Francisco Parking Citation Initial Review</h1>"
            "<p>Include your citation number.</p>",
        ),
    }
    search = FakeSearcher(lambda request: [SearchHit(additional)])

    def produce(sources: Sequence[SourceRecord]) -> list[Finding]:
        found = [f for f in answers().produce(sources) if f.key is not FactKey.REQUIRED_ITEMS]
        extra = next((s for s in sources if s.url == additional), None)
        if extra:
            found.append(
                Finding(
                    FactKey.REQUIRED_ITEMS,
                    ["citation number"],
                    FindingStatus.CONFIRMED,
                    "Include your citation number",
                    (extra.sha256,),
                )
            )
        return found

    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        transport,
        FakeExtractor(produce),
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=waits.append,
        packet_plan=PacketPlan(fulfilled_items=frozenset({"citation number"})),
    )
    assert result.decision.ok
    assert len(search.calls) == 1
    assert search.calls[0].keys == frozenset({FactKey.REQUIRED_ITEMS})
    assert additional in transport.calls


def test_quote_failure_gets_one_re_extraction_without_unneeded_search() -> None:
    waits: list[float] = []
    calls = 0

    def produce(sources: Sequence[SourceRecord]) -> list[Finding]:
        nonlocal calls
        calls += 1
        return [
            replace(f, verbatim_quote="invented deadline sentence")
            if calls == 1 and f.key is FactKey.DEADLINE_DAYS
            else f
            for f in answers().produce(sources)
        ]

    extractor = FakeExtractor(produce)
    search = FakeSearcher(lambda request: [])
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        extractor,
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=waits.append,
    )
    assert result.decision.ok
    assert result.progress.re_extraction_done
    assert extractor.calls == 2
    assert result.cost == extractor.usage + extractor.usage
    assert search.calls == []


def test_re_extraction_does_not_erase_verified_conflicting_evidence() -> None:
    waits: list[float] = []
    calls = 0

    def produce(sources: Sequence[SourceRecord]) -> list[Finding]:
        nonlocal calls
        calls += 1
        found = answers().produce(sources)
        if calls == 1:
            found = [
                replace(f, verbatim_quote="invented declaration sentence")
                if f.key is FactKey.REQUIRED_DECLARATIONS
                else f
                for f in found
            ]
        else:
            found = [f for f in found if f.key is not FactKey.DEADLINE_DAYS]
        return found

    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        FakeExtractor(produce),
        TickingClock(),
        recovery=RECOVERY,
        wait=waits.append,
    )
    assert result.decision.ok
    assert next(f for f in result.findings if f.key is FactKey.DEADLINE_DAYS).value == 21


def test_exhausted_gaps_use_the_existing_ladder_without_faking_official_findings() -> None:
    waits: list[float] = []

    def produce(sources: Sequence[SourceRecord]) -> list[Finding]:
        return [
            f
            for f in answers().produce(sources)
            if f.key
            not in {
                FactKey.DEADLINE_DAYS,
                FactKey.DEADLINE_RULE,
            }
        ]

    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        FakeExtractor(produce),
        TickingClock(),
        recovery=RECOVERY,
        wait=waits.append,
    )
    steps = {p.fact: p.step for p in result.pending}
    assert isinstance(steps[CriticalFact.DEADLINE], Ask)
    assert steps[CriticalFact.COUNTING_RULE] == Stop(
        StopReason.DEADLINE_NOT_ESTABLISHED, HardStop.S4
    )
    assert not result.decision.ok
    assert (
        next(f for f in result.findings if f.key is FactKey.DEADLINE_RULE).status
        is FindingStatus.UNKNOWN
    )


def test_search_failure_is_recorded_and_never_looped() -> None:
    waits: list[float] = []

    def fail(request: SearchRequest) -> list[SearchHit]:
        raise SearchError("provider offline")

    search = FakeSearcher(fail)
    result = run_research(
        TICKET,
        TEST_AGENCY,
        (),
        fetcher(),
        FakeExtractor(lambda sources: []),
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=waits.append,
    )
    assert len(search.calls) == 2  # initial request and the one broader recovery
    assert [a.error for a in result.attempts] == ["provider offline", "provider offline"]
    assert not result.decision.ok


def test_disallowed_discovery_and_redirect_are_never_fetched() -> None:
    waits: list[float] = []
    url = "https://sfmta.com/redirect"
    transport = fetcher()
    transport.responses = {**transport.responses, url: redirect_response(url, "https://evil.com/")}
    search = FakeSearcher(lambda request: [SearchHit("https://evil.com/"), SearchHit(url)])
    result = run_research(
        TICKET,
        TEST_AGENCY,
        (),
        transport,
        FakeExtractor(lambda sources: []),
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=waits.append,
    )
    assert transport.calls == [url]
    assert not result.decision.ok
    assert not any(a.stage == "fetch_retry" for a in result.attempts)


@pytest.mark.parametrize("delays", [(-1.0,), (float("nan"),), (float("inf"),), (11.0,), (0.0,) * 4])
def test_retry_budget_rejects_unbounded_or_invalid_delays(delays: tuple[float, ...]) -> None:
    with pytest.raises(ValueError, match="retry delays"):
        RecoveryConfig(retry_delays=delays)


@pytest.mark.parametrize("limit", [0, 21])
def test_page_budget_must_be_bounded(limit: int) -> None:
    with pytest.raises(ValueError, match="max_pages"):
        RecoveryConfig(max_pages=limit)


def test_oversized_seed_packet_is_rejected_before_fetching() -> None:
    waits: list[float] = []
    transport = fetcher()
    seeds = [
        SeedUrl(f"https://sfmta.com/{i}", AuthorityClass.OFFICIAL_PROCEDURE, frozenset())
        for i in range(21)
    ]
    with pytest.raises(ValueError, match="seed pages"):
        run_research(
            TICKET,
            TEST_AGENCY,
            seeds,
            transport,
            answers(),
            TickingClock(),
            recovery=RECOVERY,
            wait=waits.append,
        )
    assert transport.calls == []


def test_persistent_quote_failure_cannot_spend_another_extraction_on_empty_search() -> None:
    waits: list[float] = []

    def produce(sources: Sequence[SourceRecord]) -> list[Finding]:
        return [
            replace(f, verbatim_quote="invented deadline sentence")
            if f.key is FactKey.DEADLINE_DAYS
            else f
            for f in answers().produce(sources)
        ]

    extractor = FakeExtractor(produce)
    search = FakeSearcher(lambda request: [])
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        extractor,
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=waits.append,
    )
    assert not result.decision.ok
    assert extractor.calls == 2
    assert len(search.calls) == 1
    assert result.progress.re_extraction_done
    assert result.progress.broadened_search_done


def test_verified_disagreement_discovered_later_cannot_replace_the_original_value() -> None:
    waits: list[float] = []
    additional = "https://sfmta.com/new-deadline"
    transport = fetcher()
    transport.responses = {
        **transport.responses,
        additional: html_response(
            additional, "<p>Contests must arrive within 22 days of issue.</p>"
        ),
    }
    search = FakeSearcher(lambda request: [SearchHit(additional)])

    def produce(sources: Sequence[SourceRecord]) -> list[Finding]:
        extra = next((s for s in sources if s.url == additional), None)
        found = answers().produce(sources)
        if extra is None:
            return [f for f in found if f.key is not FactKey.REQUIRED_ITEMS]
        return [f for f in found if f.key is not FactKey.DEADLINE_DAYS] + [
            Finding(
                FactKey.DEADLINE_DAYS,
                22,
                FindingStatus.CONFIRMED,
                "must arrive within 22 days",
                (extra.sha256,),
            )
        ]

    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        transport,
        FakeExtractor(produce),
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=waits.append,
    )
    assert not result.decision.ok
    assert (
        next(f for f in result.findings if f.key is FactKey.DEADLINE_DAYS).status
        is FindingStatus.CONFLICTED
    )
    assert any(p.fact is CriticalFact.DEADLINE and isinstance(p.step, Ask) for p in result.pending)


def test_customer_resolution_reuses_saved_sources_without_new_provider_calls() -> None:
    waits: list[float] = []

    def produce(sources: Sequence[SourceRecord]) -> list[Finding]:
        return [f for f in answers().produce(sources) if f.key is not FactKey.DEADLINE_DAYS]

    extractor = FakeExtractor(produce)
    search = FakeSearcher(lambda request: [])
    transport = fetcher()
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        transport,
        extractor,
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=waits.append,
    )
    before = (
        extractor.calls,
        len(search.calls),
        tuple(transport.calls),
        result.findings,
        result.cost,
    )
    customer = answer_question(
        ResearchAnswers(), CustomerQuestion.NOTICE_PRINTED_DEADLINE, date(2026, 10, 20)
    )
    first = result.resolve(customer)
    second = result.resolve(customer)
    assert first == second
    assert first.ready
    assert (
        extractor.calls,
        len(search.calls),
        tuple(transport.calls),
        result.findings,
        result.cost,
    ) == before


def test_verified_notice_clock_asks_customer_without_restarting_recovery() -> None:
    waits: list[float] = []
    page = fixture("sfmta_contest.html").replace("citation issue date.", "notice date.")
    transport = FakeFetcher({**fetcher().responses, CONTEST_URL: html_response(CONTEST_URL, page)})

    def produce(sources: Sequence[SourceRecord]) -> list[Finding]:
        found = answers().produce(sources)
        return [
            replace(f, value="NOTICE_DATE", verbatim_quote="of the notice date")
            if f.key is FactKey.DEADLINE_CLOCK_START
            # A duration alone does not establish that this is a contest deadline.
            else replace(f, verbatim_quote="within 21 calendar days of the notice date")
            if f.key is FactKey.DEADLINE_DAYS
            else replace(f, verbatim_quote="within 21 calendar days of the notice date")
            if f.key is FactKey.DEADLINE_ABSOLUTE
            else f
            for f in found
        ]

    extractor = FakeExtractor(produce)
    search = FakeSearcher(lambda request: [])
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        transport,
        extractor,
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=waits.append,
    )
    assert not result.decision.ok
    assert result.decision.reasons == ("DEADLINE_CLOCK_START: customer notice date missing",)
    assert not result.progress.broadened_search_done
    assert result.resolve().questions == (Ask(CustomerQuestion.NOTICE_ISSUE_DATE),)
    customer = answer_question(
        ResearchAnswers(), CustomerQuestion.NOTICE_ISSUE_DATE, date(2026, 10, 5)
    )
    resolved = result.resolve(customer)
    assert resolved.ready
    assert resolved.deadline is not None
    assert resolved.deadline.deadline == date(2026, 10, 26)
    assert search.calls == []
    assert extractor.calls == 1
    assert len(transport.calls) == len(SEEDS)


@pytest.mark.parametrize("status", [FindingStatus.UNKNOWN, FindingStatus.CONFLICTED])
def test_tier2_gap_recovers_once_is_recorded_and_never_blocks(status: FindingStatus) -> None:
    from parkingbreaker.research_records import decode_research, research_json

    extractor = FakeExtractor(
        lambda sources: [
            replace(f, status=status, value=None, evidence=())
            if f.key is FactKey.GOVERNING_CODE_SECTION
            else f
            for f in answers().produce(sources)
        ]
    )
    search = FakeSearcher(lambda request: [])
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        extractor,
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=lambda _: None,
    )
    assert len(search.calls) == 1
    assert search.calls[0].keys == frozenset({FactKey.GOVERNING_CODE_SECTION})
    assert extractor.calls == 1
    assert result.decision.ok
    assert result.resolve().ready
    assert result.pending == ()
    saved = decode_research(research_json(result))
    assert (
        next(f for f in saved.findings if f.key is FactKey.GOVERNING_CODE_SECTION).status is status
    )
    assert saved.progress.broadened_search_done
    assert saved.resolve().ready


def test_tier3_only_quote_gap_has_no_dedicated_recovery() -> None:
    extractor = FakeExtractor(
        lambda sources: [
            replace(f, verbatim_quote="unsupported claim") if f.key is FactKey.FINE_AMOUNT else f
            for f in answers().produce(sources)
        ]
    )
    search = FakeSearcher(lambda request: [])
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        extractor,
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=lambda _: None,
    )
    assert result.decision.ok
    assert result.resolve().ready
    assert extractor.calls == 1
    assert search.calls == []
    assert not result.progress.re_extraction_done
    assert not result.progress.broadened_search_done


def test_tier2_uses_remaining_re_extraction_and_ai_budget_only() -> None:
    extractor = FakeExtractor(
        lambda sources: [
            replace(f, verbatim_quote="unsupported claim")
            if f.key is FactKey.GOVERNING_CODE_SECTION
            else f
            for f in answers().produce(sources)
        ],
        usage=answers().usage,
    )
    search = FakeSearcher(lambda request: [])
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        extractor,
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=lambda _: None,
        max_cost_cents=3,
    )
    assert extractor.calls == 1  # the retry cannot renew the spent AI allowance
    assert result.cost.cost_cents == 3
    assert result.progress.re_extraction_done
    assert result.progress.broadened_search_done
    assert len(search.calls) == 1
    assert result.resolve().ready
    assert any(a.error == "budget exhausted" for a in result.attempts)


def test_required_and_tier2_gaps_share_one_search_with_no_second_budget() -> None:
    extractor = FakeExtractor(
        lambda sources: [
            f
            for f in answers().produce(sources)
            if f.key not in {FactKey.DEADLINE_DAYS, FactKey.GOVERNING_CODE_SECTION}
        ]
    )
    search = FakeSearcher(lambda request: [])
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        extractor,
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=lambda _: None,
    )
    assert len(search.calls) == 1
    assert search.calls[0].keys == frozenset(
        {FactKey.DEADLINE_DAYS, FactKey.GOVERNING_CODE_SECTION}
    )
    assert result.resolve().questions == (Ask(CustomerQuestion.NOTICE_ISSUE_DATE),)
    assert not result.resolve().ready
    resolved = result.resolve(ResearchAnswers(printed_deadline=date(2026, 10, 20)))
    assert resolved.ready
    assert len(search.calls) == 1
    assert extractor.calls == 1


@pytest.mark.parametrize("elapsed_minutes", [25, 30])
def test_optional_recovery_at_review_reserve_or_window_limit_does_not_delay_adoption(
    elapsed_minutes: int,
) -> None:
    from datetime import datetime, timedelta

    from test_research_records import result_fixture
    from test_research_run import CONTEST_SEED

    baseline = result_fixture()

    @dataclass
    class FakeClock:
        now: datetime = baseline.started_at

        def __call__(self) -> datetime:
            return self.now

    clock = FakeClock()

    def produce(sources: Sequence[SourceRecord]) -> list[Finding]:
        clock.now += timedelta(minutes=elapsed_minutes)
        return [f for f in baseline.findings if f.key is not FactKey.GOVERNING_CODE_SECTION]

    extractor = FakeExtractor(produce)
    search = FakeSearcher(lambda request: [])
    delays: list[float] = []
    result = run_research(
        TICKET,
        TEST_AGENCY,
        [CONTEST_SEED],
        fetcher(),
        extractor,
        clock,
        searcher=search,
        recovery=RECOVERY,
        wait=delays.append,
        remaining_time=timedelta(minutes=30),
    )
    assert result.finished_at == baseline.started_at + timedelta(minutes=elapsed_minutes)
    assert search.calls == []
    assert delays == []
    assert extractor.calls == 1
    assert result.decision.ok
    assert result.resolve().ready
    assert any(a.error == "review time reserved" for a in result.attempts)


def test_tier2_transient_fetch_does_not_retry_inside_review_reserve() -> None:
    from datetime import datetime, timedelta

    from test_research_records import result_fixture

    baseline = result_fixture()

    @dataclass
    class FakeClock:
        now: datetime = baseline.started_at

        def __call__(self) -> datetime:
            return self.now

    clock = FakeClock()

    @dataclass
    class Transport:
        calls: int = 0

        def get(self, url: str) -> FetchResponse:
            if url == CODE_URL:
                self.calls += 1
                clock.now += timedelta(minutes=25)
                return html_response(url, "", 503)
            return html_response(url, fixture("sfmta_contest.html"))

    transport = Transport()
    delays: list[float] = []
    search = FakeSearcher(lambda request: [])
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        transport,
        FakeExtractor(lambda _: baseline.findings),
        clock,
        searcher=search,
        recovery=RECOVERY,
        wait=delays.append,
    )
    assert transport.calls == 1
    assert delays == []
    assert search.calls == []
    assert result.resolve().ready
    assert any(
        a.stage == "fetch_retry" and a.error == "review time reserved" for a in result.attempts
    )


def test_retired_customer_citation_cannot_promote_a_recovery_gap() -> None:
    """Audit A016/A017: a customer defence/citation selection is inert, not a research key."""
    extractor = FakeExtractor(
        lambda sources: [
            replace(f, verbatim_quote="unsupported claim")
            if f.key is FactKey.GOVERNING_CODE_SECTION
            else f
            for f in answers().produce(sources)
        ]
    )
    search = FakeSearcher(lambda request: [])
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        extractor,
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=lambda _: None,
        customer_position=CustomerPosition(code_citation="SEC. 7.2.22"),
    )
    assert result.tier_evaluation is not None
    assert all(p.key is not FactKey.CODE_EXCEPTIONS for p in result.tier_evaluation.promotions)
    # Optional recovery may still chase the procedural code provision, but never the
    # retired defence key the customer selected long ago.
    assert all(FactKey.CODE_EXCEPTIONS not in request.keys for request in search.calls)
    assert result.resolve().ready


@pytest.mark.parametrize("failure", ["search", "extraction"])
def test_required_provider_failure_stops_not_yet_verified_never_unsupported(failure: str) -> None:
    from parkingbreaker import case_state
    from parkingbreaker.research.resolution import resolution_event

    def failed_search(request: SearchRequest) -> list[SearchHit]:
        raise SearchError("offline")

    extractor = FakeExtractor(
        lambda sources: (
            []
            if failure == "extraction"
            else [f for f in answers().produce(sources) if f.key is not FactKey.MAIL_IN_ACCEPTED]
        )
    )
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        extractor,
        TickingClock(),
        searcher=FakeSearcher(failed_search),
        recovery=RECOVERY,
        wait=lambda _: None,
    )
    event = resolution_event(result.resolve(ResearchAnswers(answered=frozenset(CustomerQuestion))))
    assert isinstance(event, case_state.Stopped)
    assert event.reason is case_state.StopReason.NOT_YET_VERIFIED


def test_expired_caller_window_runs_no_providers_and_never_renews_budget() -> None:
    from datetime import timedelta

    transport = fetcher()
    search = FakeSearcher(lambda request: [])
    extractor = FakeExtractor(lambda sources: [])
    result = run_research(
        TICKET,
        TEST_AGENCY,
        (),
        transport,
        extractor,
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=lambda _: None,
        remaining_time=timedelta(0),
    )
    assert transport.calls == []
    assert search.calls == []
    assert extractor.calls == 0
    assert not result.decision.ok
    assert not result.resolve().ready
    assert len(result.findings) == len(ANSWERABLE_KEYS)


def test_optional_extractor_failure_keeps_verified_tier1_and_ends_ai_recovery() -> None:
    from parkingbreaker.llm import LlmProviderError

    calls = 0

    def produce(sources: Sequence[SourceRecord]) -> list[Finding]:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise LlmProviderError("provider offline", retryable=True)
        return [
            replace(f, verbatim_quote="unsupported claim")
            if f.key is FactKey.GOVERNING_CODE_SECTION
            else f
            for f in answers().produce(sources)
        ]

    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        FakeExtractor(produce),
        TickingClock(),
        searcher=FakeSearcher(lambda request: []),
        recovery=RECOVERY,
        wait=lambda _: None,
    )
    assert calls == 2
    assert result.decision.ok
    assert result.resolve().ready
    assert (
        next(f for f in result.findings if f.key is FactKey.GOVERNING_CODE_SECTION).status
        is FindingStatus.UNKNOWN
    )
    assert any(a.error == "provider offline" for a in result.attempts)


def test_tier2_quote_recovery_can_confirm_without_spending_broader_search() -> None:
    calls = 0

    def produce(sources: Sequence[SourceRecord]) -> list[Finding]:
        nonlocal calls
        calls += 1
        return [
            replace(f, verbatim_quote="unsupported claim")
            if calls == 1 and f.key is FactKey.GOVERNING_CODE_SECTION
            else f
            for f in answers().produce(sources)
        ]

    search = FakeSearcher(lambda request: [])
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        FakeExtractor(produce),
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=lambda _: None,
    )
    assert calls == 2
    assert result.resolve().ready
    assert search.calls == []
    assert (
        next(f for f in result.findings if f.key is FactKey.GOVERNING_CODE_SECTION).status
        is FindingStatus.CONFIRMED
    )
