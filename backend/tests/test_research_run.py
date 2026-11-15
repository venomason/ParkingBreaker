from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import cast

import pytest

from parkingbreaker.agencies import SourceScope
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import LlmBudgetExceeded, Usage
from parkingbreaker.research.extractor import LlmExtractor
from parkingbreaker.research.fakes import (
    FakeExtractor,
    FakeFetcher,
    FakeLlmClient,
    FakeSearcher,
    html_response,
    redirect_response,
)
from parkingbreaker.research.findings import Finding, FindingStatus
from parkingbreaker.research.procedure import assessment_data
from parkingbreaker.research.questions import (
    CRITICAL_KEYS,
    RETIRED_KEYS,
    RULE_KEYS,
    FactKey,
    Ticket,
)
from parkingbreaker.research.run import (
    RecoveryConfig,
    SeedUrl,
    run_research,
)
from parkingbreaker.research.search import SHARED_CODE_DOMAINS, SearchHit
from parkingbreaker.research.sources import CODE_PUBLISHER_DOMAINS, AuthorityClass, SourceRecord
from samples import RETRIEVED_AT, SAN_FRANCISCO, fixture
from test_research_findings import silence_finding

K = FactKey
CONTEST_URL = "https://www.sfmta.com/getting-around/drive-park/citations/contest-citation"
CONTEST_SEED = SeedUrl(
    CONTEST_URL,
    AuthorityClass.OFFICIAL_PROCEDURE,
    frozenset({K.DEADLINE_DAYS, K.DEADLINE_CLOCK_START, K.MAILING_ADDRESS}),
)
# Test-only seed for the synthetic code fixture; not a real registry URL.
CODE_URL = "https://codes.fixture-city.gov/transportation/7.2.22"
TICKET = Ticket("us-ca-san_francisco", "TC 7.2.22", "Street cleaning", date(2026, 10, 1))

TEST_AGENCY = dataclasses.replace(SAN_FRANCISCO, official_domains=("sfmta.com", "fixture-city.gov"))
SEEDS = (
    CONTEST_SEED,
    SeedUrl(CODE_URL, AuthorityClass.STATUTE, frozenset({K.GOVERNING_CODE_SECTION})),
)

ADDRESS: dict[str, JsonValue] = {
    "recipient": "SFMTA Customer Service Center",
    "attention": "Citation Review",
    "line1": "11 South Van Ness Avenue",
    "line2": None,
    "city": "San Francisco",
    "state": "CA",
    "zip_code": "94103",
}


def fetcher(contest: str = "sfmta_contest.html") -> FakeFetcher:
    return FakeFetcher(
        {
            CONTEST_URL: html_response(CONTEST_URL, fixture(contest)),
            CODE_URL: html_response(CODE_URL, fixture("sfmta_code.html")),
        }
    )


class TickingClock:
    def __init__(self) -> None:
        self.ticks = 0

    def __call__(self) -> datetime:
        self.ticks += 1
        return RETRIEVED_AT + timedelta(seconds=self.ticks)


def sha_of(sources: Sequence[SourceRecord], url: str) -> str:
    return next(s.sha256 for s in sources if s.url == url)


def answers(address: dict[str, JsonValue] = ADDRESS, deadline: int = 21) -> FakeExtractor:
    def produce(sources: Sequence[SourceRecord]) -> list[Finding]:
        c, s = sha_of(sources, CONTEST_URL), sha_of(sources, CODE_URL)

        def ok(key: FactKey, value: JsonValue, quote: str, sha: str) -> Finding:
            return Finding(key, value, FindingStatus.CONFIRMED, quote, (sha,))

        return [
            ok(K.MAIL_IN_ACCEPTED, True, "Contests sent by mail must be received", c),
            ok(K.GOVERNING_CODE_SECTION, "SEC. 7.2.22", "SEC. 7.2.22. STREET CLEANING.", s),
            ok(K.DEADLINE_DAYS, deadline, "within 21 calendar days of the citation", c),
            ok(K.DEADLINE_CLOCK_START, "ISSUE_DATE", "of the citation issue date", c),
            ok(K.DEADLINE_RULE, "RECEIPT", "must be received by the SFMTA by the deadline", c),
            ok(
                K.MAILING_ADDRESS,
                address,
                "SFMTA Customer Service Center ATTN: Citation Review "
                "11 South Van Ness Avenue San Francisco, CA 94103",
                c,
            ),
            ok(K.REQUIRED_ITEMS, ["citation number"], "Include your citation number", c),
            ok(K.REQUIRED_DECLARATIONS, [], "A signed declaration is not required", c),
            *[
                silence_finding(key, next(page for page in sources if page.sha256 == c))
                for key in RULE_KEYS
            ],
            ok(
                K.DEADLINE_ABSOLUTE,
                {"applicable": False, "date": None},
                "within 21 calendar days of the citation issue date",
                c,
            ),
            ok(K.FINE_AMOUNT, 90, "The fine for a violation of this Section is $90.", s),
            ok(
                K.CODE_EXCEPTIONS,
                ["a vehicle stopped to load or unload passengers"],
                "shall not apply to a vehicle stopped to load or unload passengers",
                s,
            ),
        ]

    return FakeExtractor(produce, usage=Usage(5000, 800, 3))


def test_full_research_releases() -> None:
    waits: list[float] = []
    fake_fetcher = fetcher()
    clock = TickingClock()
    result = run_research(
        TICKET, TEST_AGENCY, SEEDS, fake_fetcher, answers(), clock, wait=waits.append
    )
    assert result.decision == (True, ())
    assert [s.url for s in result.sources] == [CONTEST_URL, CODE_URL]
    assert result.failures == ()
    assert {f.key for f in result.findings} == set(FactKey) - RETIRED_KEYS
    assert all(f.status is FindingStatus.CONFIRMED for f in result.findings)
    assert result.cost == Usage(5000, 800, 3)
    assert result.started_at < result.sources[0].retrieved_at < result.finished_at
    assert fake_fetcher.calls == [CONTEST_URL, CODE_URL]
    assert [q.key for q in result.questions] == [k for k in FactKey if k not in RETIRED_KEYS]


def test_extraction_budget_refusal_is_recorded_without_findings_or_spend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
    ) -> tuple[JsonValue, Usage]:
        raise LlmBudgetExceeded("no request was made")

    client = FakeLlmClient({})
    monkeypatch.setattr(client, "complete_json", refuse)
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        LlmExtractor(client),
        TickingClock(),
        wait=lambda _: None,
    )
    assert len(result.findings) == len(FactKey) - len(RETIRED_KEYS)
    assert all(f.status is FindingStatus.UNKNOWN and f.value is None for f in result.findings)
    assert result.cost == Usage()
    assert not result.decision.ok
    assert result.attempts[-1].stage == "extract"
    assert result.attempts[-1].error == "budget exhausted"
    assert client.calls == []


def test_fresh_contest_destination_needs_no_remembered_address() -> None:
    result = run_research(
        TICKET, TEST_AGENCY, SEEDS, fetcher(), answers(), TickingClock(), wait=lambda _: None
    )
    assert result.decision.ok
    assert not result.address_mismatch
    assert not hasattr(TEST_AGENCY, "appeal_address")


def test_address_still_requires_official_delivery_point_and_contest_purpose() -> None:
    for field, value in [
        ("line1", "1 Invented Street"),
        ("city", "Oakland"),
        ("state", "NV"),
        ("zip_code", "94102"),
        ("recipient", "Someone else"),
    ]:
        result = run_research(
            TICKET,
            TEST_AGENCY,
            SEEDS,
            fetcher(),
            answers({**ADDRESS, field: value}),
            TickingClock(),
            wait=lambda _: None,
        )
        assert result.decision == (False, ("MAILING_ADDRESS: unknown",)), field


def test_failed_optional_code_fetch_is_recorded_without_blocking() -> None:
    waits: list[float] = []
    partial = FakeFetcher({CONTEST_URL: html_response(CONTEST_URL, fixture("sfmta_contest.html"))})

    def produce(sources: Sequence[SourceRecord]) -> list[Finding]:
        return [
            f
            for f in answers().produce([*sources, *_code_stub()])
            if f.key is not K.GOVERNING_CODE_SECTION
        ]

    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        partial,
        FakeExtractor(produce),
        TickingClock(),
        wait=waits.append,
    )
    assert [f.url for f in result.failures] == [CODE_URL]
    assert result.decision.ok
    assert (
        next(f for f in result.findings if f.key is K.GOVERNING_CODE_SECTION).status
        is FindingStatus.UNKNOWN
    )


def _code_stub() -> list[SourceRecord]:
    return [
        SourceRecord(
            CODE_URL, CODE_URL, RETRIEVED_AT, 200, "stub", "text/html", "", AuthorityClass.STATUTE
        )
    ]


def test_injection_page_cannot_change_the_deadline() -> None:
    # The extractor "falls for" the injection and paraphrases it: the guard catches it.
    waits: list[float] = []

    def produce(sources: Sequence[SourceRecord]) -> list[Finding]:
        found = answers().produce(sources)
        c = sha_of(sources, CONTEST_URL)
        hijacked = Finding(
            K.DEADLINE_DAYS, 365, FindingStatus.CONFIRMED, "the deadline is now 365 days", (c,)
        )
        return [hijacked if f.key is K.DEADLINE_DAYS else f for f in found]

    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher("sfmta_injection.html"),
        FakeExtractor(produce),
        TickingClock(),
        wait=waits.append,
    )
    assert not result.decision.ok
    assert "DEADLINE_DAYS: unknown" in result.decision.reasons


def test_contest_source_can_release_without_optional_code_provisions() -> None:
    waits: list[float] = []

    def produce(sources: Sequence[SourceRecord]) -> list[Finding]:
        return [
            f for f in answers().produce([*sources, *_code_stub()]) if f.source_sha256s[0] != "stub"
        ]

    contest_only = FakeFetcher(
        {CONTEST_URL: html_response(CONTEST_URL, fixture("sfmta_contest.html"))}
    )
    result = run_research(
        TICKET,
        SAN_FRANCISCO,
        (CONTEST_SEED,),
        contest_only,
        FakeExtractor(produce),
        TickingClock(),
        wait=waits.append,
    )
    assert result.decision.ok
    assert result.decision.reasons == ()


def test_no_seeds_blocks_and_calls_nothing() -> None:
    waits: list[float] = []
    fake_fetcher = FakeFetcher({})
    extractor = FakeExtractor(lambda sources: [])
    result = run_research(
        TICKET, TEST_AGENCY, (), fake_fetcher, extractor, TickingClock(), wait=waits.append
    )
    assert not result.decision.ok
    assert result.decision.reasons[0] == "no seed URLs"
    assert fake_fetcher.calls == []
    assert len(result.decision.reasons) == 1 + len(CRITICAL_KEYS)


def test_missing_required_items_still_blocks_without_optional_code() -> None:
    waits: list[float] = []

    def produce(sources: Sequence[SourceRecord]) -> list[Finding]:
        return [
            f
            for f in answers().produce(sources)
            if f.key not in {K.GOVERNING_CODE_SECTION, K.REQUIRED_ITEMS}
        ]

    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        FakeExtractor(produce),
        TickingClock(),
        wait=waits.append,
    )
    assert not result.decision.ok
    assert result.decision.reasons == ("REQUIRED_ITEMS: missing",)


def test_resolved_agency_cannot_release_without_fresh_mail_in_acceptance() -> None:
    waits: list[float] = []

    def produce(sources: Sequence[SourceRecord]) -> list[Finding]:
        return [f for f in answers().produce(sources) if f.key is not K.MAIL_IN_ACCEPTED]

    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        FakeExtractor(produce),
        TickingClock(),
        wait=waits.append,
    )
    assert result.decision == (False, ("MAIL_IN_ACCEPTED: missing",))


def test_model_cannot_waive_all_critical_facts_with_notes() -> None:
    waits: list[float] = []
    extractor = FakeExtractor(
        lambda sources: [
            Finding(key, None, FindingStatus.NOT_APPLICABLE, None, (), "nothing needed")
            for key in CRITICAL_KEYS
        ]
    )
    result = run_research(
        TICKET, TEST_AGENCY, SEEDS, fetcher(), extractor, TickingClock(), wait=waits.append
    )
    assert not result.decision.ok
    assert len(result.decision.reasons) == len(CRITICAL_KEYS)
    assert all(f.status is FindingStatus.UNKNOWN for f in result.findings)


def test_resolved_agency_cannot_hide_a_fabricated_research_address() -> None:
    waits: list[float] = []
    fabricated = {**ADDRESS, "line1": "11 Invented Street"}
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        answers(fabricated),
        TickingClock(),
        wait=waits.append,
    )
    assert result.decision == (False, ("MAILING_ADDRESS: unknown",))
    finding = next(f for f in result.findings if f.key is K.MAILING_ADDRESS)
    assert finding.status is FindingStatus.UNKNOWN
    assert "value not in quote" in finding.notes


def test_new_official_contest_destination_can_release_without_activation() -> None:
    fresh = {**ADDRESS, "line1": "22 Main Street", "city": "Oakland", "zip_code": "94612"}
    contest = fixture("sfmta_contest.html").replace(
        "11 South Van Ness Avenue<br>\n      San Francisco, CA 94103",
        "22 Main Street<br>\n      Oakland, CA 94612",
    )
    contest = contest.replace(
        "<h1>Contest a Citation</h1>", "<h1>San Francisco: Contest a Citation</h1>"
    )
    fake_fetcher = FakeFetcher(
        {
            CONTEST_URL: html_response(CONTEST_URL, contest),
            CODE_URL: html_response(CODE_URL, fixture("sfmta_code.html")),
        }
    )

    def produce(sources: Sequence[SourceRecord]) -> list[Finding]:
        return [
            dataclasses.replace(
                f,
                value=fresh,
                verbatim_quote=(
                    "SFMTA Customer Service Center ATTN: Citation Review "
                    "22 Main Street Oakland, CA 94612"
                ),
            )
            if f.key is K.MAILING_ADDRESS
            else f
            for f in answers().produce(sources)
        ]

    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fake_fetcher,
        FakeExtractor(produce),
        TickingClock(),
        wait=lambda _: None,
    )
    assert result.decision.ok
    address = next(f for f in result.findings if f.key is K.MAILING_ADDRESS)
    assert address.value == fresh


PORTAL = "https://portal.com"
PORTAL_AGENCY = dataclasses.replace(
    SAN_FRANCISCO, delegated_scopes=(SourceScope("portal.com", "/sf"),)
)


@pytest.mark.parametrize("path", ["/sf", "/sf/contest"])
def test_research_fetches_delegated_tenant_seeds(path: str) -> None:
    url = PORTAL + path
    transport = FakeFetcher({url: html_response(url, "<p>Contest procedure</p>")})
    result = run_research(
        TICKET,
        PORTAL_AGENCY,
        (SeedUrl(url, AuthorityClass.OFFICIAL_PROCEDURE, CRITICAL_KEYS),),
        transport,
        FakeExtractor(lambda sources: []),
        TickingClock(),
        wait=lambda _: None,
    )
    assert [source.url for source in result.sources] == [url]
    assert result.failures == ()
    assert transport.calls == [url]


@pytest.mark.parametrize(
    "url",
    [
        PORTAL + "/sfx",
        PORTAL + "/sf-other",
        "https://sub.portal.com/sf",
        PORTAL + "/sf/%2e",
        PORTAL + "/sf/../sf",
        PORTAL + "/sf//x",
        PORTAL + "/sf/./x",
        PORTAL + "/SF",
    ],
)
def test_research_never_fetches_invalid_delegated_seeds(url: str) -> None:
    transport = FakeFetcher({})
    result = run_research(
        TICKET,
        PORTAL_AGENCY,
        (SeedUrl(url, AuthorityClass.OFFICIAL_PROCEDURE, CRITICAL_KEYS),),
        transport,
        FakeExtractor(lambda sources: []),
        TickingClock(),
        wait=lambda _: None,
    )
    assert result.sources == ()
    assert len(result.failures) == 1
    assert "not on the allowlist" in result.failures[0].reason
    assert transport.calls == []


@pytest.mark.parametrize("target", [PORTAL + "/sf", PORTAL + "/sfx"])
def test_research_checks_tenant_scope_on_official_redirect(target: str) -> None:
    transport = FakeFetcher(
        {CONTEST_URL: redirect_response(CONTEST_URL, target), target: html_response(target, "Form")}
    )
    result = run_research(
        TICKET,
        PORTAL_AGENCY,
        (CONTEST_SEED,),
        transport,
        FakeExtractor(lambda sources: []),
        TickingClock(),
        wait=lambda _: None,
    )
    if target.endswith("/sf"):
        assert [source.final_url for source in result.sources] == [target]
        assert result.failures == ()
        assert transport.calls == [CONTEST_URL, target]
    else:
        assert result.sources == ()
        assert "not on the allowlist" in result.failures[0].reason
        assert transport.calls == [CONTEST_URL]


@pytest.mark.parametrize("broadened", [False, True])
def test_discovery_searches_portal_host_but_filters_and_fetches_only_tenant(
    broadened: bool,
) -> None:
    allowed = PORTAL + "/sf/contest"
    sibling = PORTAL + "/sf-other/contest"
    search = FakeSearcher(lambda request: [SearchHit(sibling), SearchHit(allowed)])
    transport = FakeFetcher({allowed: html_response(allowed, "Contest procedure")})
    result = run_research(
        TICKET,
        PORTAL_AGENCY,
        (CONTEST_SEED,) if broadened else (),
        transport,
        FakeExtractor(lambda sources: []),
        TickingClock(),
        searcher=search,
        recovery=RecoveryConfig(retry_delays=()) if broadened else None,
        wait=lambda _: None,
    )
    assert len(search.calls) == 1
    request = search.calls[0]
    assert request.broadened is broadened
    assert request.official_domains == (
        "sfmta.com",
        "portal.com",
        *SHARED_CODE_DOMAINS,
        *CODE_PUBLISHER_DOMAINS,
    )
    assert [source.url for source in result.sources] == [allowed]
    assert sibling not in transport.calls
    assert allowed in transport.calls


class CeilingRecorder:
    """Wraps a fake extractor and records the ceiling each call received."""

    def __init__(self, inner: FakeExtractor) -> None:
        self.inner = inner
        self.ceilings: list[int | None] = []

    def extract(
        self,
        questions: Sequence[object],
        sources: Sequence[SourceRecord],
        *,
        max_cost_cents: int | None = None,
    ) -> object:
        self.ceilings.append(max_cost_cents)
        return self.inner.extract(questions, sources, max_cost_cents=max_cost_cents)  # type: ignore[arg-type]


def test_research_ceiling_is_passed_to_the_extractor() -> None:
    recorder = CeilingRecorder(answers())
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        recorder,  # type: ignore[arg-type]
        TickingClock(),
        wait=lambda _: None,
        max_cost_cents=50,
    )
    assert recorder.ceilings == [50]
    assert result.decision.ok
    assert result.cost.cost_cents <= 50


def test_research_with_spent_ceiling_makes_no_ai_call() -> None:
    extractor = answers()
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        extractor,
        TickingClock(),
        wait=lambda _: None,
        max_cost_cents=0,
    )
    assert extractor.calls == 0
    assert result.cost == Usage()
    assert not result.decision.ok
    assert result.attempts[-1].stage == "extract"
    assert result.attempts[-1].error == "budget exhausted"


def test_research_ceiling_below_call_cost_is_budget_exhausted() -> None:
    extractor = answers()
    assert extractor.usage.cost_cents > 1
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        extractor,
        TickingClock(),
        wait=lambda _: None,
        max_cost_cents=1,
    )
    assert result.cost == Usage()
    assert result.attempts[-1].error == "budget exhausted"


@pytest.mark.parametrize("bad", [-1, 1.5, True])
def test_research_ceiling_rejects_bad_values(bad: object) -> None:
    with pytest.raises(ValueError, match="max_cost_cents"):
        run_research(
            TICKET,
            TEST_AGENCY,
            SEEDS,
            fetcher(),
            answers(),
            TickingClock(),
            wait=lambda _: None,
            max_cost_cents=bad,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("failure", ["no_sources", "invalid_json", "empty", "budget"])
def test_failed_extraction_always_returns_the_complete_guarded_answer_set(failure: str) -> None:
    from parkingbreaker.research.findings import UnknownReason

    extractor = LlmExtractor(FakeLlmClient(None if failure == "invalid_json" else {"findings": []}))
    result = run_research(
        TICKET,
        TEST_AGENCY,
        () if failure == "no_sources" else SEEDS,
        fetcher(),
        extractor,
        TickingClock(),
        wait=lambda _: None,
        max_cost_cents=0 if failure == "budget" else None,
    )
    assert len(result.findings) == len(result.questions) == len(FactKey) - len(RETIRED_KEYS)
    assert [f.key for f in result.findings] == [q.key for q in result.questions]
    assert all(f.status is FindingStatus.UNKNOWN and f.value is None for f in result.findings)
    expected = {
        "no_sources": UnknownReason.NO_SOURCES,
        "invalid_json": UnknownReason.INVALID_JSON,
        "empty": UnknownReason.MISSING_ANSWER,
        "budget": UnknownReason.BUDGET_EXHAUSTED,
    }
    assert all(f.unknown_reason is expected[failure] for f in result.findings)
    assert not result.decision.ok


def test_run_uses_resolved_jurisdiction_and_stage_for_every_question_and_answer() -> None:
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        answers(),
        TickingClock(),
        stage="administrative review",
        wait=lambda _: None,
    )
    assert result.decision.ok
    assert result.ticket.jurisdiction == TEST_AGENCY.id
    assert result.ticket.stage == "administrative review"
    assert all(
        TEST_AGENCY.id in q.ask and "administrative review" in q.ask for q in result.questions
    )
    assert all(f.jurisdiction == TEST_AGENCY.id for f in result.findings)
    assert all(f.stage != "administrative review" for f in result.findings)
    assert (
        next(f for f in result.findings if f.key is K.MAILING_ADDRESS).stage == "first-level review"
    )


def test_duplicate_json_claims_keep_disagreement_and_request_scope() -> None:
    base = fetcher()
    page = fixture("sfmta_contest.html").replace("</main>", "<p>Contest within 30 days.</p></main>")
    transport = FakeFetcher({**base.responses, CONTEST_URL: html_response(CONTEST_URL, page)})

    # Use the real JSON parser, whose consolidation happens before run adds scope.
    from parkingbreaker.research.extractor import ExtractionResult, parse_findings
    from parkingbreaker.research.questions import Question

    class JsonExtractor:
        def extract(
            self,
            questions: Sequence[Question],
            sources: Sequence[SourceRecord],
            *,
            max_cost_cents: int | None = None,
        ) -> ExtractionResult:
            claims: list[JsonValue] = [
                {
                    "key": f.key.value,
                    "status": f.status.value,
                    "value": f.value,
                    "verbatim_quote": f.verbatim_quote,
                    "source_sha256s": list(f.source_sha256s),
                    "evidence_basis": f.evidence_basis,
                    "procedure_assessment": cast(JsonValue, assessment_data(f.procedure_assessment))
                    if f.procedure_assessment is not None
                    else None,
                }
                for f in answers().produce(sources)
            ]
            claims.append(
                {
                    "key": "DEADLINE_DAYS",
                    "status": "CONFIRMED",
                    "value": 30,
                    "verbatim_quote": "Contest within 30 days.",
                    "source_sha256s": [sha_of(sources, CONTEST_URL)],
                }
            )
            return ExtractionResult(parse_findings({"findings": claims}, questions), Usage())

    result = run_research(
        TICKET, TEST_AGENCY, SEEDS, transport, JsonExtractor(), TickingClock(), wait=lambda _: None
    )
    assert len(result.findings) == len(FactKey) - len(RETIRED_KEYS)
    assert not result.decision.ok
    conflict = next(f for f in result.findings if f.key is K.DEADLINE_DAYS)
    assert conflict.status is FindingStatus.CONFLICTED
    assert len(conflict.evidence) == 2
    assert all(
        f.jurisdiction == TEST_AGENCY.id and f.stage == result.ticket.stage
        for f in conflict.evidence
    )
