"""One qualification run through the production path (bench plan section 4.4).

All effects are injected. Recording observes the seams without altering their returns
or exceptions; provider errors are turned into outcomes only at the run boundary.
Snapshot date/hash live in the injected DotGovSnapshot, avoiding duplicate metadata.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, timedelta
from typing import Literal, cast

from parkingbreaker import case_state
from parkingbreaker.agencies import ResolvedAgency
from parkingbreaker.bench.cases import (
    KeyRow,
    _decode,
    _unique_fields,
    build_issuer_query,
    build_scripted_answers,
    build_ticket,
)
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import LlmBudgetExceeded, LlmClient, LlmProviderError, Usage
from parkingbreaker.oplog.counters import counter_keys
from parkingbreaker.oplog.digest import (
    PhaseReached,
    ResearchStatus,
    iso_week_of,
    issuer_token,
    make_digest,
)
from parkingbreaker.prehold import PreholdDeps, PreholdRequest, run_prehold
from parkingbreaker.research.extractor import LlmExtractor
from parkingbreaker.research.findings import critical_problems
from parkingbreaker.research.issuer_assessment import LlmIssuerAssessor
from parkingbreaker.research.issuer_resolution import IssuerResolutionRun
from parkingbreaker.research.jurisdiction import DotGovSnapshot, match_outcome
from parkingbreaker.research.link_assessment import LlmLinkAssessor
from parkingbreaker.research.resolution import resolution_event
from parkingbreaker.research.run import RecoveryConfig, ResearchResult, run_research
from parkingbreaker.research.search import Searcher, SearchError, SearchHit, SearchRequest
from parkingbreaker.research.sources import Clock, Fetcher, FetchError, FetchResponse, SourceRecord
from parkingbreaker.service_policy import (
    SERVICE_CONTROL,
    STATE_BASIS,
    IssuerKind,
)

type Phase = Literal["prehold", "research"]
type EvidenceWriter = Callable[[str, str, bytes], None]


@dataclass(frozen=True)
class RunDeps:
    fetcher: Fetcher
    searcher: Searcher
    llm: LlmClient
    clock: Clock
    monotonic: Callable[[], float]
    wait: Callable[[float], None]
    snapshot: DotGovSnapshot
    preset: str
    commit: str
    key_version: int
    prehold_budget_cents: int = 25
    research_budget_cents: int = 50
    evidence_dir: str | None = None
    evidence_writer: EvidenceWriter | None = None

    def __post_init__(self) -> None:
        for name in ("prehold_budget_cents", "research_budget_cents"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name}: must be a nonnegative integer")
        if (self.evidence_dir is None) != (self.evidence_writer is None):
            raise ValueError("evidence_dir and evidence_writer must be supplied together")


@dataclass(frozen=True)
class FetchRecord:
    url: str
    status: int
    sha256: str
    byte_length: int
    save_error: str | None = None


class RecordingFetcher:
    def __init__(
        self,
        inner: Fetcher,
        *,
        evidence_dir: str | None = None,
        writer: EvidenceWriter | None = None,
    ) -> None:
        self.inner = inner
        self.evidence_dir = evidence_dir
        self.writer = writer
        self.records: list[FetchRecord] = []

    def get(self, url: str) -> FetchResponse:
        response = self.inner.get(url)
        sha = hashlib.sha256(response.body).hexdigest()
        save_error = None
        if self.writer is not None and self.evidence_dir is not None:
            try:
                self.writer(self.evidence_dir, sha, response.body)
            except Exception as exc:
                # Evidence storage is observation: its failure cannot change research.
                save_error = str(exc)
        self.records.append(FetchRecord(url, response.status, sha, len(response.body), save_error))
        return response


@dataclass(frozen=True)
class AiCall:
    phase: Phase
    usage: Usage
    wall_seconds: float
    empty: bool
    error: str | None = None
    provider_metadata: dict[str, JsonValue] | None = None


_SAFE_PROVIDER_METADATA_FIELDS = frozenset(
    {
        "provider",
        "model_id",
        "pricing_source",
        "pricing_version",
        "currency",
        "cost_status",
        "reserved_cost_cents",
        "usage",
        "outcome",
        "request_count",
        "request_sha256",
        "response_sha256",
        "result_sha256",
    }
)


def _provider_metadata(inner: object, previous: object | None) -> dict[str, JsonValue] | None:
    current = getattr(inner, "last_call", None)
    if current is None or current is previous or not is_dataclass(current):
        return None
    record = _record(current)
    selected = {
        name: value for name, value in record.items() if name in _SAFE_PROVIDER_METADATA_FIELDS
    }
    return selected or None


class RecordingLlm:
    """Empty means no JSON answer (including empty findings/links), or an exception.

    The interface exposes no separate refusal/truncation signal. Adapters encode invalid
    JSON as an empty object. Exceptions have no usage, so their reported cost is zero.
    """

    def __init__(self, inner: LlmClient, monotonic: Callable[[], float]) -> None:
        self.inner = inner
        self.monotonic = monotonic
        self.phase: Phase = "prehold"
        self.calls: list[AiCall] = []

    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
    ) -> tuple[JsonValue, Usage]:
        started = self.monotonic()
        previous_metadata = getattr(self.inner, "last_call", None)
        try:
            answer = self.inner.complete_json(system, user, schema, max_cost_cents=max_cost_cents)
        except (LlmBudgetExceeded, LlmProviderError) as exc:
            metadata = _provider_metadata(self.inner, previous_metadata)
            if metadata is None and isinstance(exc, LlmProviderError):
                metadata = {"cost_status": "pending", "outcome": "provider_error"}
            self.calls.append(
                AiCall(
                    self.phase,
                    Usage(),
                    self.monotonic() - started,
                    True,
                    str(exc),
                    metadata,
                )
            )
            raise
        data, usage = answer
        empty = not data or (
            isinstance(data, dict) and any(data.get(key) == [] for key in ("findings", "links"))
        )
        self.calls.append(
            AiCall(
                self.phase,
                usage,
                self.monotonic() - started,
                empty,
                provider_metadata=_provider_metadata(self.inner, previous_metadata),
            )
        )
        return answer


class RecordingSearcher:
    """Observe cumulative provider usage, even when a billable search fails."""

    def __init__(self, inner: Searcher, monotonic: Callable[[], float]) -> None:
        self.inner = inner
        self.monotonic = monotonic
        self.phase: Phase = "prehold"
        self.calls: list[AiCall] = []

    def search(self, request: SearchRequest) -> tuple[SearchHit, ...]:
        if not hasattr(self.inner, "usage"):
            return tuple(self.inner.search(request))
        before = getattr(self.inner, "usage", Usage())
        previous_metadata = getattr(self.inner, "last_call", None)
        started = self.monotonic()
        hits: tuple[SearchHit, ...] = ()
        error = None
        try:
            hits = tuple(self.inner.search(request))
            return hits
        except (LlmBudgetExceeded, SearchError) as exc:
            error = str(exc)
            raise
        finally:
            after = getattr(self.inner, "usage", Usage())
            if (
                isinstance(before, Usage)
                and isinstance(after, Usage)
                and hasattr(self.inner, "usage")
            ):
                usage = Usage(
                    after.input_tokens - before.input_tokens,
                    after.output_tokens - before.output_tokens,
                    after.cost_cents - before.cost_cents,
                )
                metadata = _provider_metadata(self.inner, previous_metadata)
                if metadata is None and error is not None and usage.cost_cents > 0:
                    metadata = {"cost_status": "pending", "outcome": "provider_error"}
                self.calls.append(
                    AiCall(
                        self.phase,
                        usage,
                        self.monotonic() - started,
                        not hits,
                        error,
                        metadata,
                    )
                )


def _json(value: object) -> JsonValue:
    """Serialize internal dataclasses without a provider SDK or filesystem codec."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json(getattr(value, field.name))
            for field in fields(value)
            if not (field.metadata.get("omit_if_none") and getattr(value, field.name) is None)
        }
    if isinstance(value, Mapping):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_json(item) for item in sorted(value)]
    raise TypeError(f"Unsupported record type: {type(value).__name__}")


def _record(value: object) -> dict[str, JsonValue]:
    result = _json(value)
    assert isinstance(result, dict)
    return result


@dataclass(frozen=True)
class CaseRun:
    case_id: str
    preset: str
    commit: str
    snapshot_sha256: str
    snapshot_date: date
    key_version: int
    run_date: date
    issuer_outcome: dict[str, JsonValue]
    delegation_outcomes: tuple[dict[str, JsonValue], ...]
    production_outcome: dict[str, JsonValue]
    research_outcome: dict[str, JsonValue]
    findings: tuple[dict[str, JsonValue], ...]
    sources: tuple[dict[str, JsonValue], ...]
    attempts: tuple[dict[str, JsonValue], ...]
    ai_calls: tuple[AiCall, ...]
    cost_cents: dict[str, int]
    brave_requests: int
    wall_seconds: dict[str, float]
    empty_ai_answers: int
    digest_ok: bool
    digest_error: str | None
    digest: dict[str, JsonValue] | None
    counter_keys: tuple[dict[str, JsonValue], ...]
    fetches: tuple[FetchRecord, ...]
    resolution: dict[str, JsonValue] | None
    critical_problems: tuple[dict[str, JsonValue], ...]
    stage_represented: bool = False
    search_calls: tuple[AiCall, ...] = ()

    def to_json(self) -> str:
        record = _record(self)
        for name in ("ai_calls", "search_calls"):
            calls = record.get(name)
            if isinstance(calls, list):
                for call in calls:
                    if isinstance(call, dict) and call.get("provider_metadata") is None:
                        call.pop("provider_metadata", None)
        # Preserve the offline/legacy wire format when no paid search usage exists.
        if not self.search_calls:
            record.pop("search_calls")
        return json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_json(cls, text: str) -> CaseRun:
        """Restore dates, tuples and nested records, allowing CLI envelope metadata."""
        raw: object = json.loads(text, object_pairs_hook=_unique_fields)
        if isinstance(raw, dict):
            raw.setdefault("search_calls", [])
            for name in ("ai_calls", "search_calls"):
                calls = raw.get(name)
                if isinstance(calls, list):
                    for call in calls:
                        if isinstance(call, dict):
                            call.setdefault("provider_metadata", None)
            for name in ("run_id", "repeat_index", "fake", "search"):
                raw.pop(name, None)
        return cast(CaseRun, _decode(cls, raw, "run"))


def _source(source: SourceRecord) -> dict[str, JsonValue]:
    # Evidence text/bytes are held by their hash, not duplicated in every run record.
    return {
        "url": source.url,
        "final_url": source.final_url,
        "sha256": source.sha256,
        "retrieved_at": source.retrieved_at.isoformat(),
        "authority_class": source.authority_class,
        "http_status": source.http_status,
    }


def _provider_stop(exc: Exception, hard_stop: case_state.HardStop) -> dict[str, JsonValue]:
    return {
        "outcome": "Stopped",
        "family": case_state.StopFamily.NOT_YET_VERIFIED,
        "reason": f"provider error: {exc}",
        "hard_stop": hard_stop,
        "error_type": type(exc).__name__,
    }


def _digest(
    agency: ResolvedAgency | None,
    issuer: IssuerResolutionRun | None,
    outcome: dict[str, JsonValue],
    run_date: date,
    costs: dict[str, int],
    durations: dict[str, float],
    result: ResearchResult | None,
    research_status: ResearchStatus,
) -> tuple[dict[str, JsonValue], tuple[dict[str, JsonValue], ...]]:
    # The production digest has no unknown issuer type or customer-pending outcome.
    # Record that limitation explicitly rather than inventing an issuer/category.
    if agency is None:
        raise ValueError("issuer_type: unresolved issuer has no digest category")
    if outcome["outcome"] == "SERVICEABLE":
        family: Literal["SERVICEABLE"] | case_state.StopFamily = "SERVICEABLE"
    elif isinstance(outcome.get("family"), str):
        family = case_state.StopFamily(str(outcome["family"]))
    else:
        raise ValueError(f"outcome: {outcome['outcome']} has no digest category")
    hard_stop = outcome.get("hard_stop")
    codes = (
        tuple(
            code
            for code in (issuer.code, *(d.code for d in issuer.delegations))
            if code is not None
        )
        if issuer is not None
        else ()
    )
    researching = outcome.get("ran") is True
    digest = make_digest(
        issuer_token=issuer_token(agency),
        issuer_type=agency.issuer_type,
        basis_key_token=agency.basis_key.replace(":", "_"),
        ticket_type="parking",
        violation_category="parking",
        stage_token=None,
        phase_reached=PhaseReached.RESEARCHING if researching else PhaseReached.PREHOLD,
        outcome=family,
        hard_stop=case_state.HardStop(hard_stop) if isinstance(hard_stop, str) else None,
        veto_codes=codes,
        match_outcome=match_outcome(issuer.candidates) if issuer is not None else None,
        delegation_counts=Counter(d.outcome for d in issuer.delegations) if issuer else {},
        mode="restricted_clerical",
        states_reached=("RESEARCHING",) if researching else ("PREHOLD",),
        stage_durations_s={phase: int(seconds) for phase, seconds in durations.items()},
        research_counts={
            "sources": len(result.sources) if result else 0,
            "findings": len(result.findings) if result else 0,
        },
        question_types=(),
        default_types=(),
        redraft_count=0,
        flag_types=(),
        feasibility_margin_days=None,
        lob_lag_days=None,
        usps_lag_days=None,
        payment_class="no_hold",
        remedy_class=None,
        cost_cents=sum(costs.values()),
        ai_cost_cents=sum(costs.values()),
        mail_class=None,
        iso_week=iso_week_of(run_date),
        research_status=research_status,
    )
    # Use the digest's own allowlisted JSON, including mappings rather than pair arrays.
    return _record(json.loads(digest.to_prompt_json())), tuple(
        _record(k) for k in counter_keys(digest)
    )


def run_case(row: KeyRow, run_date: date, deps: RunDeps) -> CaseRun:
    if row.key_version != deps.key_version:
        raise ValueError("key_version: row and run dependencies disagree")
    fetcher = RecordingFetcher(
        deps.fetcher, evidence_dir=deps.evidence_dir, writer=deps.evidence_writer
    )
    client = RecordingLlm(deps.llm, deps.monotonic)
    searcher = RecordingSearcher(deps.searcher, deps.monotonic)
    request_start = int(
        getattr(deps.searcher, "brave_requests", getattr(deps.searcher, "requests_made", 0))
    )
    issuer: IssuerResolutionRun | None = None
    result: ResearchResult | None = None
    agency: ResolvedAgency | None = None
    attempts: list[dict[str, JsonValue]] = []
    started = deps.monotonic()
    ticket = build_ticket(row, run_date)
    answers = build_scripted_answers(row, run_date)
    printed_offset = row.notice.printed_deadline_offset_days
    prehold = run_prehold(
        PreholdRequest(
            build_issuer_query(row, run_date),
            row.notice.stage_text,
            IssuerKind.GOVERNMENT,
            answers.notice_issue_date or ticket.issue_date,
            None if printed_offset is None else run_date + timedelta(days=printed_offset),
            run_date,
        ),
        PreholdDeps(
            deps.snapshot,
            searcher,
            fetcher,
            LlmIssuerAssessor(client),
            LlmLinkAssessor(client),
            deps.clock,
            deps.prehold_budget_cents,
            STATE_BASIS,
            SERVICE_CONTROL,
        ),
        # The bench measures national coverage; launch scope is not under test.
        launch_allowlist=None,
    )
    issuer = prehold.issuer
    production = prehold.support
    if issuer is not None:
        agency = issuer.agency
        issuer_outcome = {
            "outcome": issuer.outcome.value,
            "agency": _json(agency),
            "family": issuer.stop_family,
            "hard_stop": issuer.stop,
            "reason": issuer.reason,
            "code": issuer.code,
            "evidence": _json(issuer.issuer_evidence),
        }
        attempts.extend({"phase": "prehold", **_record(a)} for a in issuer.attempts)
    else:
        exc = prehold.provider_error
        assert exc is not None  # the bench always resolves a government issuer
        issuer_outcome = {
            **_provider_stop(exc, case_state.HardStop.S2),
            "agency": None,
            "code": None,
        }
        attempts.append({"phase": "prehold", "stage": "provider_error", "error": str(exc)})
    durations = {"prehold": deps.monotonic() - started, "research": 0.0}
    resolution: dict[str, JsonValue] | None = None
    research_status = ResearchStatus.PENDING
    # No research when issuer resolution fails. Preserve its traced stop as the outcome.
    research_outcome = {**issuer_outcome, "outcome": "Stopped", "ran": False}
    if agency is not None:
        client.phase = "research"
        searcher.phase = "research"
        started = deps.monotonic()
        try:
            result = run_research(
                ticket,
                agency,
                (),
                fetcher,
                LlmExtractor(client),
                deps.clock,
                searcher=searcher,
                recovery=RecoveryConfig(),
                wait=deps.wait,
                max_cost_cents=deps.research_budget_cents,
            )
            attempts.extend({"phase": "research", **_record(a)} for a in result.attempts)
            resolved = result.resolve(answers)
            resolution = _record(resolved)
            event = resolution_event(resolved)
            if isinstance(event, case_state.ResearchComplete):
                research_outcome = {"outcome": "SERVICEABLE"}
                research_status = ResearchStatus.TIER1_PASSED
            elif isinstance(event, case_state.CustomerNeeded):
                research_outcome = {"outcome": "CUSTOMER_NEEDED", "question": event.question}
            elif isinstance(event, case_state.Stopped):
                if event.reason is case_state.StopReason.NOT_YET_VERIFIED:
                    research_status = ResearchStatus.REQUIRED_FACTS_UNVERIFIED
                research_outcome = {
                    "outcome": "Stopped",
                    "family": event.reason,
                    "reason": event.detail,
                    "hard_stop": event.hard_stop,
                }
            else:
                research_outcome = {"outcome": "RECOVERY_PENDING"}
            research_outcome["ran"] = True
        except (LlmBudgetExceeded, LlmProviderError, FetchError, SearchError) as exc:
            research_status = ResearchStatus.REQUIRED_FACTS_UNVERIFIED
            research_outcome = {**_provider_stop(exc, case_state.HardStop.S1), "ran": True}
            attempts.append({"phase": "research", "stage": "provider_error", "error": str(exc)})
        durations["research"] = deps.monotonic() - started
    costs = {
        phase: sum(
            call.usage.cost_cents
            for call in (*client.calls, *searcher.calls)
            if call.phase == phase
        )
        for phase in ("prehold", "research")
    }
    digest = None
    keys: tuple[dict[str, JsonValue], ...] = ()
    digest_error = None
    try:
        digest, keys = _digest(
            agency, issuer, research_outcome, run_date, costs, durations, result, research_status
        )
    except ValueError as exc:
        digest_error = str(exc)
    sources = (*(issuer.pages if issuer else ()), *(result.sources if result else ()))
    return CaseRun(
        case_id=row.case_id,
        preset=deps.preset,
        commit=deps.commit,
        snapshot_sha256=deps.snapshot.content_sha256,
        snapshot_date=deps.snapshot.snapshot_date,
        key_version=deps.key_version,
        run_date=run_date,
        issuer_outcome=issuer_outcome,
        delegation_outcomes=tuple(_record(d) for d in issuer.delegations) if issuer else (),
        production_outcome=_record(production),
        research_outcome=research_outcome,
        findings=tuple(_record(f) for f in result.findings) if result else (),
        sources=tuple(_source(s) for s in sources),
        attempts=tuple(attempts),
        ai_calls=tuple(client.calls),
        cost_cents=costs,
        brave_requests=int(
            getattr(deps.searcher, "brave_requests", getattr(deps.searcher, "requests_made", 0))
        )
        - request_start,
        search_calls=tuple(searcher.calls),
        wall_seconds=durations,
        empty_ai_answers=sum(call.empty for call in client.calls),
        digest_ok=digest_error is None,
        digest_error=digest_error,
        digest=digest,
        counter_keys=keys,
        fetches=tuple(fetcher.records),
        resolution=resolution,
        critical_problems=tuple(
            _record(p) for p in critical_problems(result.findings, result.sources)
        )
        if result
        else (),
    )
