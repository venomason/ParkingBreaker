"""Task 7: real gates joined by fake I/O; saved agency fixtures remain unchanged."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from parkingbreaker import case_state as cs
from parkingbreaker.appeal.adoption import adopt
from parkingbreaker.appeal.packet import ApprovalRefused, Packet, approval_gate, build_packet
from parkingbreaker.bench.cases import build_ticket
from parkingbreaker.json_types import JsonValue
from parkingbreaker.oplog.digest import CounterMilestone
from parkingbreaker.research.extractor import FINDINGS_SCHEMA, LlmExtractor
from parkingbreaker.research.fakes import (
    FakeFetcher,
    FakeLlmClient,
    FakeSearcher,
    fixed_clock,
    html_response,
)
from parkingbreaker.research.findings import FindingStatus, ReleaseDecision
from parkingbreaker.research.ladder import Ask, CustomerQuestion
from parkingbreaker.research.questions import (
    ANSWERABLE_KEYS,
    CRITICAL_KEYS,
    DEFAULT_TIERS,
    CustomerPosition,
    FactKey,
    FactTier,
    PacketPlan,
)
from parkingbreaker.research.resolution import (
    ResearchAnswers,
    VehicleRelationship,
    answer_question,
    resolution_event,
)
from parkingbreaker.research.run import RecoveryConfig, ResearchResult, SeedUrl, run_research
from parkingbreaker.research.search import SearchError
from parkingbreaker.research.sources import AuthorityClass
from parkingbreaker.research_records import (
    answers_json,
    decode_answers,
    decode_research,
    decode_resolution,
    research_json,
    resolution_json,
)
from parkingbreaker.session_window import WindowStatus
from parkingbreaker.submit_gate import GateFailure, GateResult, check_submit_gate
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from samples import SAN_FRANCISCO
from test_appeal_packet import CITATION, letter_text
from test_bench_runner import CONTEST, NOW, TODAY, ScriptedLlm, dependencies, row
from test_transaction_store import coverage_rows

# Only synthetic pages are constructed here, separately from the saved fixture files.
POLICY_URL = CONTEST + "/synthetic-task7-policy"
SCOPE = "San Diego Parking Citation Initial Review"
NOTICE = ResearchAnswers(
    notice_issue_date=TODAY - timedelta(days=7),
    answered=frozenset(CustomerQuestion),
)
# The already-known identifier printed by the shared fixture letter.
CITATION_NUMBER = "SF-998877"


def cited(research: ResearchResult) -> ResearchResult:
    """Record the fixture's known citation number in the saved packet plan."""
    plan = research.packet_plan
    if any(name == "citation_number" for name, _ in plan.customer_fields):
        return research
    return replace(
        research,
        packet_plan=replace(
            plan,
            customer_fields=(*plan.customer_fields, ("citation_number", CITATION_NUMBER)),
        ),
    )


def model_answers() -> list[dict[str, JsonValue]]:
    reply, _ = ScriptedLlm().complete_json("", "", FINDINGS_SCHEMA)
    assert isinstance(reply, dict)
    return cast(list[dict[str, JsonValue]], reply["findings"])


def extract_case(
    findings: list[dict[str, JsonValue]],
    *,
    policy: str | None = None,
    missing_page: bool = False,
    failed_search: bool = False,
) -> tuple[ResearchResult, FakeLlmClient, FakeSearcher]:
    deps = dependencies()
    transport = deps.fetcher
    # dependencies() deliberately exposes protocol ports; keep mutations on our fake.
    assert isinstance(transport, FakeFetcher)
    responses = dict(transport.responses)
    seeds = [SeedUrl(CONTEST, AuthorityClass.OFFICIAL_PROCEDURE, CRITICAL_KEYS)]
    if missing_page:
        responses.pop(CONTEST)
    if policy is not None:
        responses[POLICY_URL] = html_response(POLICY_URL, policy)
        seeds.append(SeedUrl(POLICY_URL, AuthorityClass.OFFICIAL_PROCEDURE, CRITICAL_KEYS))
    transport.responses = responses

    def search(_: object) -> tuple[()]:
        if failed_search:
            raise SearchError("synthetic search unavailable")
        return ()

    searcher = FakeSearcher(search)
    client = FakeLlmClient({"findings": cast(JsonValue, findings)})
    result = run_research(
        build_ticket(row(), TODAY),
        # The bench's resolved jurisdiction, without rerunning issuer discovery.
        replace(
            SAN_FRANCISCO,
            id="CA:sandiego.gov",
            name="City of San Diego",
            official_domains=("sandiego.gov",),
            deciding_body="City Parking Administration",
        ),
        seeds,
        transport,
        LlmExtractor(client),
        fixed_clock(NOW),
        searcher=searcher,
        recovery=RecoveryConfig(retry_delays=()),
        wait=lambda _: None,
    )
    assert result.ticket.stage == "first-level review"
    assert len(result.questions) == len(result.findings) == len(ANSWERABLE_KEYS)
    assert {f.key for f in result.findings} == set(ANSWERABLE_KEYS)
    assert all(f.status in FindingStatus for f in result.findings)
    assert decode_research(research_json(result)) == result
    return decode_research(research_json(result)), client, searcher


def packet() -> Packet:
    return build_packet(letter_text(), CITATION, [], NOW, citation_number=CITATION_NUMBER)


def submit(
    research: ResearchResult, frozen: Packet, answers: ResearchAnswers = NOTICE
) -> GateResult:
    return check_submit_gate(
        auth_valid=True,
        window_status=WindowStatus.ACTIVE,
        adoption_hash=adopt(letter_text(), "customer", fixed_clock(NOW)).sha256,
        frozen_letter_hash=frozen.letter_text_sha256,
        deadline_feasible_now=True,
        address_verified=True,
        research=cited(research),
        research_answers=answers,
        frozen_pdf=frozen.pdf,
    )


def start(store: SqliteTransactionStore) -> CaseRecord:
    store.save_workspace("task7", Workspace(), NOW)
    return store.start_case("task7", "fake-authorization", NOW, NOW + timedelta(days=5))


def letter(
    store: SqliteTransactionStore,
    current: CaseRecord,
    event: cs.Event,
    *,
    effect_ids: tuple[str, ...] = (),
) -> CaseRecord:
    return store.apply_letter_event(
        current.case_token, event, current.revision, occurred_at=NOW, effect_ids=effect_ids
    )


def assert_blocked(research: ResearchResult, answers: ResearchAnswers = NOTICE) -> None:
    frozen = packet()
    resolved = research.resolve(
        answers,
        packet_plan=PacketPlan(
            frozen_pdf=frozen.pdf,
            customer_fields=(("citation_number", CITATION_NUMBER),),
        ),
    )
    assert not resolved.ready
    assert not isinstance(resolution_event(resolved), cs.ResearchComplete)
    assert decode_resolution(resolution_json(resolved)) == resolved
    with pytest.raises(ApprovalRefused, match=r"Tier 1|required agency declaration"):
        approval_gate(
            adopt(letter_text(), "customer", fixed_clock(NOW)),
            frozen,
            research=research,
            answers=answers,
            citation_number=CITATION_NUMBER,
        )
    assert submit(research, frozen, answers).failures == (GateFailure.RESEARCH_UNRESOLVED,)


def policy_page(quote: str) -> str:
    return f"<h1>{SCOPE}</h1><p>{quote}</p>"


def requirement(key: FactKey, quote: str) -> dict[str, JsonValue]:
    policy = policy_page(quote)
    return {
        "key": key.value,
        "status": "CONFIRMED",
        "value": [quote],
        "verbatim_quote": quote,
        "source_sha256s": [hashlib.sha256(policy.encode()).hexdigest()],
        "jurisdiction": "CA:sandiego.gov",
        "stage": "first-level review",
        "scope_quote": SCOPE,
    }


@pytest.mark.parametrize("key", sorted(CRITICAL_KEYS))
@pytest.mark.parametrize(
    "damage", ["missing", "malformed", "quote", "hash", "bare_na", "conflict", "stage", "secondary"]
)
def test_each_tier1_gap_blocks_research_resolution_approval_and_submit(
    key: FactKey, damage: str
) -> None:
    findings = model_answers()
    claim = next(f for f in findings if f["key"] == key)
    if damage == "missing":
        findings.remove(claim)
    elif damage == "malformed":
        claim["status"] = "invalid"
    elif damage == "quote":
        claim["verbatim_quote"] = "This sentence is absent from every saved page."
    elif damage == "hash":
        claim["source_sha256s"] = ["0" * 64]
    elif damage == "conflict":
        claim["status"] = "CONFLICTED"
    elif damage == "stage":
        claim["stage"] = "administrative hearing"
    elif damage == "bare_na":
        claim.update(status="NOT_APPLICABLE", value=None, verbatim_quote=None)
    research, _, searcher = extract_case(findings)
    if damage == "secondary":
        research = replace(
            research,
            sources=tuple(
                replace(source, authority_class=AuthorityClass.SECONDARY)
                for source in research.sources
            ),
        ).reassess()
        research = decode_research(research_json(research))
    assert not research.decision.ok
    assert any(key in reason for reason in research.decision.reasons)
    assert len(searcher.calls) == 1
    assert_blocked(research)


@pytest.mark.parametrize("key", sorted(set(ANSWERABLE_KEYS) - CRITICAL_KEYS))
@pytest.mark.parametrize("status", [FindingStatus.UNKNOWN, FindingStatus.CONFLICTED])
def test_tier2_and_tier3_gaps_pass_all_gates_after_bounded_recovery(
    key: FactKey, status: FindingStatus
) -> None:
    findings = model_answers()
    findings.append(
        {
            "key": key.value,
            "value": None,
            "status": status.value,
            "verbatim_quote": None,
            "source_sha256s": [],
        }
    )
    research, client, searcher = extract_case(findings)
    assert len(client.calls) == 1
    assert len(searcher.calls) == 1  # Tier 2 gaps share the single remaining search.
    assert all(DEFAULT_TIERS[k] is FactTier.IMPORTANT for k in searcher.calls[0].keys)
    assert research.resolve(NOTICE).ready
    assert resolution_event(research.resolve(NOTICE)) == cs.ResearchComplete()
    frozen = packet()
    assert (
        approval_gate(
            adopt(letter_text(), "customer", fixed_clock(NOW)),
            frozen,
            research=research,
            answers=NOTICE,
            citation_number=CITATION_NUMBER,
        )
        == cs.CustomerApproved()
    )
    assert submit(research, frozen).passed
    assert next(f for f in research.findings if f.key is key).status is status


def test_tier3_only_gap_reaches_frozen_submit_without_dedicated_recovery() -> None:
    facts: tuple[tuple[FactKey, JsonValue, str], ...] = (
        (FactKey.GOVERNING_CODE_SECTION, "SEC. 10.2", "SEC. 10.2. PARKING."),
        (
            FactKey.CODE_EXCEPTIONS,
            ["a vehicle stopped to load or unload passengers"],
            "Exceptions: a vehicle stopped to load or unload passengers.",
        ),
    )
    policy = policy_page(" ".join(quote for _, _, quote in facts))
    sha = hashlib.sha256(policy.encode()).hexdigest()
    findings = model_answers()
    for key, value, quote in facts:
        findings.append(
            {
                **requirement(key, quote),
                "value": value,
                "source_sha256s": [sha],
            }
        )
    research, client, searcher = extract_case(findings, policy=policy)
    assert research.resolve(NOTICE).ready
    assert (
        next(f for f in research.findings if f.key is FactKey.FINE_AMOUNT).status
        is FindingStatus.UNKNOWN
    )
    assert len(client.calls) == 1
    assert searcher.calls == []
    assert not research.progress.re_extraction_done
    assert not research.progress.broadened_search_done
    frozen = packet()
    assert (
        approval_gate(
            adopt(letter_text(), "customer", fixed_clock(NOW)),
            frozen,
            research=research,
            answers=NOTICE,
            citation_number=CITATION_NUMBER,
        )
        == cs.CustomerApproved()
    )
    assert submit(research, frozen).passed


@pytest.mark.parametrize("key", sorted(CRITICAL_KEYS))
def test_tier1_gap_after_approval_cannot_reuse_saved_pass(key: FactKey) -> None:
    research, _, _ = extract_case(model_answers())
    frozen = packet()
    adoption = adopt(letter_text(), "customer", fixed_clock(NOW))
    assert (
        approval_gate(
            adoption,
            frozen,
            research=research,
            answers=NOTICE,
            citation_number=CITATION_NUMBER,
        )
        == cs.CustomerApproved()
    )
    assert submit(research, frozen).passed
    stale = replace(
        research,
        decision=ReleaseDecision(True, ()),
        findings=tuple(
            replace(f, status=FindingStatus.UNKNOWN, value=None, evidence=()) if f.key is key else f
            for f in research.findings
        ),
    )
    # Keep the earlier decision/pending fields deliberately: neither gate may trust them.
    stale = decode_research(research_json(stale))
    assert stale.decision.ok
    assert_blocked(stale)


@pytest.mark.parametrize("omitted", ["research", "packet", "both"])
def test_submit_cannot_omit_research_or_frozen_packet(omitted: str) -> None:
    research, _, _ = extract_case(model_answers())
    frozen = packet()
    result = check_submit_gate(
        auth_valid=True,
        window_status=WindowStatus.ACTIVE,
        adoption_hash=frozen.letter_text_sha256,
        frozen_letter_hash=frozen.letter_text_sha256,
        deadline_feasible_now=True,
        address_verified=True,
        research=research if omitted == "packet" else None,
        frozen_pdf=frozen.pdf if omitted == "research" else None,
    )
    assert result.failures == (GateFailure.RESEARCH_UNRESOLVED,)


def test_customer_clarification_promotion_and_frozen_inventory_survive_readback(
    tmp_path: Path,
) -> None:
    findings = model_answers()
    quote = "Only the registered owner may contest."
    findings = [f for f in findings if f["key"] != FactKey.CONTEST_ELIGIBILITY]
    findings.append(requirement(FactKey.CONTEST_ELIGIBILITY, quote))
    research, client, searcher = extract_case(findings, policy=policy_page(quote))
    assert_blocked(research, ResearchAnswers())
    # The official standing rule survives; only the customer's relationship is supplied.
    resolution = research.resolve(ResearchAnswers(notice_issue_date=NOTICE.notice_issue_date))
    assert resolution.questions == (Ask(CustomerQuestion.VEHICLE_RELATIONSHIP),)
    answers = answer_question(
        ResearchAnswers(notice_issue_date=NOTICE.notice_issue_date),
        CustomerQuestion.VEHICLE_RELATIONSHIP,
        VehicleRelationship.REGISTERED_OWNER,
    )
    answers = decode_answers(answers_json(answers))
    assert research.resolve(answers).ready
    # A historic customer defence selection is inert: it promotes nothing, revives no
    # retired key and never starts defence research (audit A017/A019).
    promoted = research.reassess(
        customer_position=CustomerPosition(exception_basis="chosen defense")
    )
    promoted = decode_research(research_json(promoted))
    assert promoted.tier_evaluation is not None
    assert promoted.tier_evaluation.tiers[FactKey.CODE_EXCEPTIONS] is FactTier.NICE_TO_HAVE
    assert promoted.tier_evaluation.promotions == ()
    assert research.resolve(answers).ready
    assert len(client.calls) == len(searcher.calls) == 1  # No new research on edit/readback.

    path = tmp_path / "acceptance.sqlite"
    with SqliteTransactionStore(path) as store:
        current = start(store)
        event = resolution_event(research.resolve(answers))
        assert isinstance(event, cs.ResearchComplete)
        current = letter(store, current, event)
        assert current.letter_state is cs.CaseState.DRAFTING
        current = letter(store, current, cs.DraftReady())
        frozen = packet()
        approved = approval_gate(
            adopt(letter_text(), "customer", fixed_clock(NOW)),
            frozen,
            research=research,
            answers=answers,
            citation_number=CITATION_NUMBER,
        )
        current = letter(store, current, approved)
        assert current.letter_state is cs.CaseState.FROZEN
        assert submit(research, frozen, answers).passed
        assert store.flush_counters(occurred_at=NOW)
        assert store.flush_counters(occurred_at=NOW)
    assert Counter(k.milestone for k in coverage_rows(path)) == {
        CounterMilestone.REQUEST_RECEIVED: 1,
        CounterMilestone.TIER1_PASSED: 1,
    }


@pytest.mark.parametrize("kind", ["form", "signature", "declaration"])
def test_frozen_inventory_is_rechecked_after_extraction_and_approval(kind: str) -> None:
    from item2_helpers import DECLARATION, DECLARATION_RULE, customer_ink, form_case
    from test_research_fulfilment import with_requirement

    text = letter_text()
    if kind == "form":
        from parkingbreaker.appeal.letter import enclosure_line

        research, _, form = form_case()
        text += "\n\n" + str(
            enclosure_line(True, 0, required_forms=("Parking Citation Initial Review Form",))
        )
        complete = build_packet(
            text,
            CITATION,
            [],
            NOW,
            research=research,
            completed_forms=[form],
            citation_number=CITATION_NUMBER,
        )
    else:
        key = (
            FactKey.SIGNATURE_REQUIREMENT if kind == "signature" else FactKey.REQUIRED_DECLARATIONS
        )
        quote = "You must sign the letter." if kind == "signature" else DECLARATION_RULE
        research = with_requirement(
            key, "letter" if kind == "signature" else "signed declaration", quote
        )
        research = cited(research)
        if kind == "declaration":
            text = text.replace("Sincerely,", DECLARATION + "\n\nSincerely,")
        complete = build_packet(
            text,
            CITATION,
            [],
            NOW,
            research=research,
            customer_signature=customer_ink(),
            citation_number=CITATION_NUMBER,
        )
    saved = decode_research(
        research_json(
            research.reassess(packet_plan=replace(research.packet_plan, frozen_pdf=complete.pdf))
        )
    )
    assert saved.resolve().ready
    assert (
        approval_gate(
            adopt(text, "customer", fixed_clock(NOW)),
            complete,
            research=saved,
            citation_number=CITATION_NUMBER,
        )
        == cs.CustomerApproved()
    )
    assert_blocked(saved)


@pytest.mark.parametrize("damage", ["judgment", "fragment", "hash", "stage", "secondary"])
def test_procedural_silence_requires_complete_official_procedure_at_every_gate(damage: str) -> None:
    findings = model_answers()
    claim = next(f for f in findings if f["key"] == FactKey.MANDATORY_FORMS)
    assessment = claim["procedure_assessment"]
    assert isinstance(assessment, dict)
    if damage == "judgment":
        assessment["complete"] = False
    elif damage == "fragment":
        assessment["procedure_text"] = "You may contest your parking citation by mail."
    elif damage == "hash":
        assessment["source_sha256"] = "0" * 64
    elif damage == "stage":
        assessment["stage"] = "administrative hearing"
    research, _, _ = extract_case(findings)
    if damage == "secondary":
        research = replace(
            research,
            sources=tuple(
                replace(s, authority_class=AuthorityClass.SECONDARY) for s in research.sources
            ),
        ).reassess()
        research = decode_research(research_json(research))
    assert_blocked(research)


@pytest.mark.parametrize("quote", ["Include a SASE.", "Return the original ticket."])
def test_unfulfillable_s3_preserves_official_quote_through_all_gates(quote: str) -> None:
    findings = [f for f in model_answers() if f["key"] != FactKey.REQUIRED_ITEMS]
    findings.append(requirement(FactKey.REQUIRED_ITEMS, quote))
    research, _, _ = extract_case(findings, policy=policy_page(quote))
    assert_blocked(research)
    event = resolution_event(research.resolve(NOTICE))
    assert event == cs.Stopped(cs.StopReason.PROCESS_INCOMPATIBLE, quote, cs.HardStop.S3)
    transition = cs.transition(cs.CaseState.RESEARCHING, event)
    assert transition.target is cs.CaseState.STOPPED_NO_CHARGE
    assert transition.effects == (cs.Effect.RELEASE_HOLD,)


@pytest.mark.parametrize("failure", ["missing_page", "empty_extraction", "search", "conflict"])
def test_failed_research_counts_once_never_unsupported(failure: str, tmp_path: Path) -> None:
    findings = model_answers()
    if failure == "empty_extraction":
        findings = []
    elif failure == "search":
        findings = [f for f in findings if f["key"] != FactKey.MAIL_IN_ACCEPTED]
    policy = None
    if failure == "conflict":
        quote = "Your mailed contest must be received by the deadline."
        policy = policy_page(quote)
        conflicting = requirement(FactKey.DEADLINE_RULE, quote)
        conflicting["value"] = "RECEIPT"
        findings.append(conflicting)
    research, _, _ = extract_case(
        findings,
        policy=policy,
        missing_page=failure == "missing_page",
        failed_search=failure == "search",
    )
    assert_blocked(research)
    if failure == "conflict":
        assert (
            next(f for f in research.findings if f.key is FactKey.DEADLINE_RULE).status
            is FindingStatus.CONFLICTED
        )
    event = resolution_event(research.resolve(NOTICE))
    assert isinstance(event, cs.Stopped)
    assert event.reason is cs.StopReason.NOT_YET_VERIFIED
    if failure == "conflict":
        assert event.hard_stop is cs.HardStop.S7
    path = tmp_path / "failed.sqlite"
    with SqliteTransactionStore(path) as store:
        current = start(store)
        current = letter(store, current, event, effect_ids=("release-hold", "release-payment"))
        assert current.letter_state is cs.CaseState.STOPPED_NO_CHARGE
        assert store.flush_counters(occurred_at=NOW)
    with SqliteTransactionStore(path) as store:
        assert store.flush_counters(occurred_at=NOW)
    counts = Counter(k.milestone for k in coverage_rows(path))
    assert counts == {
        CounterMilestone.REQUEST_RECEIVED: 1,
        CounterMilestone.REQUIRED_FACTS_UNVERIFIED: 1,
    }
    denominator = (
        counts[CounterMilestone.REQUEST_RECEIVED] - counts[CounterMilestone.GENUINELY_UNSUPPORTED]
    )
    assert denominator == 1
