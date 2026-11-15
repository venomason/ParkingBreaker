"""Optional payment-rule evidence and pure action decisions, separate from packet requirements.

These records do not extend FactKey or admit a ticket. NO_ADDITIONAL_GATE leaves every
ordinary research, packet and authorization gate in force. Callers supply scope and sources.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from parkingbreaker.research.findings import FindingStatus, verify_quote
from parkingbreaker.research.ladder import CustomerQuestion
from parkingbreaker.research.procedure import (
    REVIEW_ROUTE_TEXT,
    is_hearing_deposit_requirement,
    jurisdiction_in_text,
    normal,
    procedure_stage,
    same_stage,
)
from parkingbreaker.research.sources import AuthorityClass, SourceRecord


class PaymentAction(StrEnum):
    FINE_PAYMENT = "FINE_PAYMENT"
    HEARING_DEPOSIT = "HEARING_DEPOSIT"
    PLAN_ENROLLMENT = "PLAN_ENROLLMENT"


class PaymentEffect(StrEnum):
    EXPLICIT_REVIEW_BAR = "EXPLICIT_REVIEW_BAR"
    REQUIRED_DEPOSIT = "REQUIRED_DEPOSIT"
    NO_STATED_EFFECT = "NO_STATED_EFFECT"
    UNRESOLVED_CONFLICT = "UNRESOLVED_CONFLICT"


class PaymentCondition(StrEnum):
    ACTION_OCCURRED = "ACTION_OCCURRED"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    NONE = "NONE"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class PaymentScope:
    issuer: str
    deciding_body: str
    stage: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(v, str) or not v.strip()
            for v in (self.issuer, self.deciding_body, self.stage)
        ):
            raise ValueError("payment scope needs issuer, deciding body and stage")


@dataclass(frozen=True)
class PaymentRule:
    action: PaymentAction
    effect: PaymentEffect
    condition: PaymentCondition
    scope: PaymentScope | None
    verbatim_quote: str
    scope_quote: str
    source_sha256s: tuple[str, ...]
    status: FindingStatus = FindingStatus.UNKNOWN


class PaymentOutcome(StrEnum):
    NO_ADDITIONAL_GATE = "NO_ADDITIONAL_GATE"
    ASK_CUSTOMER = "ASK_CUSTOMER"
    REVIEW_BAR = "REVIEW_BAR"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class PaymentDecision:
    outcome: PaymentOutcome
    question: CustomerQuestion | None = None
    official_quote: str | None = None


def payment_scope_from_data(value: object) -> PaymentScope:
    if not isinstance(value, dict) or set(value) != {"issuer", "deciding_body", "stage"}:
        raise ValueError("payment_scope fields invalid")
    parts = [value[name] for name in ("issuer", "deciding_body", "stage")]
    if any(not isinstance(part, str) or not part.strip() for part in parts):
        raise ValueError("payment_scope requires nonempty strings")
    return PaymentScope(str(parts[0]), str(parts[1]), str(parts[2]))


def payment_rule_from_data(value: object) -> PaymentRule:
    """One strict decoder for extractor output and durable records; no coercion."""
    fields = {
        "action",
        "effect",
        "condition",
        "status",
        "scope",
        "verbatim_quote",
        "scope_quote",
        "source_sha256s",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("payment_rule fields invalid")
    for name in ("action", "effect", "condition", "status", "verbatim_quote", "scope_quote"):
        if not isinstance(value[name], str) or not value[name].strip():
            raise ValueError("payment_rule requires nonempty strings")
    hashes = value["source_sha256s"]
    if (
        not isinstance(hashes, list)
        or len(hashes) > 32
        or any(
            not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None for sha in hashes
        )
    ):
        raise ValueError("payment_rule source hashes invalid")
    try:
        return PaymentRule(
            PaymentAction(value["action"]),
            PaymentEffect(value["effect"]),
            PaymentCondition(value["condition"]),
            None if value["scope"] is None else payment_scope_from_data(value["scope"]),
            value["verbatim_quote"],
            value["scope_quote"],
            tuple(hashes),
            FindingStatus(value["status"]),
        )
    except ValueError as exc:
        raise ValueError("payment_rule contains invalid enum or scope") from exc


def unresolved_payment_rule() -> PaymentRule:
    """An unusable visible claim stays unresolved rather than disappearing."""
    return PaymentRule(
        PaymentAction.FINE_PAYMENT,
        PaymentEffect.UNRESOLVED_CONFLICT,
        PaymentCondition.UNRESOLVED,
        None,
        "Unresolved payment-rule extraction",
        "Unresolved payment-rule extraction",
        (),
        FindingStatus.UNKNOWN,
    )


def retain_unresolved_payment_claim(rules: Sequence[PaymentRule]) -> tuple[PaymentRule, ...]:
    unresolved = unresolved_payment_rule()
    return tuple(rules) if unresolved in rules else (*rules[:31], unresolved)


def uncovered_payment_claims(
    rules: Sequence[PaymentRule], sources: Sequence[SourceRecord], stage: str | None = None
) -> bool:
    """Every supported action/effect sentence needs its exact source-bound evidence.

    This independent veto uses the same complete-sentence grammar as validation.
    Payment navigation, negated requirements and an unrelated waived fee are not bars.
    Unsupported syntax still cannot validate a model's claimed rule.
    """
    claims: list[tuple[SourceRecord, str, PaymentEffect]] = []
    for page in sources:
        if page.http_status != 200 or page.authority_class is AuthorityClass.SECONDARY:
            continue
        for sentence in re.split(r"(?<=[.!?;])\s+|\n+", page.text):
            stated_stage = procedure_stage(sentence)
            if (
                stage is not None
                and stated_stage is not None
                and not same_stage(stated_stage, stage)
            ):
                continue
            for action in PaymentAction:
                effects = _stated_effects(action, sentence)
                if len(effects) == 1 and PaymentEffect.UNRESOLVED_CONFLICT not in effects:
                    claims.append((page, sentence, next(iter(effects))))
    if not rules and not any(
        effect is not PaymentEffect.NO_STATED_EFFECT for _, _, effect in claims
    ):
        return False
    return any(
        not any(
            page.sha256 in rule.source_sha256s and verify_quote(sentence, rule.verbatim_quote)
            for rule in rules
        )
        for page, sentence, _ in claims
    )


_QUESTION = {
    PaymentAction.FINE_PAYMENT: CustomerQuestion.PAID_FINE,
    PaymentAction.HEARING_DEPOSIT: CustomerQuestion.PAID_HEARING_DEPOSIT,
    PaymentAction.PLAN_ENROLLMENT: CustomerQuestion.ENTERED_PLAN,
}
_CONDITION = {
    PaymentEffect.EXPLICIT_REVIEW_BAR: PaymentCondition.ACTION_OCCURRED,
    PaymentEffect.REQUIRED_DEPOSIT: PaymentCondition.ACTION_REQUIRED,
    PaymentEffect.NO_STATED_EFFECT: PaymentCondition.NONE,
}
_ACTION_TEXT = {
    PaymentAction.FINE_PAYMENT: r"paying (?:the |your )?(?:fine|citation)",
    PaymentAction.HEARING_DEPOSIT: r"paying (?:the |a |your )?hearing deposit",
    PaymentAction.PLAN_ENROLLMENT: (
        r"(?:enrolling in|entering) (?:a |the )?(?:payment|installment) plan"
    ),
}
_BAR_TEXT = rf"(?:waives?|bars?|forfeits?) (?:your right to (?:request|seek) )?{REVIEW_ROUTE_TEXT}"
_RETAINED_TEXT = (
    rf"does not (?:waive|bar|forfeit) (?:your right to (?:request|seek) )?{REVIEW_ROUTE_TEXT}"
)


def _stated_effects(action: PaymentAction, quote: str) -> set[PaymentEffect]:
    """Admit only complete supported action-to-route sentences.

    Unknown syntax remains unresolved. Matching a payment word and an unrelated
    bar/negation elsewhere in a sentence never proves a rule.
    """
    effects: set[PaymentEffect] = set()
    sentences = re.split(r"(?<=[.!?])\s+|\n+", normal(quote))
    for sentence in sentences:
        content = sentence.removesuffix(".")
        if re.fullmatch(rf"{_ACTION_TEXT[action]} {_BAR_TEXT}", content, re.I):
            effects.add(PaymentEffect.EXPLICIT_REVIEW_BAR)
        elif re.fullmatch(rf"{_ACTION_TEXT[action]} {_RETAINED_TEXT}", content, re.I):
            effects.add(PaymentEffect.NO_STATED_EFFECT)
        elif action is PaymentAction.HEARING_DEPOSIT and is_hearing_deposit_requirement(sentence):
            effects.add(PaymentEffect.REQUIRED_DEPOSIT)
        else:
            return {PaymentEffect.UNRESOLVED_CONFLICT}
    return effects


def payment_rule_problem(
    rule: PaymentRule, sources: Sequence[SourceRecord], expected: PaymentScope
) -> str | None:
    """Recheck claimed semantics against one exact official source and its scoped quote."""
    if rule.status is not FindingStatus.CONFIRMED:
        return "payment rule unconfirmed"
    if rule.scope is None or rule.scope != expected:
        return "payment rule scope unverified"
    if not isinstance(rule.action, PaymentAction) or not isinstance(rule.effect, PaymentEffect):
        return "payment rule action or effect invalid"
    if (
        not isinstance(rule.condition, PaymentCondition)
        or rule.effect is PaymentEffect.UNRESOLVED_CONFLICT
        or _CONDITION.get(rule.effect) is not rule.condition
    ):
        return "payment rule condition unresolved"
    if (
        rule.effect is PaymentEffect.REQUIRED_DEPOSIT
        and rule.action is not PaymentAction.HEARING_DEPOSIT
    ) or (
        rule.effect is PaymentEffect.EXPLICIT_REVIEW_BAR
        and rule.action is PaymentAction.HEARING_DEPOSIT
    ):
        return "payment action and effect disagree"
    if re.search(
        r"\bunless\b|\bexcept\b|\bonly (?:if|when)\b|\bsubject to\b", rule.verbatim_quote, re.I
    ):
        return "payment rule has an unsupported additional condition"
    if (
        not jurisdiction_in_text(expected.issuer, rule.scope_quote)
        or normal(expected.deciding_body).casefold() not in normal(rule.scope_quote).casefold()
        or not same_stage(procedure_stage(rule.scope_quote) or "", expected.stage)
        or not verify_quote(rule.verbatim_quote, rule.scope_quote)
    ):
        return "payment scope quote unverified"
    # The supported scope excerpt is one heading immediately followed by the
    # complete effect sentence(s). A title cannot lend scope across another section.
    scope_text, effect_text = normal(rule.scope_quote), normal(rule.verbatim_quote)
    if not scope_text.endswith(effect_text):
        return "payment scope is not adjacent to its effect"
    heading = scope_text[: -len(effect_text)].strip().removesuffix(".").strip()
    if (
        not heading
        or any(mark in heading for mark in (".", ":", ";", "!", "?"))
        or not jurisdiction_in_text(expected.issuer, heading)
        or normal(expected.deciding_body).casefold() not in heading.casefold()
        or not same_stage(procedure_stage(heading) or "", expected.stage)
        or not same_stage(procedure_stage(rule.verbatim_quote) or "", expected.stage)
    ):
        return "payment effect section or stage unverified"
    cited = [source for source in sources if source.sha256 in rule.source_sha256s]
    if not rule.source_sha256s or set(rule.source_sha256s) != {s.sha256 for s in cited}:
        return "payment source absent"
    for source in cited:
        if (
            source.authority_class
            not in {
                AuthorityClass.OFFICIAL_PROCEDURE,
                AuthorityClass.OFFICIAL_FORM,
                AuthorityClass.STATUTE,
                AuthorityClass.REGULATION,
            }
            or source.http_status != 200
            or re.fullmatch(r"[0-9a-f]{64}", source.sha256) is None
            or (source.jurisdiction is not None and source.jurisdiction != expected.issuer)
            or not verify_quote(rule.scope_quote, source.text)
        ):
            return "payment source unverified"
    if _stated_effects(rule.action, rule.verbatim_quote) != {rule.effect}:
        return "payment effect not established by quote"
    return None


def evaluate_payment_rule(
    rule: PaymentRule,
    sources: Sequence[SourceRecord],
    expected: PaymentScope,
    *,
    action_occurred: bool | None,
    answered: bool,
) -> PaymentDecision:
    """Apply only a proved rule to its matching closed factual answer, never to a reason."""
    if (action_occurred is not None and type(action_occurred) is not bool) or type(
        answered
    ) is not bool:
        raise ValueError("payment action and answered flags must be booleans")
    if payment_rule_problem(rule, sources, expected) is not None:
        return PaymentDecision(PaymentOutcome.UNRESOLVED)
    if rule.effect is PaymentEffect.NO_STATED_EFFECT:
        return PaymentDecision(PaymentOutcome.NO_ADDITIONAL_GATE)
    if action_occurred is None:
        return PaymentDecision(
            PaymentOutcome.UNRESOLVED if answered else PaymentOutcome.ASK_CUSTOMER,
            None if answered else _QUESTION[rule.action],
        )
    if rule.effect is PaymentEffect.REQUIRED_DEPOSIT:
        return PaymentDecision(
            PaymentOutcome.NO_ADDITIONAL_GATE if action_occurred else PaymentOutcome.UNRESOLVED
        )
    if action_occurred:
        return PaymentDecision(PaymentOutcome.REVIEW_BAR, official_quote=rule.verbatim_quote)
    return PaymentDecision(PaymentOutcome.NO_ADDITIONAL_GATE)
