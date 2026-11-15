from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from enum import StrEnum

import pytest

from parkingbreaker import case_state
from parkingbreaker.case_state import HardStop
from parkingbreaker.research.findings import (
    Finding,
    FindingStatus,
    apply_quote_guard,
    apply_value_bounds,
    consolidate,
)
from parkingbreaker.research.ladder import (
    Ask,
    CountingChoice,
    CountingRule,
    CriticalFact,
    CustomerQuestion,
    Recover,
    Stop,
    StopReason,
)
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.resolution import (
    PendingResearch,
    RecoveryProgress,
    ResearchAnswers,
    ResearchResolution,
    VehicleRelationship,
    answer_question,
    official_address_candidates,
    resolution_event,
    resolution_steps,
    resolve_research,
)
from parkingbreaker.research.sources import AuthorityClass, SourceRecord
from samples import source
from test_research_findings import SOURCES, full_findings, guarded
from test_research_run import TICKET


def test_complete_evidence_has_no_resolution_steps() -> None:
    findings = guarded(full_findings())
    assert resolution_steps(findings, findings, SOURCES, RecoveryProgress()) == ()


def test_grouped_deadline_keys_produce_one_customer_question() -> None:
    findings = guarded(
        [
            f
            for f in full_findings()
            if f.key
            not in {
                FactKey.DEADLINE_DAYS,
                FactKey.DEADLINE_CLOCK_START,
            }
        ]
    )
    pending = resolution_steps(findings, findings, SOURCES, RecoveryProgress(True, True))
    assert len(pending) == 1
    assert pending[0].fact is CriticalFact.DEADLINE
    assert pending[0].keys == (FactKey.DEADLINE_DAYS, FactKey.DEADLINE_CLOCK_START)
    assert isinstance(pending[0].step, Ask)


def test_recovery_precedes_customer_questions_and_defaults() -> None:
    findings = guarded([f for f in full_findings() if f.key is not FactKey.DEADLINE_RULE])
    pending = resolution_steps(findings, findings, SOURCES, RecoveryProgress())
    assert isinstance(pending[0].step, Recover)
    pending = resolution_steps(findings, findings, SOURCES, RecoveryProgress(True, True))
    assert pending[0].step == Stop(StopReason.DEADLINE_NOT_ESTABLISHED, HardStop.S4)


def test_only_guarded_official_addresses_can_be_offered() -> None:
    honest = next(f for f in guarded(full_findings()) if f.key is FactKey.MAILING_ADDRESS)
    secondary_page = source("other address", AuthorityClass.SECONDARY)
    false = replace(honest, status=FindingStatus.UNKNOWN)
    secondary = replace(honest, source_sha256s=(secondary_page.sha256,))
    assert official_address_candidates(
        [false, secondary, honest, honest], [*SOURCES, secondary_page]
    ) == (honest,)


def test_address_conflict_preserves_both_official_options_after_consolidation() -> None:
    first = next(f for f in guarded(full_findings()) if f.key is FactKey.MAILING_ADDRESS)
    assert isinstance(first.value, dict)
    quote = (
        "Mail your contest to SFMTA Customer Service Center ATTN: Citation Review "
        "22 Main Street Oakland CA 94612"
    )
    page = source(quote, url="https://sfmta.com/second-address")
    second = Finding(
        FactKey.MAILING_ADDRESS,
        {**first.value, "line1": "22 Main Street", "city": "Oakland", "zip_code": "94612"},
        FindingStatus.CONFIRMED,
        quote,
        (page.sha256,),
    )
    pages = [*SOURCES, page]
    candidates = apply_value_bounds(apply_quote_guard([*full_findings(), second], pages))
    findings = consolidate(candidates)
    pending = resolution_steps(findings, candidates, pages, RecoveryProgress(True, True))
    assert len(pending) == 1
    assert isinstance(pending[0].step, Ask)
    assert len(pending[0].official_addresses) == 2


def test_fresh_single_official_address_needs_no_registry_cross_check() -> None:
    findings = guarded(full_findings())
    pending = resolution_steps(findings, findings, SOURCES, RecoveryProgress(True, True))
    assert pending == ()


# --- Customer answers and operational timing (never rewritten official findings) ---


def resolve(
    findings: tuple[Finding, ...], answers: ResearchAnswers | None = None
) -> ResearchResolution:
    return resolve_research(
        TICKET,
        findings,
        findings,
        SOURCES,
        RecoveryProgress(True, True),
        answers if answers is not None else ResearchAnswers(),
    )


def test_verified_research_resolves_with_customer_issue_date() -> None:
    resolution = resolve(guarded(full_findings()))
    assert resolution.ready
    assert resolution.deadline is not None
    assert resolution.deadline.deadline == date(2026, 10, 22)
    assert resolution.deadline.official == date(2026, 10, 22)
    assert resolution.deadline.notice is None
    assert not resolution.timing_disclosures


def test_printed_deadline_resolves_unknown_rule_without_fabricating_evidence() -> None:
    findings = guarded(
        [
            f
            for f in full_findings()
            if f.key
            not in {
                FactKey.DEADLINE_DAYS,
                FactKey.DEADLINE_CLOCK_START,
            }
        ]
    )
    answers = answer_question(
        ResearchAnswers(), CustomerQuestion.NOTICE_PRINTED_DEADLINE, date(2026, 10, 20)
    )
    before = findings
    resolution = resolve(findings, answers)
    assert resolution.ready
    assert resolution.deadline is not None
    assert resolution.deadline.deadline == date(2026, 10, 20)
    assert resolution.deadline.official is None
    assert resolution.deadline.notice == date(2026, 10, 20)
    assert resolution.customer_answers == answers
    assert resolution.timing_disclosures
    assert findings == before
    assert not any(f.key is FactKey.DEADLINE_DAYS for f in findings)


@pytest.mark.parametrize("notice", [date(2026, 10, 20), date(2026, 10, 25)])
def test_official_and_notice_deadlines_use_the_earlier_and_disclose(notice: date) -> None:
    answers = answer_question(ResearchAnswers(), CustomerQuestion.NOTICE_PRINTED_DEADLINE, notice)
    resolution = resolve(guarded(full_findings()), answers)
    assert resolution.ready
    assert resolution.deadline is not None
    assert resolution.deadline.deadline == min(notice, date(2026, 10, 22))
    assert resolution.deadline.disclose
    assert resolution.timing_disclosures


def test_unknown_counting_rule_safety_plan_does_not_pass_after_recovery() -> None:
    findings = guarded([f for f in full_findings() if f.key is not FactKey.DEADLINE_RULE])
    resolution = resolve(findings)
    assert not resolution.ready
    assert resolution.stops == (Stop(StopReason.DEADLINE_NOT_ESTABLISHED, HardStop.S4),)
    assert resolution.counting == CountingChoice(CountingRule.RECEIPT, True)
    assert resolution.timing_disclosures
    before_recovery = resolve_research(
        TICKET, findings, findings, SOURCES, RecoveryProgress(), ResearchAnswers()
    )
    assert not before_recovery.ready
    assert before_recovery.counting is None


def test_timing_default_cannot_skip_an_unspent_quote_re_extraction() -> None:
    findings = guarded(
        [
            replace(f, verbatim_quote="invented counting rule sentence")
            if f.key is FactKey.DEADLINE_RULE
            else f
            for f in full_findings()
        ]
    )
    resolution = resolve_research(
        TICKET, findings, findings, SOURCES, RecoveryProgress(False, True), ResearchAnswers()
    )
    assert not resolution.ready
    assert resolution.counting is None
    assert any(isinstance(p.step, Recover) for p in resolution.pending)


@pytest.mark.parametrize(
    "missing", [FactKey.MAIL_IN_ACCEPTED, FactKey.MAILING_ADDRESS, FactKey.REQUIRED_ITEMS]
)
def test_printed_deadline_cannot_waive_other_critical_requirements(missing: FactKey) -> None:
    findings = guarded([f for f in full_findings() if f.key is not missing])
    resolution = resolve(findings, ResearchAnswers(printed_deadline=date(2026, 10, 20)))
    assert not resolution.ready
    assert resolution.stops
    assert resolution.questions == ()


def test_questions_advance_after_customer_cannot_supply_an_answer_then_stop() -> None:
    findings = guarded([f for f in full_findings() if f.key is not FactKey.DEADLINE_DAYS])
    answers = ResearchAnswers()
    for question in [
        CustomerQuestion.NOTICE_ISSUE_DATE,
        CustomerQuestion.NOTICE_PRINTED_DEADLINE,
        CustomerQuestion.NOTICE_PRIOR_STEP,
    ]:
        resolution = resolve(findings, answers)
        assert resolution.questions == (Ask(question),)
        answers = answer_question(answers, question, None)
    resolution = resolve(findings, answers)
    assert not resolution.ready
    assert resolution.stops == (Stop(StopReason.DEADLINE_NOT_ESTABLISHED, HardStop.S4),)
    assert resolution.questions == ()


def test_customer_prior_step_requirement_stops_even_otherwise_complete_research() -> None:
    answers = answer_question(ResearchAnswers(), CustomerQuestion.NOTICE_PRIOR_STEP, True)
    resolution = resolve(guarded(full_findings()), answers)
    assert not resolution.ready
    assert resolution.stops == (Stop(StopReason.PRIOR_STEP_REQUIRED, HardStop.S3),)
    assert resolution.questions == ()


@pytest.mark.parametrize(
    ("question", "value"),
    [
        (CustomerQuestion.NOTICE_ISSUE_DATE, "2026-10-01"),
        (CustomerQuestion.NOTICE_PRINTED_DEADLINE, True),
        (CustomerQuestion.NOTICE_ISSUE_DATE, datetime(2026, 10, 1)),
        (CustomerQuestion.NOTICE_PRIOR_STEP, "no"),
        (CustomerQuestion.NOTICE_PRIOR_STEP, 1),
        (CustomerQuestion.NOTICE_ADDRESS_CHOICE, " "),
        (CustomerQuestion.NOTICE_ADDRESS_CHOICE, False),
        (CustomerQuestion.NOTICE_ISSUING_AGENCY, " "),
        (CustomerQuestion.NOTICE_STAGE, 3),
        (CustomerQuestion.DRAFT_FACT, "I parked there"),
    ],
)
def test_answers_are_typed_and_restricted_to_the_research_question_list(
    question: CustomerQuestion, value: object
) -> None:
    with pytest.raises(
        ValueError, match=r"notice dates|notice text|prior step|address choice|research-resolution"
    ):
        answer_question(ResearchAnswers(), question, value)  # type: ignore[arg-type]


def test_agency_and_stage_answers_are_recorded_and_can_be_declined() -> None:
    answers = answer_question(
        ResearchAnswers(), CustomerQuestion.NOTICE_ISSUING_AGENCY, "City of Springfield"
    )
    answers = answer_question(answers, CustomerQuestion.NOTICE_STAGE, None)
    assert answers.issuing_agency == "City of Springfield"
    assert answers.notice_stage is None
    assert answers.answered == {
        CustomerQuestion.NOTICE_ISSUING_AGENCY,
        CustomerQuestion.NOTICE_STAGE,
    }


def test_vehicle_relationship_choices_are_closed_and_have_stable_values() -> None:
    assert issubclass(VehicleRelationship, StrEnum)
    assert [choice.value for choice in VehicleRelationship] == [
        "REGISTERED_OWNER",
        "DRIVER_NOT_OWNER",
        "BUSINESS_OR_FLEET_VEHICLE",
        "RENTAL_OR_LEASE_RENTER",
        "AUTHORISED_REPRESENTATIVE",
        "OTHER",
    ]


@pytest.mark.parametrize("value", [None, *VehicleRelationship])
def test_vehicle_relationship_answer_records_choice_or_spends_declined_question(
    value: VehicleRelationship | None,
) -> None:
    original = answer_question(ResearchAnswers(), CustomerQuestion.NOTICE_STAGE, "Initial notice")
    answers = answer_question(original, CustomerQuestion.VEHICLE_RELATIONSHIP, value)
    assert answers.vehicle_relationship is value
    assert answers.answered == {
        CustomerQuestion.NOTICE_STAGE,
        CustomerQuestion.VEHICLE_RELATIONSHIP,
    }
    assert answers.notice_stage == original.notice_stage
    assert original.vehicle_relationship is None
    assert CustomerQuestion.VEHICLE_RELATIONSHIP not in original.answered
    # No standing research consumes this answer yet, even when declined or OTHER.
    resolution = resolve(guarded(full_findings()), answers)
    assert resolution.ready
    assert resolution.customer_answers == answers


@pytest.mark.parametrize(
    "value", ["REGISTERED_OWNER", "I own it", "", True, 1, CustomerQuestion.NOTICE_STAGE]
)
def test_vehicle_relationship_rejects_free_text_and_other_types(value: object) -> None:
    with pytest.raises(ValueError, match="vehicle relationship"):
        answer_question(ResearchAnswers(), CustomerQuestion.VEHICLE_RELATIONSHIP, value)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="vehicle relationship"):
        ResearchAnswers(vehicle_relationship=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("progress", [RecoveryProgress(), RecoveryProgress(True, True)])
def test_notice_based_clock_requires_the_customer_date(progress: RecoveryProgress) -> None:
    quote = "The deadline is 21 days from the notice date."
    page = source(quote)
    pages = [*SOURCES, page]
    candidates = apply_value_bounds(
        apply_quote_guard(
            [f for f in full_findings() if f.key is not FactKey.DEADLINE_CLOCK_START]
            + [
                Finding(
                    FactKey.DEADLINE_CLOCK_START,
                    "NOTICE_DATE",
                    FindingStatus.CONFIRMED,
                    quote,
                    (page.sha256,),
                )
            ],
            pages,
        )
    )
    before = resolve_research(TICKET, candidates, candidates, pages, progress, ResearchAnswers())
    assert not before.ready
    assert before.questions == (Ask(CustomerQuestion.NOTICE_ISSUE_DATE),)
    assert isinstance(resolution_event(before), case_state.CustomerNeeded)
    answers = answer_question(
        ResearchAnswers(), CustomerQuestion.NOTICE_ISSUE_DATE, date(2026, 10, 5)
    )
    after = resolve_research(TICKET, candidates, candidates, pages, progress, answers)
    assert after.ready
    assert after.deadline is not None
    assert after.deadline.deadline == date(2026, 10, 26)
    printed = resolve_research(
        TICKET,
        candidates,
        candidates,
        pages,
        progress,
        ResearchAnswers(printed_deadline=date(2026, 10, 20)),
    )
    assert printed.ready
    assert printed.deadline is not None
    assert printed.deadline.deadline == date(2026, 10, 20)
    assert printed.deadline.official is None


def conflicting_address_data() -> tuple[
    tuple[Finding, ...], tuple[Finding, ...], list[SourceRecord]
]:
    quote = (
        "Mail your contest to SFMTA Customer Service Center ATTN: Citation Review "
        "22 Main Street Oakland CA 94612"
    )
    page = source(quote, url="https://sfmta.com/second-address")
    first = next(f for f in guarded(full_findings()) if f.key is FactKey.MAILING_ADDRESS)
    assert isinstance(first.value, dict)
    second = Finding(
        FactKey.MAILING_ADDRESS,
        {**first.value, "line1": "22 Main Street", "city": "Oakland", "zip_code": "94612"},
        FindingStatus.CONFIRMED,
        quote,
        (page.sha256,),
    )
    pages = [*SOURCES, page]
    candidates = apply_value_bounds(apply_quote_guard([*full_findings(), second], pages))
    return consolidate(candidates), candidates, pages


def test_customer_address_selection_uses_original_official_finding() -> None:
    findings, candidates, pages = conflicting_address_data()
    chosen = "SFMTA Customer Service Center, Citation Review, 22 Main St., Oakland, CA 94612"
    answers = answer_question(ResearchAnswers(), CustomerQuestion.NOTICE_ADDRESS_CHOICE, chosen)
    resolution = resolve_research(
        TICKET, findings, candidates, pages, RecoveryProgress(True, True), answers
    )
    assert resolution.ready
    assert resolution.mailing_address is not None
    assert resolution.mailing_address in candidates
    assert isinstance(resolution.mailing_address.value, dict)
    assert resolution.mailing_address.value["line1"] == "22 Main Street"
    assert (
        next(f for f in findings if f.key is FactKey.MAILING_ADDRESS).status
        is FindingStatus.CONFLICTED
    )


def test_customer_cannot_introduce_a_new_address() -> None:
    findings, candidates, pages = conflicting_address_data()
    answers = answer_question(
        ResearchAnswers(), CustomerQuestion.NOTICE_ADDRESS_CHOICE, "11 Invented Street"
    )
    resolution = resolve_research(
        TICKET, findings, candidates, pages, RecoveryProgress(True, True), answers
    )
    assert not resolution.ready
    assert resolution.mailing_address is None
    assert resolution.stops == (Stop(StopReason.ADDRESS_CHOICE_NOT_OFFICIAL, HardStop.S7),)


def test_declining_to_choose_an_official_address_stops_without_reasking() -> None:
    findings, candidates, pages = conflicting_address_data()
    answers = answer_question(ResearchAnswers(), CustomerQuestion.NOTICE_ADDRESS_CHOICE, None)
    resolution = resolve_research(
        TICKET, findings, candidates, pages, RecoveryProgress(True, True), answers
    )
    assert not resolution.ready
    assert resolution.stops == (Stop(StopReason.MAILING_ADDRESS_NOT_CONFIRMED, HardStop.S7),)
    assert not resolution.questions


def test_resolution_event_moves_the_existing_letter_machine_and_releases_on_stop() -> None:
    complete = resolve(guarded(full_findings()))
    event = resolution_event(complete)
    assert isinstance(event, case_state.ResearchComplete)
    assert (
        case_state.transition(case_state.CaseState.RESEARCHING, event).target
        is case_state.CaseState.DRAFTING
    )

    findings = guarded([f for f in full_findings() if f.key is not FactKey.DEADLINE_DAYS])
    event = resolution_event(resolve(findings))
    assert event == case_state.CustomerNeeded(CustomerQuestion.NOTICE_ISSUE_DATE.value)
    assert (
        case_state.transition(case_state.CaseState.RESEARCHING, event).target
        is case_state.CaseState.NEEDS_CUSTOMER
    )

    findings = guarded([f for f in full_findings() if f.key is not FactKey.MAIL_IN_ACCEPTED])
    event = resolution_event(resolve(findings))
    assert isinstance(event, case_state.Stopped)
    stopped = case_state.transition(case_state.CaseState.RESEARCHING, event)
    assert stopped.target is case_state.CaseState.STOPPED_NO_CHARGE
    assert stopped.effects == (case_state.Effect.RELEASE_HOLD,)


def test_resolution_event_does_not_skip_unspent_automatic_recovery() -> None:
    findings = guarded([f for f in full_findings() if f.key is not FactKey.DEADLINE_RULE])
    resolution = resolve_research(
        TICKET, findings, findings, SOURCES, RecoveryProgress(), ResearchAnswers()
    )
    assert resolution_event(resolution) is None


def test_notice_date_before_the_citation_is_bad_input_not_a_new_deadline() -> None:
    quote = "The deadline is 21 days from the notice date."
    page = source(quote)
    pages = [*SOURCES, page]
    findings = apply_value_bounds(
        apply_quote_guard(
            [f for f in full_findings() if f.key is not FactKey.DEADLINE_CLOCK_START]
            + [
                Finding(
                    FactKey.DEADLINE_CLOCK_START,
                    "NOTICE_DATE",
                    FindingStatus.CONFIRMED,
                    quote,
                    (page.sha256,),
                )
            ],
            pages,
        )
    )
    with pytest.raises(ValueError, match="before issue_date"):
        resolve_research(
            TICKET,
            findings,
            findings,
            pages,
            RecoveryProgress(True, True),
            ResearchAnswers(notice_issue_date=date(2026, 9, 30)),
        )


def test_conflicting_official_deadlines_and_notice_preserve_all_candidates() -> None:
    quote = "Contests must arrive within 22 days of the citation issue date."
    page = source(quote, url="https://sfmta.com/other-rule")
    pages = [*SOURCES, page]
    candidates = apply_value_bounds(
        apply_quote_guard(
            [
                *full_findings(),
                Finding(
                    FactKey.DEADLINE_DAYS,
                    22,
                    FindingStatus.CONFIRMED,
                    quote,
                    (page.sha256,),
                ),
            ],
            pages,
        )
    )
    findings = consolidate(candidates)
    answers = answer_question(
        ResearchAnswers(), CustomerQuestion.NOTICE_PRINTED_DEADLINE, date(2026, 10, 25)
    )
    resolution = resolve_research(
        TICKET, findings, candidates, pages, RecoveryProgress(True, True), answers
    )
    assert resolution.ready
    assert resolution.official_deadline_candidates == (date(2026, 10, 22), date(2026, 10, 23))
    assert resolution.deadline is not None
    assert resolution.deadline.deadline == date(2026, 10, 22)
    assert resolution.timing_disclosures
    assert (
        next(f for f in findings if f.key is FactKey.DEADLINE_DAYS).status
        is FindingStatus.CONFLICTED
    )


def test_uncertain_trigger_remains_unverified_after_customer_questions() -> None:
    quote = "The deadline is 21 days from the notice date."
    page = source(quote, url="https://sfmta.com/notice-rule")
    pages = [*SOURCES, page]
    candidates = apply_value_bounds(
        apply_quote_guard(
            [
                *full_findings(),
                Finding(
                    FactKey.DEADLINE_CLOCK_START,
                    "NOTICE_DATE",
                    FindingStatus.CONFIRMED,
                    quote,
                    (page.sha256,),
                ),
            ],
            pages,
        )
    )
    findings = consolidate(candidates)
    answers = answer_question(
        ResearchAnswers(), CustomerQuestion.NOTICE_ISSUE_DATE, date(2026, 10, 5)
    )
    before = resolve_research(
        TICKET, findings, candidates, pages, RecoveryProgress(True, True), answers
    )
    assert not before.ready
    assert before.questions == (Ask(CustomerQuestion.NOTICE_PRINTED_DEADLINE),)
    answers = answer_question(answers, CustomerQuestion.NOTICE_PRINTED_DEADLINE, None)
    answers = answer_question(answers, CustomerQuestion.NOTICE_PRIOR_STEP, False)
    after = resolve_research(
        TICKET, findings, candidates, pages, RecoveryProgress(True, True), answers
    )
    assert not after.ready
    assert after.deadline is None
    assert after.stops == (Stop(StopReason.DEADLINE_NOT_ESTABLISHED, HardStop.S7),)


def test_notice_deadline_cannot_skip_automatic_recovery() -> None:
    findings = guarded([f for f in full_findings() if f.key is not FactKey.DEADLINE_DAYS])
    answers = ResearchAnswers(printed_deadline=date(2026, 10, 25))
    resolution = resolve_research(TICKET, findings, findings, SOURCES, RecoveryProgress(), answers)
    assert not resolution.ready
    assert resolution.deadline is None
    assert resolution_event(resolution) is None


@pytest.mark.parametrize(
    ("key", "hard_stop"),
    [
        (FactKey.MAIL_IN_ACCEPTED, HardStop.S1),
        (FactKey.MAILING_ADDRESS, HardStop.S2),
        (FactKey.REQUIRED_ITEMS, HardStop.S3),
        (FactKey.REQUIRED_DECLARATIONS, HardStop.S3),
        (FactKey.DEADLINE_DAYS, HardStop.S4),
        (FactKey.DEADLINE_CLOCK_START, HardStop.S4),
    ],
)
def test_exhausted_missing_fact_is_not_yet_verified(key: FactKey, hard_stop: HardStop) -> None:
    findings = tuple(f for f in guarded(full_findings()) if f.key is not key)
    answers = ResearchAnswers(answered=frozenset(CustomerQuestion))
    resolution = resolve(findings, answers)
    event = resolution_event(resolution)
    assert isinstance(event, case_state.Stopped)
    assert event.reason is case_state.StopReason.NOT_YET_VERIFIED
    assert event.hard_stop is hard_stop


def test_critical_conflict_exhausted_after_ladder_is_s7_not_process_incompatible() -> None:
    findings = tuple(
        replace(f, status=FindingStatus.CONFLICTED) if f.key is FactKey.MAIL_IN_ACCEPTED else f
        for f in guarded(full_findings())
    )
    event = resolution_event(resolve(findings))
    assert isinstance(event, case_state.Stopped)
    assert event.reason is case_state.StopReason.NOT_YET_VERIFIED
    assert event.hard_stop is HardStop.S7


def test_legacy_saved_prior_step_stop_emits_v7_s3_without_incompatibility_claim() -> None:
    resolution = ResearchResolution(
        None,
        None,
        (),
        None,
        (PendingResearch(CriticalFact.DEADLINE, (), Stop(StopReason.PRIOR_STEP_REQUIRED)),),
        ResearchAnswers(),
    )
    event = resolution_event(resolution)
    assert isinstance(event, case_state.Stopped)
    assert event.reason is case_state.StopReason.NOT_YET_VERIFIED
    assert event.hard_stop is HardStop.S3


# Retained procedural promotions only: customer defence/citation promotions are
# retired with defence research (audit A014-A017).
@pytest.mark.parametrize("trigger", ["form", "packet", "amount"])
@pytest.mark.parametrize("status", [FindingStatus.UNKNOWN, FindingStatus.CONFLICTED])
def test_promotion_flips_the_same_optional_gap_to_blocking(
    trigger: str, status: FindingStatus
) -> None:
    from parkingbreaker.research.findings import validate_tier1
    from parkingbreaker.research.questions import (
        DEFAULT_TIERS,
        CustomerPosition,
        FactTier,
        PacketPlan,
        RequirementEvidence,
        VerifiedRequirements,
        effective_tiers,
    )

    proof = RequirementEvidence(
        "A form requires the provision and fine amount.", (SOURCES[0].sha256,)
    )
    requirements = VerifiedRequirements(
        code_required=proof if trigger == "form" else None,
        amount_required=proof if trigger == "amount" else None,
    )
    position = CustomerPosition()
    plan = PacketPlan(code_required=proof if trigger == "packet" else None)
    key = FactKey.FINE_AMOUNT if trigger == "amount" else FactKey.GOVERNING_CODE_SECTION
    findings = [f for f in full_findings() if f.key is not key]
    findings.append(Finding(key, None, status, None, ()))
    assert validate_tier1(findings, SOURCES).proceed
    tiers = effective_tiers(TICKET, requirements, position, plan)
    assert tiers == effective_tiers(TICKET, requirements, position, plan)
    assert all(
        tiers.tiers[k] is FactTier.MUST_HAVE
        for k in DEFAULT_TIERS
        if DEFAULT_TIERS[k] is FactTier.MUST_HAVE
    )
    assert tiers.tiers[key] is FactTier.MUST_HAVE
    assert tiers.promotions
    assert tiers.promotions[0].key is key
    assert tiers.promotions[0].reason
    assert tiers.promotions[0].evidence == proof
    result = validate_tier1(findings, SOURCES, tiers=tiers)
    assert not result.proceed
    assert [p.key for p in result.reasons] == [key]
    resolution = resolve_research(
        TICKET,
        findings,
        findings,
        SOURCES,
        RecoveryProgress(True, True),
        ResearchAnswers(),
        verified_requirements=requirements,
        customer_position=position,
        packet_plan=plan,
    )
    assert not resolution.ready
    assert resolution.stops == (
        Stop(
            StopReason.REQUIRED_ITEMS_NOT_CONFIRMED,
            HardStop.S7 if status is FindingStatus.CONFLICTED else HardStop.S3,
        ),
    )


def test_new_compulsory_condition_cannot_be_hidden_by_a_confirmed_empty_list() -> None:
    from parkingbreaker.research.findings import validate_tier1
    from parkingbreaker.research.questions import (
        PacketPlan,
        RequirementEvidence,
        VerifiedRequirements,
        effective_tiers,
    )

    quote = "You must include a delivery affidavit."
    page = source(quote)
    proof = RequirementEvidence(quote, (page.sha256,))
    tiers = effective_tiers(TICKET, VerifiedRequirements(compulsory_conditions=(proof,)))
    result = validate_tier1(full_findings(), SOURCES, tiers=tiers)
    assert not result.proceed
    assert [p.key for p in result.reasons] == [FactKey.TICKET_SPECIFIC_REQUIREMENTS]
    checked = [f for f in full_findings() if f.key is not FactKey.TICKET_SPECIFIC_REQUIREMENTS]
    checked.append(
        Finding(
            FactKey.TICKET_SPECIFIC_REQUIREMENTS,
            [quote],
            FindingStatus.CONFIRMED,
            quote,
            (page.sha256,),
        )
    )
    assert not validate_tier1(checked, [*SOURCES, page], tiers=tiers).proceed
    assert not validate_tier1(
        checked,
        [*SOURCES, page],
        tiers=tiers,
        packet_plan=PacketPlan(fulfilled_items=frozenset({quote})),
    ).proceed


def test_evidenced_absolute_date_resolves_without_day_count_or_clock_start() -> None:
    from test_research_findings import absolute_findings

    findings, pages = absolute_findings()
    resolution = resolve_research(
        TICKET, findings, findings, pages, RecoveryProgress(), ResearchAnswers()
    )
    assert resolution.ready
    assert resolution.deadline is not None
    assert resolution.deadline.deadline == date(2026, 11, 15)
    assert not any(f.key is FactKey.DEADLINE_DAYS for f in findings)
