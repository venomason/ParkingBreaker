from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from parkingbreaker import case_state
from parkingbreaker.bench.cases import (
    KeyRow,
    build_issuer_query,
    build_scripted_answers,
    build_ticket,
    parse_answer_key,
)
from parkingbreaker.bench.runner import (
    CaseRun,
    RecordingFetcher,
    RecordingLlm,
    RecordingSearcher,
    RunDeps,
    run_case,
)
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import LlmBudgetExceeded, LlmProviderError, Usage
from parkingbreaker.oplog.digest import CounterMilestone, ResearchStatus
from parkingbreaker.research.extractor import LlmExtractor
from parkingbreaker.research.fakes import (
    FakeFetcher,
    FakeLlmClient,
    FakeSearcher,
    fixed_clock,
    html_response,
    redirect_response,
)
from parkingbreaker.research.issuer_assessment import LlmIssuerAssessor
from parkingbreaker.research.issuer_resolution import resolve_ticket_issuer
from parkingbreaker.research.jurisdiction import DotGovRecord, DotGovSnapshot
from parkingbreaker.research.link_assessment import LlmLinkAssessor
from parkingbreaker.research.procedure import SILENCE_KEYS, ProcedureAssessment, assessment_data
from parkingbreaker.research.resolution import resolution_event
from parkingbreaker.research.run import RecoveryConfig, run_research
from parkingbreaker.research.search import SearchError, SearchHit, SearchRequest
from parkingbreaker.research.sources import FetchError, html_to_text

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 10, 14)
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
ROOT = "https://sandiego.gov/"
CONTEST = ROOT + "contest"
PAYMENT = "https://payment.example.com/pay"
ISSUER_QUOTE = "City of San Diego is a municipal government in California."
BODY_QUOTE = "City Parking Administration decides initial written reviews."
ANCHOR = "Pay parking citation online"
ROOT_HTML = f'<p>{ISSUER_QUOTE}</p><p>{BODY_QUOTE}</p><p><a href="{PAYMENT}">{ANCHOR}</a></p>'
ADDRESS: dict[str, JsonValue] = {
    "recipient": "Initial Review Desk",
    "attention": None,
    "line1": "P.O. Box 129038",
    "line2": None,
    "city": "San Diego",
    "state": "CA",
    "zip_code": "92112-9038",
}
FACTS: tuple[tuple[str, JsonValue, str], ...] = (
    ("MAIL_IN_ACCEPTED", True, "You may contest your parking citation by mail."),
    ("DEADLINE_DAYS", 14, "Contest within 14 days from the delinquency notice mailing date."),
    ("DEADLINE_CLOCK_START", "NOTICE_DATE", "From the delinquency notice mailing date."),
    ("DEADLINE_RULE", "POSTMARK", "Your mailed contest must be postmarked by the deadline."),
    (
        "MAILING_ADDRESS",
        ADDRESS,
        "Mail your initial review to Initial Review Desk, "
        "P.O. Box 129038, San Diego CA 92112-9038.",
    ),
    (
        "DEADLINE_ABSOLUTE",
        {"applicable": False, "date": None},
        "Contest within 14 days from the delinquency notice mailing date.",
    ),
    ("REQUIRED_ITEMS", ["citation number"], "Include your citation number."),
)
CONTEST_HTML = "<h1>San Diego Parking Citation Initial Review</h1>" + "".join(
    f"<p>{quote}</p>" for _, _, quote in FACTS
)
PROCEDURE_TEXT = html_to_text(CONTEST_HTML)
PROCEDURE_ASSESSMENT = ProcedureAssessment(
    True,
    "CA:sandiego.gov",
    "first-level review",
    "parking citation",
    hashlib.sha256(CONTEST_HTML.encode()).hexdigest(),
    PROCEDURE_TEXT,
    PROCEDURE_TEXT,
    "You may contest your parking citation by mail.",
    "Mail your initial review to Initial Review Desk, P.O. Box 129038, San Diego CA 92112-9038.",
    "Contest within 14 days from the delinquency notice mailing date.",
    "The page gives written mail-contest instructions, a destination, deadline and inventory.",
)


def test_case_run_json_round_trip() -> None:
    text = (FIXTURES / "bench_b16_run.json").read_text()
    run = CaseRun.from_json(text)
    assert run.run_date == TODAY
    assert isinstance(run.snapshot_date, date)
    assert isinstance(run.ai_calls[0].usage, Usage)
    assert isinstance(run.fetches, tuple)
    assert isinstance(run.findings, tuple)
    assert json.loads(run.to_json()) == json.loads(text)
    assert CaseRun.from_json(run.to_json()) == run
    envelope = {**json.loads(text), "run_id": "test:B16:1", "repeat_index": 1, "fake": True}
    assert CaseRun.from_json(json.dumps(envelope)) == run


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_date", "invalid-date"),
        ("findings", {}),
        ("ai_calls", [{"phase": "invalid"}]),
        ("brave_requests", True),
        ("cost_cents", {"research": "10"}),
        ("wall_seconds", {"research": float("nan")}),
        ("unexpected", "field"),
    ],
)
def test_case_run_json_rejects_invalid_fields(field: str, value: JsonValue) -> None:
    raw = json.loads((FIXTURES / "bench_b16_run.json").read_text())
    raw[field] = value
    with pytest.raises(ValueError, match=field):
        CaseRun.from_json(json.dumps(raw))


class Timer:
    def __init__(self) -> None:
        self.seconds = -1.0

    def __call__(self) -> float:
        self.seconds += 1.0
        return self.seconds


@dataclass(frozen=True)
class ProviderMetadataFixture:
    provider: str = "minimax"
    model_id: str = "MiniMax-M3.1-Flash"
    pricing_source: str = "https://provider.example/pricing"
    pricing_version: str = "2026-10-19"
    currency: str = "USD"
    cost_status: str = "pending"
    reserved_cost_cents: int = 5
    usage: Usage | None = None
    outcome: str = "provider_error"
    request_count: int = 1
    request_sha256: str = "a" * 64
    response_sha256: str | None = None
    result_sha256: str | None = None
    raw_prompt: str = "private customer text must not be recorded"


class MetadataLlm:
    last_call: ProviderMetadataFixture | None = None

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
    ) -> tuple[JsonValue, Usage]:
        del system, user, schema, max_cost_cents
        self.last_call = ProviderMetadataFixture(
            cost_status="pending" if self.fail else "estimated",
            outcome="provider_error" if self.fail else "completed",
            usage=None if self.fail else Usage(40, 2, 3),
        )
        if self.fail:
            raise LlmProviderError("sanitized provider failure")
        return {"ok": True}, Usage(40, 2, 3)


class CountingSearcher(FakeSearcher):
    def __init__(self) -> None:
        super().__init__(lambda _: (SearchHit(CONTEST),))
        # A reused searcher proves the runner records a delta, not its lifetime count.
        self.requests_made = 17

    def search(self, request: SearchRequest) -> Sequence[SearchHit]:
        self.requests_made += 1
        return super().search(request)


class ScriptedLlm:
    def __init__(self, *, fail_at: int | None = None, empty_research: bool = False) -> None:
        self.ceilings: list[int | None] = []
        self.fail_at = fail_at
        self.empty_research = empty_research

    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
    ) -> tuple[JsonValue, Usage]:
        self.ceilings.append(max_cost_cents)
        if len(self.ceilings) == self.fail_at:
            raise LlmProviderError("provider unavailable", retryable=True)
        properties = schema["properties"]
        assert isinstance(properties, dict)
        if "candidate_index" in properties:
            quote: JsonValue = {
                "verbatim_quote": ISSUER_QUOTE,
                "source_sha256": hashlib.sha256(ROOT_HTML.encode()).hexdigest(),
            }
            return {
                "candidate_index": 1,
                "issuer_name": "City of San Diego",
                "issuer_type": "MUNICIPAL",
                "deciding_body": "City Parking Administration",
                "violation_state": "CA",
                "is_private_operator": False,
                "conflicted": False,
                "issuer_evidence": quote,
                "issuer_type_evidence": quote,
                "deciding_body_evidence": {
                    "verbatim_quote": BODY_QUOTE,
                    "source_sha256": hashlib.sha256(ROOT_HTML.encode()).hexdigest(),
                },
            }, Usage(100, 20, 5)
        if "links" in properties:
            return {
                "links": [
                    {
                        "index": 1,
                        "purpose": "PAYMENT",
                        "belongs_to_issuer": True,
                        "stage": None,
                        "verbatim_quote": ANCHOR,
                    }
                ]
            }, Usage(50, 10, 2)
        assert "findings" in properties
        if self.empty_research:
            return {}, Usage(40, 5, 1)
        return cast(
            JsonValue,
            {
                "findings": [
                    {
                        "key": key,
                        "value": value,
                        "status": "CONFIRMED",
                        "verbatim_quote": quote,
                        "source_sha256s": [hashlib.sha256(CONTEST_HTML.encode()).hexdigest()],
                    }
                    for key, value, quote in FACTS
                ]
                + [
                    {
                        "key": key,
                        "value": [],
                        "status": "CONFIRMED",
                        "verbatim_quote": PROCEDURE_TEXT,
                        "source_sha256s": [PROCEDURE_ASSESSMENT.source_sha256],
                        "evidence_basis": "NONE_STATED_IN_OFFICIAL_PROCEDURE",
                        "procedure_assessment": cast(
                            JsonValue, assessment_data(PROCEDURE_ASSESSMENT)
                        ),
                    }
                    for key in sorted(SILENCE_KEYS)
                ]
            },
        ), Usage(200, 40, 3)


def row() -> KeyRow:
    return parse_answer_key((FIXTURES / "bench_b16.jsonl").read_text())[0]


def dependencies(client: ScriptedLlm | None = None) -> RunDeps:
    return RunDeps(
        fetcher=FakeFetcher(
            {
                ROOT: html_response(ROOT, ROOT_HTML),
                CONTEST: html_response(CONTEST, CONTEST_HTML),
            }
        ),
        searcher=CountingSearcher(),
        llm=client or ScriptedLlm(),
        clock=fixed_clock(NOW),
        monotonic=Timer(),
        wait=lambda _: None,
        snapshot=DotGovSnapshot(
            date(2026, 10, 13),
            (DotGovRecord("sandiego.gov", "City", "City of San Diego", None, "San Diego", "CA"),),
            {},
            "a" * 64,
        ),
        preset="fake",
        commit="test-commit",
        key_version=1,
    )


def test_b16_golden_run() -> None:
    client = ScriptedLlm()
    result = run_case(row(), TODAY, dependencies(client))
    assert result.research_outcome == {"outcome": "SERVICEABLE", "ran": True}
    assert result.production_outcome["status"] == "supported"
    assert result.issuer_outcome["agency"] == {
        "id": "CA:sandiego.gov",
        "name": "City of San Diego",
        "state": "CA",
        "official_domains": ["sandiego.gov"],
        "issuer_type": "MUNICIPAL",
        "deciding_body": "City Parking Administration",
        "basis_key": "CA",
        "delegated_scopes": [],
    }
    assert result.resolution is not None
    assert result.resolution["deadline"] == {
        "deadline": "2026-10-21",
        "official": "2026-10-21",
        "notice": "2026-10-21",
        "disclose": False,
    }
    assert result.resolution["counting"] == {"rule": "POSTMARK", "disclose": False}
    assert result.brave_requests == 2
    assert result.cost_cents == {"prehold": 7, "research": 3}
    assert result.wall_seconds == {"prehold": 5.0, "research": 3.0}
    assert result.empty_ai_answers == 0
    assert result.digest_ok
    assert len(result.counter_keys) == 2
    assert client.ceilings == [25, 20, 50]
    # Preserve the historical fixture. Adapt recovery, fulfilment and milestone
    # expectations here without changing its source text or old-record readback.
    expected = json.loads((FIXTURES / "bench_b16_tiers_run.json").read_text())
    # Compare the original golden record under the current procedural-only policy.
    expected["findings"] = [
        f
        for f in expected["findings"]
        if f["key"] not in {"CODE_EXCEPTIONS", "GOVERNING_CODE_SECTION"}
    ]
    expected["resolution"]["notice_conflicts"] = []
    expected["resolution"]["awaiting_packet"] = False
    expected["brave_requests"] = 2
    expected["digest"]["research_status"] = "TIER1_PASSED"
    expected["digest"]["research_counts"]["findings"] = len(expected["findings"])
    expected["counter_keys"].append({**expected["counter_keys"][0], "milestone": "TIER1_PASSED"})
    expected["resolution"]["fulfilled_requirements"] = [
        f
        for f in expected["findings"]
        if f["key"]
        in {
            "REQUIRED_ITEMS",
            "REQUIRED_DECLARATIONS",
            "CONTEST_ELIGIBILITY",
            "MANDATORY_FORMS",
            "SIGNATURE_REQUIREMENT",
            "PRIOR_STEP_REQUIREMENT",
            "TICKET_SPECIFIC_REQUIREMENTS",
        }
    ]
    assert json.loads(result.to_json()) == expected


def test_wrappers_preserve_unwrapped_production_results() -> None:
    plain, wrapped = dependencies(), dependencies()
    recorded_fetcher = RecordingFetcher(wrapped.fetcher)
    recorded_llm = RecordingLlm(wrapped.llm, wrapped.monotonic)

    def issuer_run(deps: RunDeps, recording: bool) -> object:
        client = recorded_llm if recording else deps.llm
        return resolve_ticket_issuer(
            build_issuer_query(row(), TODAY),
            row().notice.stage_text,
            deps.snapshot,
            TODAY,
            deps.searcher,
            recorded_fetcher if recording else deps.fetcher,
            LlmIssuerAssessor(client),
            LlmLinkAssessor(client),
            deps.clock,
            budget_cents=25,
        )

    plain_issuer = issuer_run(plain, False)
    assert issuer_run(wrapped, True) == plain_issuer
    # Exercise the full extraction/recovery/resolution path with and without recording.
    from parkingbreaker.research.issuer_resolution import IssuerResolutionRun

    assert isinstance(plain_issuer, IssuerResolutionRun)
    assert plain_issuer.agency is not None
    agency = plain_issuer.agency
    recorded_llm.phase = "research"
    plain_result = run_research(
        build_ticket(row(), TODAY),
        agency,
        (),
        plain.fetcher,
        LlmExtractor(plain.llm),
        plain.clock,
        searcher=plain.searcher,
        recovery=RecoveryConfig(),
        wait=plain.wait,
        max_cost_cents=50,
    )
    wrapped_result = run_research(
        build_ticket(row(), TODAY),
        agency,
        (),
        recorded_fetcher,
        LlmExtractor(recorded_llm),
        wrapped.clock,
        searcher=wrapped.searcher,
        recovery=RecoveryConfig(),
        wait=wrapped.wait,
        max_cost_cents=50,
    )
    assert plain_result == wrapped_result
    assert plain_result.resolve(build_scripted_answers(row(), TODAY)) == wrapped_result.resolve(
        build_scripted_answers(row(), TODAY)
    )
    assert resolution_event(wrapped_result.resolve(build_scripted_answers(row(), TODAY))) == (
        case_state.ResearchComplete()
    )
    assert len(recorded_llm.calls) == 3


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_provider_error_is_recorded(fail_at: int) -> None:
    result = run_case(row(), TODAY, dependencies(ScriptedLlm(fail_at=fail_at)))
    assert result.research_outcome["outcome"] == "Stopped"
    assert result.research_outcome["family"] == "NOT_YET_VERIFIED"
    assert result.research_outcome["reason"] == "provider error: provider unavailable"
    assert result.research_outcome["hard_stop"] == ("S1" if fail_at == 3 else "S2")
    assert result.empty_ai_answers == 1
    assert result.ai_calls[-1].error == "provider unavailable"
    assert result.ai_calls[-1].usage == Usage()
    assert result.attempts[-1]["stage"] == "provider_error"
    assert json.loads(result.to_json())["empty_ai_answers"] == 1
    if fail_at == 3:
        assert result.digest is not None
        assert result.digest["research_status"] == ResearchStatus.REQUIRED_FACTS_UNVERIFIED
        assert {k["milestone"] for k in result.counter_keys} == {
            CounterMilestone.REQUEST_RECEIVED,
            CounterMilestone.REQUIRED_FACTS_UNVERIFIED,
        }


def test_cost_sums_per_phase() -> None:
    result = run_case(row(), TODAY, dependencies())
    for phase in ("prehold", "research"):
        assert result.cost_cents[phase] == sum(
            call.usage.cost_cents for call in result.ai_calls if call.phase == phase
        )
    assert result.digest is not None
    assert result.digest["ai_cost_cents"] == 10


def test_json_is_deterministic_and_records_are_frozen() -> None:
    first = run_case(row(), TODAY, dependencies())
    assert first.to_json() == first.to_json() == run_case(row(), TODAY, dependencies()).to_json()
    parsed = json.loads(first.to_json())
    assert first.to_json() == json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    with pytest.raises(FrozenInstanceError):
        first.case_id = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        dependencies().key_version = 2  # type: ignore[misc]


@pytest.mark.parametrize("printed_offset", [-1, 7])
def test_research_continues_after_intake_stop(
    printed_offset: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Printed deadline is an intake S4; research must still run to completion.
    # Removing the recorded basis also exercises the production S6 used outside CA.
    if printed_offset == 7:
        monkeypatch.setattr("parkingbreaker.bench.runner.STATE_BASIS", {})
    case = replace(row(), notice=replace(row().notice, printed_deadline_offset_days=printed_offset))
    result = run_case(case, TODAY, dependencies())
    assert result.production_outcome["hard_stop"] == ("S4" if printed_offset == -1 else "S6")
    assert result.research_outcome == {"outcome": "SERVICEABLE", "ran": True}
    assert result.cost_cents["research"] == 3


def test_stale_snapshot_skips_research_and_records_digest_error() -> None:
    deps = dependencies()
    deps = replace(deps, snapshot=replace(deps.snapshot, snapshot_date=TODAY - timedelta(days=32)))
    result = run_case(row(), TODAY, deps)
    assert result.issuer_outcome["code"] == "SNAPSHOT_STALE"
    assert result.research_outcome["ran"] is False
    assert result.research_outcome["hard_stop"] == "S2"
    assert result.cost_cents == {"prehold": 0, "research": 0}
    assert not result.digest_ok
    assert result.digest_error == "issuer_type: unresolved issuer has no digest category"
    assert result.counter_keys == ()


def test_empty_answers_stop_and_keep_critical_reasons() -> None:
    result = run_case(row(), TODAY, dependencies(ScriptedLlm(empty_research=True)))
    assert result.empty_ai_answers == 1
    assert result.research_outcome["outcome"] == "Stopped"
    assert result.research_outcome["hard_stop"] == "S1"
    assert result.critical_problems
    assert result.digest_ok


@pytest.mark.parametrize("empty", [False, True])
def test_bench_counts_resolved_tier1_pass_and_research_failure(empty: bool) -> None:
    result = run_case(row(), TODAY, dependencies(ScriptedLlm(empty_research=empty)))
    assert result.digest is not None
    status = ResearchStatus.REQUIRED_FACTS_UNVERIFIED if empty else ResearchStatus.TIER1_PASSED
    milestone = (
        CounterMilestone.REQUIRED_FACTS_UNVERIFIED if empty else CounterMilestone.TIER1_PASSED
    )
    assert result.digest["research_status"] == status
    assert {k["milestone"] for k in result.counter_keys} == {
        CounterMilestone.REQUEST_RECEIVED,
        milestone,
    }


def test_budget_exhaustion_is_recorded() -> None:
    deps = replace(dependencies(), llm=FakeLlmClient({}, Usage(20, 10, 26)))
    result = run_case(row(), TODAY, deps)
    assert result.issuer_outcome["code"] == "BUDGET_EXHAUSTED"
    assert result.empty_ai_answers == 1
    assert result.cost_cents == {"prehold": 0, "research": 0}


def test_research_budget_is_passed_and_exhaustion_keeps_attempt() -> None:
    client = ScriptedLlm()
    result = run_case(row(), TODAY, replace(dependencies(client), research_budget_cents=0))
    assert client.ceilings == [25, 20]
    assert result.cost_cents == {"prehold": 7, "research": 0}
    assert result.research_outcome["outcome"] == "Stopped"
    assert any(a.get("error") == "budget exhausted" for a in result.attempts)


def test_fetch_saving_failure_does_not_change_response() -> None:
    response = html_response(ROOT, ROOT_HTML)

    def fail_write(directory: str, sha: str, body: bytes) -> None:
        raise OSError("evidence store unavailable")

    wrapper = RecordingFetcher(
        FakeFetcher({ROOT: response}), evidence_dir="in-memory", writer=fail_write
    )
    assert wrapper.get(ROOT) is response
    assert wrapper.records[0].save_error == "evidence store unavailable"


def test_fetch_wrapper_records_raw_bytes_and_preserves_response_identity() -> None:
    response = redirect_response(ROOT, CONTEST)
    fetcher = FakeFetcher({ROOT: response})
    writes: list[tuple[str, str, bytes]] = []
    wrapper = RecordingFetcher(
        fetcher, evidence_dir="in-memory", writer=lambda *args: writes.append(args)
    )
    assert wrapper.get(ROOT) is response
    record = wrapper.records[0]
    assert (record.url, record.status, record.sha256, record.byte_length) == (
        ROOT,
        302,
        hashlib.sha256(b"").hexdigest(),
        0,
    )
    assert writes == [("in-memory", record.sha256, b"")]
    with pytest.raises(FetchError, match="no canned response"):
        wrapper.get(CONTEST)
    assert len(wrapper.records) == 1


@pytest.mark.parametrize("reply", [None, {}, [], "", {"findings": []}, {"links": []}])
def test_llm_wrapper_preserves_empty_results(reply: JsonValue) -> None:
    usage = Usage(40, 2, 3)
    client = FakeLlmClient(reply, usage)
    wrapper = RecordingLlm(client, Timer())
    data, cost = wrapper.complete_json("system", "user", {}, max_cost_cents=9)
    assert data is reply
    assert cost is usage
    assert client.ceilings == [9]
    assert wrapper.calls[0].empty
    assert wrapper.calls[0].wall_seconds == 1.0
    with pytest.raises(LlmBudgetExceeded):
        wrapper.complete_json("system", "user", {}, max_cost_cents=0)
    assert wrapper.calls[-1].empty


@pytest.mark.parametrize("fail", [False, True])
def test_llm_wrapper_records_safe_provider_metadata(fail: bool) -> None:
    wrapper = RecordingLlm(MetadataLlm(fail=fail), Timer())
    if fail:
        with pytest.raises(LlmProviderError, match="sanitized"):
            wrapper.complete_json("system", "private input", {}, max_cost_cents=9)
    else:
        wrapper.complete_json("system", "private input", {}, max_cost_cents=9)
    metadata = wrapper.calls[0].provider_metadata
    assert metadata is not None
    assert metadata["provider"] == "minimax"
    assert metadata["cost_status"] == ("pending" if fail else "estimated")
    assert metadata["outcome"] == ("provider_error" if fail else "completed")
    assert "raw_prompt" not in metadata
    assert "private input" not in json.dumps(metadata)


def test_case_run_round_trips_provider_metadata_without_changing_legacy_records() -> None:
    fixture = (FIXTURES / "bench_b16_run.json").read_text()
    original = CaseRun.from_json(fixture)
    assert json.loads(original.to_json()) == json.loads(fixture)

    metadata: dict[str, JsonValue] = {
        "provider": "minimax",
        "pricing_source": "https://provider.example/pricing",
        "pricing_version": "2026-10-19",
        "currency": "USD",
        "cost_status": "pending",
        "reserved_cost_cents": 5,
        "outcome": "provider_error",
    }
    calls = (replace(original.ai_calls[0], provider_metadata=metadata), *original.ai_calls[1:])
    updated = replace(original, ai_calls=calls)
    restored = CaseRun.from_json(updated.to_json())
    assert restored == updated
    assert restored.ai_calls[0].provider_metadata == metadata


def test_search_failure_is_recorded_by_existing_path() -> None:
    def fail(_: SearchRequest) -> Sequence[SearchHit]:
        raise SearchError("search unavailable")

    result = run_case(row(), TODAY, replace(dependencies(), searcher=FakeSearcher(fail)))
    assert any(attempt.get("error") == "search unavailable" for attempt in result.attempts)
    assert result.research_outcome["outcome"] == "Stopped"


def test_key_version_mismatch_is_not_a_provider_failure() -> None:
    with pytest.raises(ValueError, match="key_version"):
        run_case(row(), TODAY, replace(dependencies(), key_version=2))


class PaidSearcher(CountingSearcher):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.usage = Usage(100, 50, 10)  # Prior case usage must not count again.
        self.brave_requests = 0
        self.fail = fail

    def search(self, request: SearchRequest) -> Sequence[SearchHit]:
        self.usage += Usage(20, 5, 2)
        if self.fail:
            raise SearchError("search unavailable")
        return super().search(request)


class MetadataPaidSearcher(PaidSearcher):
    last_call: ProviderMetadataFixture | None = None

    def search(self, request: SearchRequest) -> Sequence[SearchHit]:
        self.last_call = ProviderMetadataFixture(
            provider="brave",
            model_id="",
            cost_status="pending" if self.fail else "estimated",
            outcome="provider_error" if self.fail else "completed",
            usage=Usage(0, 0, 1),
            response_sha256="b" * 64,
        )
        return super().search(request)


@pytest.mark.parametrize("fail", [False, True])
def test_search_recorder_preserves_safe_provider_metadata(fail: bool) -> None:
    searcher = MetadataPaidSearcher(fail=fail)
    recorder = RecordingSearcher(searcher, Timer())
    request = SearchRequest("private query", (), frozenset(), False)
    if fail:
        with pytest.raises(SearchError):
            recorder.search(request)
    else:
        recorder.search(request)
    metadata = recorder.calls[0].provider_metadata
    assert metadata is not None
    assert metadata["provider"] == "brave"
    assert metadata["cost_status"] == ("pending" if fail else "estimated")
    assert metadata["response_sha256"] == "b" * 64
    assert "raw_prompt" not in metadata
    assert "private query" not in json.dumps(metadata)


def test_native_search_costs_recorded_per_phase_and_in_digest() -> None:
    deps = replace(dependencies(), searcher=PaidSearcher())
    result = run_case(row(), TODAY, deps)
    assert result.cost_cents == {"prehold": 9, "research": 5}
    assert [call.phase for call in result.search_calls] == ["prehold", "research"]
    assert all(call.usage == Usage(20, 5, 2) for call in result.search_calls)
    assert result.brave_requests == 0
    assert result.digest is not None
    assert result.digest["cost_cents"] == result.digest["ai_cost_cents"] == 14
    assert CaseRun.from_json(result.to_json()) == result
    # Discovery still fetches and hashes the same official evidence bytes.
    assert {fetch.sha256 for fetch in result.fetches} == {
        hashlib.sha256(ROOT_HTML.encode()).hexdigest(),
        hashlib.sha256(CONTEST_HTML.encode()).hexdigest(),
    }


def test_failed_native_search_retains_charge_in_case() -> None:
    deps = replace(dependencies(), searcher=PaidSearcher(fail=True))
    result = run_case(row(), TODAY, deps)
    # Production recovery may continue after a failed discovery attempt.
    assert result.cost_cents == {"prehold": 9, "research": 4}
    assert [call.phase for call in result.search_calls] == ["prehold", "research", "research"]
    assert all(call.error == "search unavailable" for call in result.search_calls)
    assert all(call.usage.cost_cents == 2 for call in result.search_calls)
    assert result.brave_requests == 0
