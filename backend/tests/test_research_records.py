from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import date

import pytest

from parkingbreaker.case_state import HardStop
from parkingbreaker.research.findings import Finding, FindingStatus
from parkingbreaker.research.ladder import (
    Ask,
    CriticalFact,
    CustomerQuestion,
    Recover,
    RecoveryAction,
    Stop,
    StopReason,
)
from parkingbreaker.research.questions import ANSWERABLE_KEYS, CRITICAL_KEYS, FactKey
from parkingbreaker.research.resolution import (
    PendingResearch,
    RecoveryProgress,
    ResearchAnswers,
    ResearchResolution,
    VehicleRelationship,
    answer_question,
)
from parkingbreaker.research.run import ResearchAttempt, ResearchResult, run_research
from parkingbreaker.research_records import (
    answers_json,
    decode_answers,
    decode_research,
    decode_resolution,
    research_json,
    resolution_json,
)
from test_research_findings import full_findings, guarded
from test_research_run import SEEDS, TEST_AGENCY, TICKET, TickingClock, answers, fetcher


def result_fixture() -> ResearchResult:
    result = run_research(
        TICKET, TEST_AGENCY, SEEDS, fetcher(), answers(), TickingClock(), wait=lambda _: None
    )
    # Retain a conflict and unfinished recovery evidence in the same saved record.
    source = result.sources[0]
    conflict = Finding(
        FactKey.DEADLINE_DAYS,
        30,
        FindingStatus.CONFIRMED,
        "within 21 calendar days of the citation",
        (source.sha256,),
        "retained conflicting extraction",
    )
    return replace(
        result,
        failures=(),
        candidates=(*result.candidates, conflict),
        progress=RecoveryProgress(re_extraction_done=True, broadened_search_done=False),
        pending=(
            PendingResearch(
                CriticalFact.DEADLINE,
                (FactKey.DEADLINE_DAYS,),
                Recover(RecoveryAction.BROADENED_SEARCH),
            ),
        ),
        attempts=(
            *result.attempts,
            ResearchAttempt("fetch_retry", source.url, "temporary failure"),
        ),
        address_mismatch=True,
    )


def test_reencode_retains_a_stored_retired_finding() -> None:
    base = result_fixture()
    source = base.sources[0]
    retired = Finding(
        FactKey.CODE_EXCEPTIONS,
        ["loading"],
        FindingStatus.CONFIRMED,
        "the sign said no loading",
        (source.sha256,),
    )
    with_retired = replace(base, findings=(*base.findings, retired))
    decoded = decode_research(research_json(with_retired))
    assert FactKey.CODE_EXCEPTIONS in [f.key for f in decoded.findings]
    # A second save keeps the history and does not shuffle the rest of the record.
    saved = research_json(decoded)
    assert "CODE_EXCEPTIONS" in saved
    assert [f.key for f in decode_research(saved).findings] == [f.key for f in decoded.findings]


def test_retired_finding_is_not_invented_when_absent() -> None:
    decoded = decode_research(research_json(result_fixture()))
    assert FactKey.CODE_EXCEPTIONS not in [f.key for f in decoded.findings]


def test_research_round_trip_preserves_all_material_evidence_and_canonical_json() -> None:
    original = result_fixture()
    encoded = research_json(original)
    decoded = decode_research(encoded)
    assert decoded == original
    assert research_json(decoded) == encoded
    assert decoded.sources[0].text == original.sources[0].text
    assert decoded.sources[0].sha256 == original.sources[0].sha256
    assert decoded.candidates[-1].status is FindingStatus.CONFIRMED
    assert decoded.pending[0].step == Recover(RecoveryAction.BROADENED_SEARCH)
    assert decoded.progress == RecoveryProgress(True, False)
    assert decoded.attempts[-1].error == "temporary failure"
    assert decoded.address_mismatch


def test_answers_distinguish_customer_answers_and_unanswered_defaults() -> None:
    answers = answer_question(
        ResearchAnswers(), CustomerQuestion.NOTICE_ISSUE_DATE, date(2026, 10, 5)
    )
    answers = answer_question(answers, CustomerQuestion.NOTICE_PRINTED_DEADLINE, None)
    assert decode_answers(answers_json(answers)) == answers
    assert decode_answers(answers_json(ResearchAnswers())) == ResearchAnswers()


def test_agency_and_stage_answers_round_trip_and_legacy_payload_is_unchanged() -> None:
    answers = answer_question(
        ResearchAnswers(), CustomerQuestion.NOTICE_ISSUING_AGENCY, "City of Springfield"
    )
    answers = answer_question(answers, CustomerQuestion.NOTICE_STAGE, "Notice of Delinquency")
    assert decode_answers(answers_json(answers)) == answers
    assert "issuing_agency" not in answers_json(ResearchAnswers())
    assert "notice_stage" not in answers_json(ResearchAnswers())


@pytest.mark.parametrize("value", [None, *VehicleRelationship])
def test_vehicle_relationship_round_trips_in_answers_and_resolution(
    value: VehicleRelationship | None,
) -> None:
    answers = answer_question(ResearchAnswers(), CustomerQuestion.VEHICLE_RELATIONSHIP, value)
    encoded = answers_json(answers)
    payload = json.loads(encoded)["payload"]
    if value is None:
        assert "vehicle_relationship" not in payload
    else:
        assert payload["vehicle_relationship"] == value.value
    decoded = decode_answers(encoded)
    assert decoded == answers
    assert decoded.vehicle_relationship is value
    assert CustomerQuestion.VEHICLE_RELATIONSHIP in decoded.answered
    assert answers_json(decoded) == encoded
    resolution = ResearchResolution(None, None, (), None, (), answers)
    encoded_resolution = resolution_json(resolution)
    assert decode_resolution(encoded_resolution) == resolution
    assert resolution_json(decode_resolution(encoded_resolution)) == encoded_resolution


def test_vehicle_relationship_addition_preserves_legacy_canonical_bytes() -> None:
    # Captured from the pre-standing codec, including verbatim text and a declined answer.
    legacy = (
        b'{"kind":"answers","payload":{"address_choice":null,"answered":'
        b'["NOTICE_ISSUING_AGENCY","NOTICE_PRINTED_DEADLINE","NOTICE_STAGE"],'
        b'"issuing_agency":"  City of Springfield  ","notice_issue_date":null,'
        b'"notice_stage":"Notice of Delinquency","printed_deadline":null,'
        b'"prior_step_required":null},"version":1}'
    )
    answers = answer_question(
        ResearchAnswers(), CustomerQuestion.NOTICE_ISSUING_AGENCY, "  City of Springfield  "
    )
    answers = answer_question(answers, CustomerQuestion.NOTICE_STAGE, "Notice of Delinquency")
    answers = answer_question(answers, CustomerQuestion.NOTICE_PRINTED_DEADLINE, None)
    assert answers_json(answers).encode() == legacy
    decoded = decode_answers(legacy.decode())
    assert decoded == answers
    assert decoded.vehicle_relationship is None
    assert answers_json(decoded).encode() == legacy


@pytest.mark.parametrize("value", ["UNKNOWN_RELATIONSHIP", "registered_owner", "", None, True, 1])
def test_vehicle_relationship_codec_rejects_unknown_or_invalid_present_value(value: object) -> None:
    document = json.loads(answers_json(ResearchAnswers()))
    document["payload"]["vehicle_relationship"] = value
    with pytest.raises(ValueError, match=r"answers\.vehicle_relationship"):
        decode_answers(json.dumps(document))
    resolution = ResearchResolution(None, None, (), None, (), ResearchAnswers())
    document = json.loads(resolution_json(resolution))
    document["payload"]["customer_answers"]["vehicle_relationship"] = value
    with pytest.raises(ValueError, match=r"answers\.vehicle_relationship"):
        decode_resolution(json.dumps(document))


def test_resolution_round_trip_retains_customer_and_disclosed_default_separately() -> None:
    finding = guarded(full_findings())[0]
    resolution = ResearchResolution(
        mailing_address=finding,
        deadline=None,
        official_deadline_candidates=(date(2026, 10, 22), date(2026, 10, 25)),
        counting=None,
        pending=(
            PendingResearch(
                CriticalFact.DEADLINE,
                (FactKey.DEADLINE_DAYS,),
                Ask(CustomerQuestion.NOTICE_ISSUE_DATE),
            ),
        ),
        customer_answers=ResearchAnswers(
            notice_issue_date=date(2026, 10, 3),
            answered=frozenset({CustomerQuestion.NOTICE_ISSUE_DATE}),
        ),
        timing_disclosures=("Used the earlier verified candidate.",),
    )
    assert decode_resolution(resolution_json(resolution)) == resolution


def test_decoded_json_values_do_not_alias_input_or_encoded_document() -> None:
    original = result_fixture()
    encoded = research_json(original)
    decoded = decode_research(encoded)
    finding = next(item for item in decoded.findings if item.key is FactKey.MAILING_ADDRESS)
    value = finding.value
    assert isinstance(value, dict)
    value["caller mutation"] = [1]
    original_finding = next(
        item for item in original.findings if item.key is FactKey.MAILING_ADDRESS
    )
    assert isinstance(original_finding.value, dict)
    assert "caller mutation" not in original_finding.value
    reread = next(
        item for item in decode_research(encoded).findings if item.key is FactKey.MAILING_ADDRESS
    )
    assert reread.value == original_finding.value


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(extra=True),
        lambda data: data.pop("address_mismatch"),
        lambda data: data.update(cost={"input_tokens": True, "output_tokens": 0, "cost_cents": 0}),
        lambda data: data.update(started_at="2026-10-01T00:00:00"),
        lambda data: data.update(finished_at="2026-09-30T00:00:00+00:00"),
        lambda data: data.update(
            progress={"re_extraction_done": 0, "broadened_search_done": False}
        ),
    ],
)
def test_research_decoder_rejects_extra_missing_and_invalid_values(
    mutation: Callable[[dict[str, object]], object],
) -> None:
    document = json.loads(research_json(result_fixture()))
    mutation(document["payload"])
    with pytest.raises(ValueError, match=r"."):
        decode_research(json.dumps(document))


def test_decoder_rejects_duplicate_keys_unknown_version_and_nonfinite_numbers() -> None:
    encoded = research_json(result_fixture())
    with pytest.raises(ValueError, match="duplicate"):
        decode_research('{"version":1,"version":1,"kind":"research","payload":{}}')
    with pytest.raises(ValueError, match="version"):
        decode_research(encoded.replace('"version":4', '"version":5', 1))
    with pytest.raises(ValueError, match="invalid JSON number"):
        decode_answers('{"version":1,"kind":"answers","payload":{"notice_issue_date":NaN}}')


def test_usage_and_hash_provenance_fail_closed() -> None:
    document = json.loads(research_json(result_fixture()))
    document["payload"]["sources"][0]["sha256"] = "invalid"
    with pytest.raises(ValueError, match="hash"):
        decode_research(json.dumps(document))
    document = json.loads(research_json(result_fixture()))
    document["payload"]["findings"][0]["source_sha256s"] = ["0" * 64]
    with pytest.raises(ValueError, match="provenance"):
        decode_research(json.dumps(document))
    document = json.loads(research_json(result_fixture()))
    document["payload"]["cost"] = {"input_tokens": -1, "output_tokens": 0, "cost_cents": 0}
    with pytest.raises(ValueError, match="integer"):
        decode_research(json.dumps(document))


def test_answers_rejects_boolean_as_date_and_invalid_enum() -> None:
    with pytest.raises(ValueError, match=r"."):
        decode_answers(
            '{"version":1,"kind":"answers","payload":{"notice_issue_date":true,"printed_deadline":null,"address_choice":null,"prior_step_required":null,"answered":[]}}'
        )
    with pytest.raises(ValueError, match=r"."):
        decode_answers(
            '{"version":1,"kind":"answers","payload":{"notice_issue_date":null,"printed_deadline":null,"address_choice":null,"prior_step_required":null,"answered":["NOT_A_QUESTION"]}}'
        )


def test_mirror_sources_with_identical_body_hash_both_round_trip() -> None:
    original = result_fixture()
    first = original.sources[0]
    mirror = replace(
        first,
        url="https://mirror.fixture-city.gov/contest",
        final_url="https://mirror.fixture-city.gov/contest",
    )
    with_mirror = replace(original, sources=(*original.sources, mirror))
    decoded = decode_research(research_json(with_mirror))
    assert decoded == with_mirror
    assert decoded.sources[-1].sha256 == decoded.sources[0].sha256
    assert decoded.sources[-1].url == mirror.url


def test_nonconfirmed_candidates_preserve_diagnostic_and_duplicate_refs() -> None:
    original = result_fixture()
    source = original.sources[0]
    unknown = Finding(
        FactKey.FINE_AMOUNT,
        None,
        FindingStatus.UNKNOWN,
        None,
        ("extractor-unfetched-reference", "extractor-unfetched-reference", source.sha256),
        "quote guard: no cited source was fetched",
    )
    conflicted = Finding(
        FactKey.DEADLINE_DAYS,
        None,
        FindingStatus.CONFLICTED,
        None,
        ("malformed-diagnostic-reference",),
        "conflicting values retained",
    )
    with_diagnostics = replace(original, candidates=(*original.candidates, unknown, conflicted))
    decoded = decode_research(research_json(with_diagnostics))
    assert decoded == with_diagnostics
    assert decoded.candidates[-2].source_sha256s == unknown.source_sha256s
    assert decoded.candidates[-1].source_sha256s == conflicted.source_sha256s


def test_confirmed_duplicate_valid_citations_are_preserved() -> None:
    original = result_fixture()
    finding = original.findings[0]
    duplicate = replace(finding, source_sha256s=(finding.source_sha256s[0],) * 2)
    with_duplicates = replace(
        original, findings=(duplicate,), candidates=(), pending=(), record_version=1
    )
    decoded = decode_research(research_json(with_duplicates))
    assert decoded.findings[0].source_sha256s == duplicate.source_sha256s


def test_confirmed_malformed_hash_is_rejected() -> None:
    original = result_fixture()
    finding = replace(original.findings[0], source_sha256s=("malformed-reference",))
    malformed = replace(original, findings=(finding, *original.findings[1:]))
    with pytest.raises(ValueError, match="invalid hash"):
        research_json(malformed)


@pytest.mark.parametrize("hard_stop", [None, *HardStop])
def test_ladder_stop_additive_codec_preserves_legacy_and_new_bytes(
    hard_stop: HardStop | None,
) -> None:
    pending = PendingResearch(
        CriticalFact.MAILING_ADDRESS,
        (FactKey.MAILING_ADDRESS,),
        Stop(StopReason.MAILING_ADDRESS_NOT_CONFIRMED, hard_stop),
    )
    result = replace(result_fixture(), pending=(pending,))
    encoded = research_json(result)
    step = json.loads(encoded)["payload"]["pending"][0]["step"]
    if hard_stop is None:
        assert step == {"type": "stop", "reason": "MAILING_ADDRESS_NOT_CONFIRMED"}
    else:
        assert step["hard_stop"] == hard_stop.value
    assert research_json(decode_research(encoded)) == encoded
    resolution = ResearchResolution(None, None, (), None, (pending,), ResearchAnswers())
    encoded_resolution = resolution_json(resolution)
    assert resolution_json(decode_resolution(encoded_resolution)) == encoded_resolution


@pytest.mark.parametrize("value", [None, "S8", "", 1])
def test_ladder_codec_rejects_present_invalid_hard_stop(value: object) -> None:
    result = replace(
        result_fixture(),
        pending=(
            PendingResearch(
                CriticalFact.MAILING_ADDRESS,
                (FactKey.MAILING_ADDRESS,),
                Stop(StopReason.MAILING_ADDRESS_NOT_CONFIRMED),
            ),
        ),
    )
    document = json.loads(research_json(result))
    document["payload"]["pending"][0]["step"]["hard_stop"] = value
    with pytest.raises(ValueError, match="hard_stop"):
        decode_research(json.dumps(document))


# Retained procedural promotions only: defence and customer-citation promotions are retired.
@pytest.mark.parametrize("trigger", ["form", "packet", "amount", "condition"])
def test_saved_promotions_and_inputs_survive_readback_and_still_block(trigger: str) -> None:
    from parkingbreaker.research.questions import (
        CustomerPosition,
        FactTier,
        PacketPlan,
        RequirementEvidence,
        VerifiedRequirements,
    )

    original = result_fixture()
    proof = RequirementEvidence(
        "You must provide the code and deposit.", (original.sources[0].sha256,)
    )
    position = CustomerPosition()
    requirements = VerifiedRequirements(
        code_required=proof if trigger == "form" else None,
        amount_required=proof if trigger == "amount" else None,
        compulsory_conditions=(proof,) if trigger == "condition" else (),
    )
    plan = PacketPlan(code_required=proof if trigger == "packet" else None)
    key = (
        FactKey.FINE_AMOUNT
        if trigger == "amount"
        else FactKey.TICKET_SPECIFIC_REQUIREMENTS
        if trigger == "condition"
        else FactKey.GOVERNING_CODE_SECTION
    )
    original = replace(original, findings=tuple(f for f in original.findings if f.key is not key))
    current = original.reassess(
        verified_requirements=requirements, customer_position=position, packet_plan=plan
    )
    decoded = decode_research(research_json(current))
    assert decoded == current
    assert current.tier_evaluation is not None
    assert decoded.tier_evaluation == current.tier_evaluation
    assert decoded.tier_evaluation is not None
    assert decoded.tier_evaluation.tiers[key] is FactTier.MUST_HAVE
    if trigger == "condition":
        assert decoded.tier_evaluation.compulsory_gaps == (proof,)
    else:
        assert decoded.tier_evaluation.promotions[0].reason
        assert (
            decoded.tier_evaluation.promotions[0].evidence
            == current.tier_evaluation.promotions[0].evidence
        )
    assert not decoded.decision.ok
    assert not decoded.resolve().ready
    assert research_json(decoded) == research_json(current)


def test_legacy_research_records_keep_bytes_but_missing_answers_block_evaluation() -> None:
    from parkingbreaker.research.questions import Ticket, build_questions

    old = result_fixture()
    legacy_ticket = Ticket(
        old.ticket.city_id,
        old.ticket.violation_code,
        old.ticket.violation_description,
        old.ticket.issue_date,
    )
    old_keys = set(list(FactKey)[:10])
    legacy = replace(
        old,
        ticket=legacy_ticket,
        record_version=1,
        tier_evaluation=None,
        questions=tuple(q for q in build_questions(legacy_ticket) if q.key in old_keys),
        candidates=(),
        findings=tuple(
            replace(
                f, unknown_reason=None, jurisdiction=None, stage=None, evidence=(), scope_quote=None
            )
            for f in old.findings
            if f.key in old_keys
        ),
    )
    encoded = research_json(legacy)
    raw = json.loads(encoded)
    assert raw["version"] == 1
    assert "tier_context" not in raw["payload"]
    assert set(raw["payload"]["ticket"]) == {
        "city_id",
        "violation_code",
        "violation_description",
        "issue_date",
    }
    assert all(
        set(f) == {"key", "status", "value", "verbatim_quote", "source_sha256s", "notes"}
        for f in raw["payload"]["findings"]
    )
    decoded = decode_research(encoded)
    assert decoded == legacy
    assert research_json(decoded) == encoded
    assert len(decoded.findings) == len(legacy.findings)
    # The saved decision is historical data, never authorization to skip today's validator.
    assert decoded.decision.ok
    assert not decoded.resolve().ready
    evaluated = decoded.reassess()
    assert len(evaluated.findings) == len(ANSWERABLE_KEYS)
    missing = [f for f in evaluated.findings if f.key not in old_keys]
    assert all(f.status is FindingStatus.UNKNOWN and f.value is None for f in missing)
    assert not evaluated.decision.ok
    assert research_json(decoded) == encoded


@pytest.mark.parametrize("status", list(FindingStatus))
def test_version2_roundtrip_preserves_status_quotes_hashes_notes_and_reasons(
    status: FindingStatus,
) -> None:
    from parkingbreaker.research.findings import UnknownReason

    original = result_fixture()
    page = original.sources[0]
    answer = Finding(
        FactKey.FINE_AMOUNT,
        None,
        status,
        "not used as proof",
        (page.sha256,),
        "saved diagnostic",
        UnknownReason.UNANSWERABLE if status is FindingStatus.UNKNOWN else None,
    )
    result = replace(
        original, findings=tuple(answer if f.key is answer.key else f for f in original.findings)
    )
    decoded = decode_research(research_json(result))
    assert decoded == result
    assert next(f for f in decoded.findings if f.key is answer.key) == answer


@pytest.mark.parametrize("key", sorted(CRITICAL_KEYS))
def test_missing_tier1_cannot_become_passing_through_version2_serialization(key: FactKey) -> None:
    from parkingbreaker.research.findings import UnknownReason, validate_tier1

    original = result_fixture()
    incomplete = replace(
        original,
        findings=tuple(f for f in original.findings if f.key is not FactKey.MANDATORY_FORMS),
    )
    decoded = decode_research(research_json(incomplete))
    f = next(f for f in decoded.findings if f.key is FactKey.MANDATORY_FORMS)
    assert f.status is FindingStatus.UNKNOWN
    assert f.value is None
    assert f.unknown_reason is UnknownReason.MISSING_ANSWER
    assert not validate_tier1(decoded.findings, decoded.sources).proceed
    assert not decoded.resolve().ready


def test_conflict_roundtrip_keeps_both_values_quotes_and_page_hashes() -> None:
    from parkingbreaker.research.findings import consolidate
    from samples import source

    original = result_fixture()
    quotes = ("Contest within 21 days.", "Contest within 30 days.")
    pages = tuple(source(q, url=f"https://sfmta.com/{i}") for i, q in enumerate(quotes))
    claims = [
        Finding(FactKey.DEADLINE_DAYS, days, FindingStatus.CONFIRMED, page.text, (page.sha256,))
        for days, page in zip((21, 30), pages, strict=True)
    ]
    (conflict,) = consolidate(claims)
    result = replace(
        original,
        sources=(*original.sources, *pages),
        findings=tuple(
            conflict if f.key is FactKey.DEADLINE_DAYS else f for f in original.findings
        ),
    )
    decoded = decode_research(research_json(result))
    f = next(f for f in decoded.findings if f.key is FactKey.DEADLINE_DAYS)
    assert f.status is FindingStatus.CONFLICTED
    assert f.evidence == tuple(claims)
    assert set(f.source_sha256s) == {p.sha256 for p in pages}
    assert not decoded.resolve().ready
