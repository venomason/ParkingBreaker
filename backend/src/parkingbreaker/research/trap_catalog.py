"""T21 trap catalogue: the trap classes research guards against, and the ones it does not.

Pure data transcribed from
build-kit/evidence/task-T21-2026-10-22/trap-catalogue-table-mimo.md. One TrapClass per
row of the guard table, in table order. Guard names are the table's dotted names with
the ``parkingbreaker.`` prefix added; a bare symbol the table cites beside a guard (its
helper or constant) is recorded under that guard's module. Veto codes the table cites in
plain parentheses (for example PAYMENT_AS_CONTEST) annotate which veto fires and are not
symbols here. Fixtures are J-case ids and pytest node ids under ``backend/``.
"""

from __future__ import annotations

from dataclasses import dataclass

from parkingbreaker.research.questions import FactKey

_ALL_FACT_KEYS: tuple[FactKey, ...] = (
    FactKey.MAIL_IN_ACCEPTED,
    FactKey.GOVERNING_CODE_SECTION,
    FactKey.DEADLINE_DAYS,
    FactKey.DEADLINE_CLOCK_START,
    FactKey.DEADLINE_RULE,
    FactKey.MAILING_ADDRESS,
    FactKey.REQUIRED_ITEMS,
    FactKey.REQUIRED_DECLARATIONS,
    FactKey.FINE_AMOUNT,
    FactKey.CODE_EXCEPTIONS,
    FactKey.CONTEST_ELIGIBILITY,
    FactKey.MANDATORY_FORMS,
    FactKey.SIGNATURE_REQUIREMENT,
    FactKey.PRIOR_STEP_REQUIREMENT,
    FactKey.TICKET_SPECIFIC_REQUIREMENTS,
    FactKey.DEADLINE_ABSOLUTE,
)


@dataclass(frozen=True)
class TrapClass:
    class_id: str
    description: str
    fact_keys: tuple[FactKey, ...]
    guards: tuple[str, ...]
    fixtures: tuple[str, ...]


@dataclass(frozen=True)
class UnguardedTrap:
    class_id: str
    where_named: str
    planned_unit: str


GUARDED: tuple[TrapClass, ...] = (
    TrapClass(
        class_id="payment_address_as_contest",
        description="payment/waiver box address offered as contest destination",
        fact_keys=(FactKey.MAILING_ADDRESS,),
        guards=(
            "parkingbreaker.research.support.address_purpose_problem",
            "parkingbreaker.research.support.value_supported_by_quote",
        ),
        fixtures=(
            "J07",
            "tests/test_research_support.py::test_la_prepayment_waiver_box_is_not_a_contest_destination",
            "tests/test_research_support.py::test_address_without_a_sole_contest_purpose_is_unsupported",
        ),
    ),
    TrapClass(
        class_id="customer_payment_lockbox_input",
        description="customer types the ticket's payment/lockbox address as appeal address",
        fact_keys=(FactKey.MAILING_ADDRESS,),
        guards=(
            "parkingbreaker.address_policy.payment_addresses_in_text",
            "parkingbreaker.address_policy.payment_address_matches",
        ),
        fixtures=(
            "tests/test_t30_payment_address.py::test_payment_addresses_read_from_payment_wording_only",
            "tests/test_t30_payment_address.py::test_address_shared_by_payment_and_appeal_is_not_a_payment_address",
        ),
    ),
    TrapClass(
        class_id="payment_link_as_contest_route",
        description="payment link cannot establish a contest route",
        fact_keys=(FactKey.MAIL_IN_ACCEPTED, FactKey.MAILING_ADDRESS),
        guards=(
            "parkingbreaker.research.link_assessment._veto_reason",
            "parkingbreaker.research.delegation.decide_delegation",
        ),
        fixtures=(
            "tests/test_research_link_assessment.py::test_payment_link_cannot_become_contest_even_with_valid_ai_quote",
            "tests/test_research_issuer_resolution.py::test_payment_link_cannot_delegate_even_if_ai_calls_it_contest",
        ),
    ),
    TrapClass(
        class_id="non_issuer_as_decider",
        description="payment body / private operator / vendor page taken as adjudicator",
        fact_keys=(),
        guards=("parkingbreaker.research.issuer_assessment.resolve_issuer",),
        fixtures=(
            "J09",
            "J26",
            "tests/test_research_issuer_assessment.py::test_payment_or_explicit_nondeciding_role_cannot_become_adjudicator",
        ),
    ),
    TrapClass(
        class_id="delegated_root_laundering",
        description="delegated tenant cannot authenticate a new root",
        fact_keys=(),
        guards=("parkingbreaker.research.delegation.decide_delegation",),
        fixtures=(
            "J11",
            "J25",
            "tests/test_research_delegation.py::test_nondotgov_page_and_delegated_tenant_cannot_authenticate_new_root",
        ),
    ),
    TrapClass(
        class_id="wrong_tenant_query",
        description="?tenant= link treated as same-agency page",
        fact_keys=(FactKey.MAIL_IN_ACCEPTED, FactKey.MAILING_ADDRESS),
        guards=("parkingbreaker.research.delegation.decide_delegation",),
        fixtures=(
            "J11",
            "tests/test_research_veto_codes.py::test_every_delegation_veto_has_stable_code",
        ),
    ),
    TrapClass(
        class_id="non_dotgov_delegation",
        description="delegation accepted off a non-.gov / unsupported root",
        fact_keys=(),
        guards=("parkingbreaker.research.delegation.decide_delegation",),
        fixtures=(
            "J10",
            "tests/test_research_veto_codes.py::test_every_delegation_veto_has_stable_code",
        ),
    ),
    TrapClass(
        class_id="unsafe_link_url",
        description="javascript:/query/root-path/redirect hrefs as source scopes",
        fact_keys=(),
        guards=(
            "parkingbreaker.research.delegation._unsafe_reference",
            "parkingbreaker.research.delegation.decide_delegation",
        ),
        fixtures=(
            "tests/test_research_delegation.py::test_unsafe_paths_queries_and_urls_are_refused",
        ),
    ),
    TrapClass(
        class_id="wrong_stage_instructions",
        description="another-stage instructions laundered into this stage",
        fact_keys=(),
        guards=(
            "parkingbreaker.research.procedure.same_stage",
            "parkingbreaker.research.findings.apply_quote_guard",
            "parkingbreaker.research.link_assessment._veto_reason",
        ),
        fixtures=(
            "tests/test_research_issuer_assessment.py::test_second_stage_body_quote_cannot_control_initial_review",
            "tests/test_research_link_assessment.py::test_stage_terms_veto_mismatches_only_when_both_texts_are_given",
        ),
    ),
    TrapClass(
        class_id="deadline_number_wrong_purpose",
        description="late-fee/other number backs the contest period",
        fact_keys=(FactKey.DEADLINE_DAYS,),
        guards=(
            "parkingbreaker.research.support.value_supported_by_quote",
            "parkingbreaker.research.support._nearest_purpose_is_other",
        ),
        fixtures=(
            "J12",
            "tests/test_research_support.py::test_45_is_not_supported_by_a_21_day_quote",
            "tests/test_research_support.py::test_deadline_number_is_bound_to_its_purpose_and_period",
        ),
    ),
    TrapClass(
        class_id="deadline_unit_mismatch",
        description="business/working days read as calendar (or reverse)",
        fact_keys=(FactKey.DEADLINE_DAYS,),
        guards=(
            "parkingbreaker.research.support.value_supported_by_quote",
            "parkingbreaker.mail_timing.parse_deadline_period",
        ),
        fixtures=(
            "tests/test_t21_business_days.py::test_unqualified_days_never_support_business_unit",
            "tests/test_t21_business_days.py::test_ambiguous_business_wording_stays_unsupported",
            "tests/test_research_support.py::test_business_day_period_is_not_silently_calculated_as_calendar_days",
        ),
    ),
    TrapClass(
        class_id="deadline_clock_cutoff",
        description="time-of-day / time-zone cutoff claims unsupported or miscounted",
        fact_keys=(
            FactKey.DEADLINE_DAYS,
            FactKey.DEADLINE_ABSOLUTE,
            FactKey.DEADLINE_CLOCK_START,
            FactKey.DEADLINE_RULE,
        ),
        guards=(
            "parkingbreaker.research.support.value_supported_by_quote",
            "parkingbreaker.research.support._UNSUPPORTED_DEADLINE_TIME",
            "parkingbreaker.research.support._one_explicit_cutoff",
        ),
        fixtures=(
            "J15",
            "tests/test_t21_business_days.py::test_explicit_cutoff_with_a_day_is_supported_only_with_cutoff_flag",
            "tests/test_t21_business_days.py::test_other_deadline_keys_keep_their_time_veto",
        ),
    ),
    TrapClass(
        class_id="deadline_wrong_trigger",
        description="service-date or unrelated mention starts the clock",
        fact_keys=(FactKey.DEADLINE_CLOCK_START,),
        guards=(
            "parkingbreaker.research.support.value_supported_by_quote",
            "parkingbreaker.research.support._ISSUE_TRIGGER",
            "parkingbreaker.research.support._NOTICE_TRIGGER",
        ),
        fixtures=(
            "J14",
            "tests/test_research_support.py::test_clock_start",
            "tests/test_research_support.py::test_unrelated_service_date_and_notice_mention_do_not_change_clock_start",
        ),
    ),
    TrapClass(
        class_id="postmark_negation",
        description='"postmarks not accepted" read as a POSTMARK rule',
        fact_keys=(FactKey.DEADLINE_RULE,),
        guards=(
            "parkingbreaker.research.support.value_supported_by_quote",
            "parkingbreaker.research.support._POSTMARK_NEGATED",
        ),
        fixtures=(
            "J13",
            "tests/test_research_support.py::test_deadline_rule",
        ),
    ),
    TrapClass(
        class_id="receipt_deadline_unsafe",
        description="certified label / mail class treated as making a receipt deadline safe",
        fact_keys=(FactKey.DEADLINE_RULE, FactKey.DEADLINE_DAYS),
        guards=(
            "parkingbreaker.mail_timing.latest_safe_send_date",
            "parkingbreaker.mail_timing.compute",
        ),
        fixtures=(
            "J31",
            "J32",
            "tests/test_mail_timing.py::test_receipt_is_never_more_lenient_than_postmark",
            "tests/test_mail_timing.py::test_worked_example_receipt_rule_across_columbus_day",
        ),
    ),
    TrapClass(
        class_id="page_injection",
        description="fetched-page text instructs the model",
        fact_keys=_ALL_FACT_KEYS,
        guards=(
            "parkingbreaker.llm.neutralise",
            "parkingbreaker.research.findings.verify_quote",
            "parkingbreaker.research.findings.apply_quote_guard",
        ),
        fixtures=(
            "tests/test_research_findings.py::test_injection_paraphrased_by_the_extractor_has_no_effect",
            "tests/test_research_delegation.py::test_prompt_injection_elsewhere_in_page_does_not_change_payment_purpose",
            "tests/test_research_issuer_assessment.py::test_injection_echo_with_fabricated_quote_still_vetoed",
        ),
    ),
    TrapClass(
        class_id="fabricated_quote",
        description="invented/near-miss/non-cited quote as evidence",
        fact_keys=_ALL_FACT_KEYS,
        guards=(
            "parkingbreaker.research.findings.verify_quote",
            "parkingbreaker.research.findings.apply_quote_guard",
            "parkingbreaker.research.findings.MIN_QUOTE_CHARS",
        ),
        fixtures=(
            "J19",
            "tests/test_research_findings.py::test_near_miss_quotes_fail",
            "tests/test_research_issuer_assessment.py::test_each_quote_must_exist_be_long_and_verbatim",
        ),
    ),
    TrapClass(
        class_id="stale_reuse",
        description="cached research reused across violation/date/issuer/stage",
        fact_keys=(FactKey.DEADLINE_DAYS, FactKey.DEADLINE_ABSOLUTE),
        guards=(
            "parkingbreaker.research.run.ResearchResult.reusable_across",
            "parkingbreaker.research.run.ResearchResult.reusable_for",
            "parkingbreaker.research.run.reuse_research",
        ),
        fixtures=(
            "J16",
            "tests/test_t21_research_reuse.py::test_date_specific_absolute_deadline_is_not_carried_to_another_issue_date",
            "tests/test_procedural_deep_reading.py::test_reuse_never_carries_conclusions_to_another_violation_or_issue_date",
        ),
    ),
    TrapClass(
        class_id="stale_discovery_snapshot",
        description="stale DotGov snapshot used as current official sites",
        fact_keys=(),
        guards=("parkingbreaker.research.issuer_resolution.resolve_ticket_issuer",),
        fixtures=(
            "tests/test_research_issuer_resolution.py::test_stale_snapshot_stops_before_any_external_call",
        ),
    ),
    TrapClass(
        class_id="statute_only_address",
        description="mailing address sourced only from a code publisher",
        fact_keys=(FactKey.MAILING_ADDRESS,),
        guards=("parkingbreaker.research.findings.apply_quote_guard",),
        fixtures=(
            "tests/test_procedural_deep_reading.py::test_statute_only_mailing_address_is_vetoed",
        ),
    ),
    TrapClass(
        class_id="notice_official_conflict",
        description="printed notice vs official finding kept as a conflict",
        fact_keys=(FactKey.DEADLINE_DAYS, FactKey.DEADLINE_CLOCK_START, FactKey.MAILING_ADDRESS),
        guards=("parkingbreaker.research.resolution.detect_notice_conflicts",),
        fixtures=(
            "J14",
            "J19",
            "tests/test_case_packet_requirements.py::test_notice_conflicts_retain_both_verbatim_quotes_without_changing_findings",
        ),
    ),
    TrapClass(
        class_id="absence_without_wording",
        description="empty requirement list w/o explicit absence; negation used as waiver",
        fact_keys=(
            FactKey.REQUIRED_ITEMS,
            FactKey.REQUIRED_DECLARATIONS,
            FactKey.CONTEST_ELIGIBILITY,
            FactKey.MANDATORY_FORMS,
            FactKey.SIGNATURE_REQUIREMENT,
            FactKey.PRIOR_STEP_REQUIREMENT,
            FactKey.TICKET_SPECIFIC_REQUIREMENTS,
            FactKey.CODE_EXCEPTIONS,
        ),
        guards=(
            "parkingbreaker.research.support.value_supported_by_quote",
            "parkingbreaker.research.support._states_absence",
            "parkingbreaker.research.findings.apply_quote_guard",
        ),
        fixtures=(
            "tests/test_research_support.py::test_unrelated_negation_cannot_prove_an_empty_list",
            "tests/test_research_support.py::test_empty_list_requires_absence_of_that_specific_requirement",
        ),
    ),
    TrapClass(
        class_id="mandatory_item_hidden_in_linked_pdf",
        description="procedural silence while linked contest form unread",
        fact_keys=(
            FactKey.MANDATORY_FORMS,
            FactKey.SIGNATURE_REQUIREMENT,
            FactKey.REQUIRED_DECLARATIONS,
        ),
        guards=(
            "parkingbreaker.research.procedure.unread_linked_artifacts",
            "parkingbreaker.research.fulfilment.requirement_fulfilled",
        ),
        fixtures=(
            "J03",
            "tests/test_procedural_deep_reading.py::test_unread_contest_form_pdf_vetoes_silence_and_remains_unknown_after_reload",
            "tests/test_case_packet_requirements.py::test_sfmta_declaration_on_linked_form_is_never_treated_as_silence",
        ),
    ),
    TrapClass(
        class_id="optional_item_false_refusal",
        description='"you may include" wording forced as mandatory (or mandatory dropped)',
        fact_keys=(FactKey.REQUIRED_ITEMS, FactKey.TICKET_SPECIFIC_REQUIREMENTS),
        guards=("parkingbreaker.research.fulfilment.customer_optional",),
        fixtures=(
            "J04",
            "U06",
            "tests/test_research_fulfilment.py::test_invited_supporting_documents_are_customer_optional",
        ),
    ),
)

UNGUARDED: tuple[UnguardedTrap, ...] = (
    UnguardedTrap(
        class_id="payment_forfeits_review",
        where_named=(
            'T21.md:35 "payment-forfeits-review"; PROCEDURAL-SAFEGUARDS.md:49 "explicit review bar"'
        ),
        planned_unit="T21 PS01, integration §2 typed payment-effect rule (J21)",
    ),
    UnguardedTrap(
        class_id="hearing_deposit_vs_waiver",
        where_named='T21.md:36 "installment/deposit"; PROCEDURAL-SAFEGUARDS.md:22-24',
        planned_unit="T21 acceptance 4 + PS01 (J22)",
    ),
    UnguardedTrap(
        class_id="installment_enrollment_waiver",
        where_named="same lines",
        planned_unit="T21 PS01; closed factual enrollment check (J23)",
    ),
    UnguardedTrap(
        class_id="relief_option_no_invented_waiver",
        where_named=(
            'PROCEDURAL-SAFEGUARDS.md:52 "Absence of waiver wording is not proof of a waiver"'
        ),
        planned_unit="T21 PS01 (J24)",
    ),
    UnguardedTrap(
        class_id="portal_only_or_upload_limit",
        where_named=(
            'T21.md:37 "portal-only or upload-limit steps"; PROCEDURAL-SAFEGUARDS.md:56-59'
        ),
        planned_unit="T21 PS03 route/resolution path (J27, J28)",
    ),
    UnguardedTrap(
        class_id="compulsory_in_person_prerequisite",
        where_named=(
            'T21.md:36 "in-person-only vs optional hearing"; PROCEDURAL-SAFEGUARDS.md:57-58'
        ),
        planned_unit=(
            "T21 PS03; only serviceability.BlockReason.IN_PERSON_ONLY_FIRST_STEP exists "
            "today (J29, J30)"
        ),
    ),
    UnguardedTrap(
        class_id="processor_lockbox_as_destination",
        where_named='T21.md:35 "processor lockbox"',
        planned_unit="partial cover by rows 1-2; processor-specific class = T21 pitfall discovery",
    ),
    UnguardedTrap(
        class_id="silent_tier_or_amount_upgrade",
        where_named="PROCEDURAL-SAFEGUARDS.md:70-72 (PS06)",
        planned_unit=(
            "ownership table assigns T14/T16 gates + T21 sourced rules (U09); no "
            "research-side guard verified in this pass"
        ),
    ),
    UnguardedTrap(
        class_id="photo_guide_merits_inference",
        where_named="PROCEDURAL-SAFEGUARDS.md:103-113 (PS05)",
        planned_unit="T37/T12 with T25 proof (U08)",
    ),
    UnguardedTrap(
        class_id="certified_mail_proves_review",
        where_named="PROCEDURAL-SAFEGUARDS.md:27-31 (PS07)",
        planned_unit="T17/T20/T24/T26 honest postal states (U10)",
    ),
)


def by_id(class_id: str) -> TrapClass | UnguardedTrap:
    """Return the catalogue entry for this class id."""
    for guarded in GUARDED:
        if guarded.class_id == class_id:
            return guarded
    for unguarded in UNGUARDED:
        if unguarded.class_id == class_id:
            return unguarded
    raise KeyError(class_id)
