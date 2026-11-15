"""Synthetic official-style evidence for the separate fine/deposit/plan policy."""

from __future__ import annotations

from dataclasses import replace

import pytest

from parkingbreaker.research.findings import FindingStatus
from parkingbreaker.research.ladder import CustomerQuestion
from parkingbreaker.research.payment_effects import (
    PaymentAction,
    PaymentCondition,
    PaymentEffect,
    PaymentOutcome,
    PaymentRule,
    PaymentScope,
    evaluate_payment_rule,
    payment_rule_problem,
)
from parkingbreaker.research.sources import AuthorityClass
from samples import source

SCOPE = PaymentScope("us-ca-san_francisco", "SFMTA", "first-level review")
HEADING = "San Francisco SFMTA Parking Citation Initial Review. "
FINE = "Paying the fine waives your right to request an initial review."
DEPOSIT = "A hearing deposit is required to request an initial review."
PLAN = "Enrolling in a payment plan waives your right to request an initial review."


def rule(
    quote: str = FINE,
    action: PaymentAction = PaymentAction.FINE_PAYMENT,
    effect: PaymentEffect = PaymentEffect.EXPLICIT_REVIEW_BAR,
    condition: PaymentCondition = PaymentCondition.ACTION_OCCURRED,
) -> PaymentRule:
    page = source(HEADING + quote)
    return PaymentRule(
        action, effect, condition, SCOPE, quote, page.text, (page.sha256,), FindingStatus.CONFIRMED
    )


@pytest.mark.parametrize(
    ("quote", "action", "effect", "condition", "question"),
    [
        (
            FINE,
            PaymentAction.FINE_PAYMENT,
            PaymentEffect.EXPLICIT_REVIEW_BAR,
            PaymentCondition.ACTION_OCCURRED,
            CustomerQuestion.PAID_FINE,
        ),
        (
            DEPOSIT,
            PaymentAction.HEARING_DEPOSIT,
            PaymentEffect.REQUIRED_DEPOSIT,
            PaymentCondition.ACTION_REQUIRED,
            CustomerQuestion.PAID_HEARING_DEPOSIT,
        ),
        (
            PLAN,
            PaymentAction.PLAN_ENROLLMENT,
            PaymentEffect.EXPLICIT_REVIEW_BAR,
            PaymentCondition.ACTION_OCCURRED,
            CustomerQuestion.ENTERED_PLAN,
        ),
    ],
)
def test_action_is_asked_only_for_its_verified_current_rule(
    quote: str,
    action: PaymentAction,
    effect: PaymentEffect,
    condition: PaymentCondition,
    question: CustomerQuestion,
) -> None:
    current = rule(quote, action, effect, condition)
    pages = (source(HEADING + quote),)
    assert payment_rule_problem(current, pages, SCOPE) is None
    result = evaluate_payment_rule(current, pages, SCOPE, action_occurred=None, answered=False)
    assert result.outcome is PaymentOutcome.ASK_CUSTOMER
    assert result.question is question
    unknown = evaluate_payment_rule(current, pages, SCOPE, action_occurred=None, answered=True)
    assert unknown.outcome is PaymentOutcome.UNRESOLVED


def test_fine_and_plan_bars_need_the_matching_true_action() -> None:
    for quote, action in (
        (FINE, PaymentAction.FINE_PAYMENT),
        (PLAN, PaymentAction.PLAN_ENROLLMENT),
    ):
        current = rule(quote, action)
        pages = (source(HEADING + quote),)
        assert (
            evaluate_payment_rule(
                current, pages, SCOPE, action_occurred=False, answered=True
            ).outcome
            is PaymentOutcome.NO_ADDITIONAL_GATE
        )
        barred = evaluate_payment_rule(current, pages, SCOPE, action_occurred=True, answered=True)
        assert barred.outcome is PaymentOutcome.REVIEW_BAR
        assert barred.official_quote == quote


def test_required_deposit_is_a_prerequisite_and_never_a_review_bar() -> None:
    current = rule(
        DEPOSIT,
        PaymentAction.HEARING_DEPOSIT,
        PaymentEffect.REQUIRED_DEPOSIT,
        PaymentCondition.ACTION_REQUIRED,
    )
    pages = (source(HEADING + DEPOSIT),)
    assert (
        evaluate_payment_rule(current, pages, SCOPE, action_occurred=True, answered=True).outcome
        is PaymentOutcome.NO_ADDITIONAL_GATE
    )
    assert (
        evaluate_payment_rule(current, pages, SCOPE, action_occurred=False, answered=True).outcome
        is PaymentOutcome.UNRESOLVED
    )
    unsafe = rule(FINE.replace("fine", "hearing deposit"), PaymentAction.HEARING_DEPOSIT)
    assert (
        evaluate_payment_rule(
            unsafe,
            (source(HEADING + unsafe.verbatim_quote),),
            SCOPE,
            action_occurred=True,
            answered=True,
        ).outcome
        is PaymentOutcome.UNRESOLVED
    )


@pytest.mark.parametrize(
    "damage",
    [
        "issuer",
        "body",
        "stage",
        "missing_scope",
        "quote",
        "hash",
        "scope_quote",
        "status",
        "secondary",
        "http",
        "foreign_source",
        "condition",
    ],
)
def test_unproved_or_foreign_evidence_never_applies_a_bar(damage: str) -> None:
    current = rule()
    page = source(HEADING + FINE)
    if damage == "issuer":
        current = replace(current, scope=replace(SCOPE, issuer="us-ma-boston"))
    elif damage == "body":
        current = replace(current, scope=replace(SCOPE, deciding_body="Other Office"))
    elif damage == "stage":
        current = replace(current, scope=replace(SCOPE, stage="administrative hearing"))
    elif damage == "missing_scope":
        current = replace(current, scope=None)
    elif damage == "quote":
        current = replace(current, verbatim_quote="Invented words that were never on the page.")
    elif damage == "hash":
        current = replace(current, source_sha256s=("a" * 64,))
    elif damage == "scope_quote":
        current = replace(current, scope_quote=FINE)
    elif damage == "status":
        current = replace(current, status=FindingStatus.UNKNOWN)
    elif damage == "secondary":
        page = replace(page, authority_class=AuthorityClass.SECONDARY)
    elif damage == "http":
        page = replace(page, http_status=403)
    elif damage == "foreign_source":
        page = replace(page, jurisdiction="us-ma-boston")
    elif damage == "condition":
        current = replace(current, condition=PaymentCondition.UNRESOLVED)
    assert payment_rule_problem(current, (page,), SCOPE) is not None
    result = evaluate_payment_rule(current, (page,), SCOPE, action_occurred=True, answered=True)
    assert result.outcome is PaymentOutcome.UNRESOLVED
    assert result.official_quote is None


@pytest.mark.parametrize(
    "quote",
    [
        "Pay the fine online. The initial review is handled separately.",
        "Paying the fine does not waive your right to request an initial review.",
        "Paying the fine is allowed, but enrolling in a payment plan waives initial review.",
        "A hearing deposit is required to request an initial review.",
        "Paying the fine waives initial review only when another condition is met.",
    ],
)
def test_a_bar_cannot_borrow_another_action_or_unstated_condition(quote: str) -> None:
    current = rule(quote)
    result = evaluate_payment_rule(
        current, (source(HEADING + quote),), SCOPE, action_occurred=True, answered=True
    )
    assert result.outcome is PaymentOutcome.UNRESOLVED


def test_explicit_retained_review_creates_no_extra_gate_or_permission() -> None:
    quote = "Paying the fine does not waive your right to request an initial review."
    current = rule(quote, effect=PaymentEffect.NO_STATED_EFFECT, condition=PaymentCondition.NONE)
    result = evaluate_payment_rule(
        current, (source(HEADING + quote),), SCOPE, action_occurred=None, answered=False
    )
    assert result.outcome is PaymentOutcome.NO_ADDITIONAL_GATE
    assert result.question is None


def test_conflicting_payment_effects_stay_unresolved() -> None:
    quote = FINE + " Paying the fine does not waive your right to request an initial review."
    current = rule(
        quote, effect=PaymentEffect.UNRESOLVED_CONFLICT, condition=PaymentCondition.UNRESOLVED
    )
    result = evaluate_payment_rule(
        current, (source(HEADING + quote),), SCOPE, action_occurred=True, answered=True
    )
    assert result.outcome is PaymentOutcome.UNRESOLVED


@pytest.mark.parametrize(
    ("quote", "action", "effect", "condition"),
    [
        (
            "Not paying the fine bars an initial review.",
            PaymentAction.FINE_PAYMENT,
            PaymentEffect.EXPLICIT_REVIEW_BAR,
            PaymentCondition.ACTION_OCCURRED,
        ),
        (
            "Paying the fine is permitted during initial review and waives the late fee.",
            PaymentAction.FINE_PAYMENT,
            PaymentEffect.EXPLICIT_REVIEW_BAR,
            PaymentCondition.ACTION_OCCURRED,
        ),
        (
            "Paying the fine does not waive the late fee and bars an initial review.",
            PaymentAction.FINE_PAYMENT,
            PaymentEffect.NO_STATED_EFFECT,
            PaymentCondition.NONE,
        ),
        (
            "No hearing deposit is required to request an initial review.",
            PaymentAction.HEARING_DEPOSIT,
            PaymentEffect.REQUIRED_DEPOSIT,
            PaymentCondition.ACTION_REQUIRED,
        ),
        (
            "A hearing deposit must not be paid to request an initial review.",
            PaymentAction.HEARING_DEPOSIT,
            PaymentEffect.REQUIRED_DEPOSIT,
            PaymentCondition.ACTION_REQUIRED,
        ),
        (
            "Paying the fine waives initial review if the request is late.",
            PaymentAction.FINE_PAYMENT,
            PaymentEffect.EXPLICIT_REVIEW_BAR,
            PaymentCondition.ACTION_OCCURRED,
        ),
        (
            "Paying the fine waives initial review after the response deadline has expired.",
            PaymentAction.FINE_PAYMENT,
            PaymentEffect.EXPLICIT_REVIEW_BAR,
            PaymentCondition.ACTION_OCCURRED,
        ),
        (
            "For Oakland initial review, paying the fine waives your right "
            "to request an initial review.",
            PaymentAction.FINE_PAYMENT,
            PaymentEffect.EXPLICIT_REVIEW_BAR,
            PaymentCondition.ACTION_OCCURRED,
        ),
    ],
)
def test_independent_review_counterexamples_never_become_applicable_rules(
    quote: str, action: PaymentAction, effect: PaymentEffect, condition: PaymentCondition
) -> None:
    current = rule(quote, action, effect, condition)
    pages = (source(HEADING + quote),)
    assert payment_rule_problem(current, pages, SCOPE) is not None
    result = evaluate_payment_rule(current, pages, SCOPE, action_occurred=True, answered=True)
    assert result.outcome is PaymentOutcome.UNRESOLVED
    assert result.question is None
    assert result.official_quote is None


def test_page_heading_cannot_override_the_selected_effect_stage() -> None:
    hearing = replace(SCOPE, stage="administrative hearing")
    page = source("San Francisco SFMTA Parking Citation Administrative Hearing. " + FINE)
    current = replace(rule(), scope=hearing, scope_quote=page.text, source_sha256s=(page.sha256,))
    assert payment_rule_problem(current, (page,), hearing) is not None


def test_foreign_section_cannot_borrow_the_page_heading() -> None:
    page = source(HEADING + "\nOakland initial review:\n" + FINE)
    current = replace(rule(), scope_quote=page.text, source_sha256s=(page.sha256,))
    assert payment_rule_problem(current, (page,), SCOPE) is not None


def test_unresolved_effect_and_untyped_condition_never_validate() -> None:
    current = replace(
        rule(),
        effect=PaymentEffect.UNRESOLVED_CONFLICT,
        condition=None,  # type: ignore[arg-type]
    )
    assert payment_rule_problem(current, (source(HEADING + FINE),), SCOPE) is not None
