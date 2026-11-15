"""The facts research must establish for a ticket, and the question set built from them.

Pure data. Value formats are stated here once; findings.validate_value enforces them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from types import MappingProxyType

from parkingbreaker.research.ladder import CriticalFact


class FactKey(StrEnum):
    MAIL_IN_ACCEPTED = "MAIL_IN_ACCEPTED"
    GOVERNING_CODE_SECTION = "GOVERNING_CODE_SECTION"
    DEADLINE_DAYS = "DEADLINE_DAYS"
    DEADLINE_CLOCK_START = "DEADLINE_CLOCK_START"
    DEADLINE_RULE = "DEADLINE_RULE"
    MAILING_ADDRESS = "MAILING_ADDRESS"
    REQUIRED_ITEMS = "REQUIRED_ITEMS"
    REQUIRED_DECLARATIONS = "REQUIRED_DECLARATIONS"
    FINE_AMOUNT = "FINE_AMOUNT"
    CODE_EXCEPTIONS = "CODE_EXCEPTIONS"

    CONTEST_ELIGIBILITY = "CONTEST_ELIGIBILITY"
    MANDATORY_FORMS = "MANDATORY_FORMS"
    SIGNATURE_REQUIREMENT = "SIGNATURE_REQUIREMENT"
    PRIOR_STEP_REQUIREMENT = "PRIOR_STEP_REQUIREMENT"
    TICKET_SPECIFIC_REQUIREMENTS = "TICKET_SPECIFIC_REQUIREMENTS"
    DEADLINE_ABSOLUTE = "DEADLINE_ABSOLUTE"


class FactTier(StrEnum):
    MUST_HAVE = "MUST_HAVE"
    IMPORTANT = "IMPORTANT"
    NICE_TO_HAVE = "NICE_TO_HAVE"


DEFAULT_TIERS: Mapping[FactKey, FactTier] = MappingProxyType(
    {
        FactKey.MAIL_IN_ACCEPTED: FactTier.MUST_HAVE,
        FactKey.GOVERNING_CODE_SECTION: FactTier.IMPORTANT,
        FactKey.DEADLINE_DAYS: FactTier.MUST_HAVE,
        FactKey.DEADLINE_CLOCK_START: FactTier.MUST_HAVE,
        FactKey.DEADLINE_RULE: FactTier.MUST_HAVE,
        FactKey.MAILING_ADDRESS: FactTier.MUST_HAVE,
        FactKey.REQUIRED_ITEMS: FactTier.MUST_HAVE,
        FactKey.REQUIRED_DECLARATIONS: FactTier.MUST_HAVE,
        FactKey.FINE_AMOUNT: FactTier.NICE_TO_HAVE,
        # Retired: kept only so historical records decode and DEFAULT_TIERS stays total.
        FactKey.CODE_EXCEPTIONS: FactTier.NICE_TO_HAVE,
        FactKey.CONTEST_ELIGIBILITY: FactTier.MUST_HAVE,
        FactKey.MANDATORY_FORMS: FactTier.MUST_HAVE,
        FactKey.SIGNATURE_REQUIREMENT: FactTier.MUST_HAVE,
        FactKey.PRIOR_STEP_REQUIREMENT: FactTier.MUST_HAVE,
        FactKey.TICKET_SPECIFIC_REQUIREMENTS: FactTier.MUST_HAVE,
        FactKey.DEADLINE_ABSOLUTE: FactTier.MUST_HAVE,
    }
)
CRITICAL_KEYS = frozenset(k for k, tier in DEFAULT_TIERS.items() if tier is FactTier.MUST_HAVE)
RULE_KEYS = frozenset(
    {
        FactKey.CONTEST_ELIGIBILITY,
        FactKey.MANDATORY_FORMS,
        FactKey.SIGNATURE_REQUIREMENT,
        FactKey.PRIOR_STEP_REQUIREMENT,
        FactKey.TICKET_SPECIFIC_REQUIREMENTS,
    }
)
LIST_KEYS = RULE_KEYS | {
    FactKey.REQUIRED_ITEMS,
    FactKey.REQUIRED_DECLARATIONS,
    FactKey.CODE_EXCEPTIONS,
}

# Keys kept for decoding historical records only. They are never asked, extracted, promoted
# or used to drive recovery, so an old saved finding cannot restart defence research.
RETIRED_KEYS: frozenset[FactKey] = frozenset({FactKey.CODE_EXCEPTIONS})

# Every key that can still be researched, in stable order.
ANSWERABLE_KEYS: tuple[FactKey, ...] = tuple(k for k in FactKey if k not in RETIRED_KEYS)


# Research keys for the critical facts that must be established before drafting.
# Registry activation is an intake gate, not evidence for this ticket.
CRITICAL_FACT_FOR: dict[FactKey, CriticalFact] = {
    FactKey.MAIL_IN_ACCEPTED: CriticalFact.MAIL_IN_ACCEPTED,
    FactKey.DEADLINE_DAYS: CriticalFact.DEADLINE,
    FactKey.DEADLINE_CLOCK_START: CriticalFact.DEADLINE,
    FactKey.DEADLINE_RULE: CriticalFact.COUNTING_RULE,
    FactKey.MAILING_ADDRESS: CriticalFact.MAILING_ADDRESS,
    FactKey.REQUIRED_ITEMS: CriticalFact.REQUIRED_ITEMS,
    FactKey.REQUIRED_DECLARATIONS: CriticalFact.REQUIRED_ITEMS,
    FactKey.DEADLINE_ABSOLUTE: CriticalFact.DEADLINE,
    **dict.fromkeys(RULE_KEYS, CriticalFact.REQUIRED_ITEMS),
}


@dataclass(frozen=True)
class Ticket:
    city_id: str
    violation_code: str
    violation_description: str
    issue_date: date
    jurisdiction: str | None = None
    stage: str | None = None


@dataclass(frozen=True)
class Question:
    key: FactKey
    critical: bool
    ask: str
    value_format: str
    # Transient; canonical records retain the existing scoped ask and field inventory.
    stage_context: str | None = field(default=None, compare=False, repr=False)

    @property
    def tier(self) -> FactTier:
        return DEFAULT_TIERS[self.key]


def build_questions(
    ticket: Ticket, jurisdiction: str | None = None, stage: str | None = None
) -> tuple[Question, ...]:
    """One question per fact key, in FactKey order, worded for this ticket."""
    violation = f"{ticket.violation_code} ({ticket.violation_description})"
    where = jurisdiction or ticket.jurisdiction or f"city {ticket.city_id}"
    scope = f"For {violation} in {where}, stage {stage or ticket.stage or 'first-level review'}: "
    issued = ticket.issue_date.isoformat()
    asks: dict[FactKey, tuple[str, str]] = {
        FactKey.MAIL_IN_ACCEPTED: (
            f"Does the agency accept contests of parking citations by mail in {where}?",
            "boolean; true only if the official source explicitly permits contests by mail",
        ),
        FactKey.GOVERNING_CODE_SECTION: (
            f"Which code section or ordinance number must a mailed contest cite or attach "
            f"for {violation} in {where}, according to the official form or notice?",
            "string, the section citation exactly as written, or an explicit statement that "
            'none is required, e.g. "Sec. 7.2.22"',
        ),
        FactKey.DEADLINE_DAYS: (
            f"How many days does a person have to contest a citation issued {issued} "
            f"in {where} by mail?",
            'integer from 1 to 365 or object {"days": int, "unit": "CALENDAR" or '
            '"BUSINESS", "time_cutoff": bool}; an unqualified day means a calendar day; '
            'unit "BUSINESS" only for explicit business/working days; "time_cutoff": true '
            "only when the source states a time-of-day cutoff; ambiguous wording is "
            "unsupported and must remain UNKNOWN",
        ),
        FactKey.DEADLINE_CLOCK_START: (
            "Which explicitly stated date starts that contest deadline?",
            'one of "ISSUE_DATE" or "NOTICE_DATE"; do not infer from an unrelated mention. '
            "Service-date and other triggers are unsupported and must remain UNKNOWN",
        ),
        FactKey.DEADLINE_RULE: (
            "Must the mailed contest be postmarked by the deadline, or received by it?",
            'one of "POSTMARK" or "RECEIPT"',
        ),
        FactKey.MAILING_ADDRESS: (
            f"What is the mailing address for contesting a parking citation in {where}?",
            "object with recipient, attention (or null), line1, line2 (or null), city, "
            "state (2 letters), zip_code",
        ),
        FactKey.REQUIRED_ITEMS: (
            "What must a mailed contest include (citation number, copies, forms, etc.)?",
            "list of strings, empty if the source says nothing is required",
        ),
        FactKey.REQUIRED_DECLARATIONS: (
            "Does a mailed contest require any signed declaration or sworn statement?",
            "list of strings naming each declaration, empty if none",
        ),
        FactKey.FINE_AMOUNT: (
            f"What is the fine amount for {violation} in {where}?",
            "number in US dollars",
        ),
    }
    asks.update(
        {
            FactKey.CONTEST_ELIGIBILITY: (
                "Who may contest, and what standing or proof is required?",
                "list of verbatim standing requirements; "
                "empty with an explicit absence or complete-procedure assessment",
            ),
            FactKey.MANDATORY_FORMS: (
                "Which agency forms are mandatory, or are no forms required?",
                "list of verbatim mandatory forms; "
                "empty with an explicit absence or complete-procedure assessment",
            ),
            FactKey.SIGNATURE_REQUIREMENT: (
                "Which documents require signatures, or is no signature required?",
                "list of verbatim signature requirements; "
                "empty with an explicit absence or complete-procedure assessment",
            ),
            FactKey.PRIOR_STEP_REQUIREMENT: (
                "Which prior steps are compulsory, or is no prior step required?",
                "list of verbatim prior steps; "
                "empty with an explicit absence or complete-procedure assessment",
            ),
            FactKey.TICKET_SPECIFIC_REQUIREMENTS: (
                "Which other compulsory conditions apply to this ticket and stage?",
                "list of verbatim compulsory conditions; "
                "empty with an explicit absence or complete-procedure assessment",
            ),
            FactKey.DEADLINE_ABSOLUTE: (
                "Does an absolute date replace the period in days, and if so which date?",
                'object {"applicable": boolean, "date": ISO YYYY-MM-DD or null}; '
                "false needs an explicit contest day-period rule; never invent days. "
                "Time-of-day/time-zone cutoffs are unsupported and must remain UNKNOWN",
            ),
        }
    )
    return tuple(
        Question(
            key=key,
            critical=key in CRITICAL_KEYS,
            ask=scope + ask,
            value_format=fmt,
            stage_context=stage or ticket.stage or "first-level review",
        )
        for key, (ask, fmt) in ((k, asks[k]) for k in FactKey if k not in RETIRED_KEYS)
    )


@dataclass(frozen=True)
class RequirementEvidence:
    verbatim_quote: str
    source_sha256s: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedRequirements:
    code_required: RequirementEvidence | None = None
    amount_required: RequirementEvidence | None = None
    compulsory_conditions: tuple[RequirementEvidence, ...] = ()


@dataclass(frozen=True)
class CustomerPosition:
    exception_basis: str | None = None
    code_citation: str | None = None


@dataclass(frozen=True)
class PacketPlan:
    code_required: RequirementEvidence | None = None
    # Legacy caller assertions are retained for decoding history, never accepted as proof.
    fulfilled_items: frozenset[str] = frozenset()
    frozen_pdf: bytes | None = None
    # Original official PDF bytes, bound to saved source hashes at fulfilment time.
    form_templates: tuple[bytes, ...] = ()
    # Customer-confirmed FORM_FIELDS values and the exact original customer_statement.
    customer_fields: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Promotion:
    key: FactKey
    reason: str
    evidence: RequirementEvidence | None = None
    customer_value: str | None = None


@dataclass(frozen=True)
class EffectiveTiers:
    tiers: Mapping[FactKey, FactTier] = field(default_factory=lambda: DEFAULT_TIERS)
    promotions: tuple[Promotion, ...] = ()
    compulsory_gaps: tuple[RequirementEvidence, ...] = ()


NO_REQUIREMENTS = VerifiedRequirements()
NO_POSITION = CustomerPosition()
EMPTY_PACKET_PLAN = PacketPlan()
DEFAULT_EFFECTIVE_TIERS = EffectiveTiers()


def effective_tiers(
    ticket_context: Ticket,
    verified_requirements: VerifiedRequirements = NO_REQUIREMENTS,
    customer_position: CustomerPosition = NO_POSITION,
    packet_plan: PacketPlan = EMPTY_PACKET_PLAN,
) -> EffectiveTiers:
    """Promote from explicit saved inputs only; never infer a rule or demote a fact."""
    tiers = dict(DEFAULT_TIERS)
    promotions: list[Promotion] = []
    scope = (
        f"{ticket_context.jurisdiction or ticket_context.city_id}/"
        f"{ticket_context.stage or 'first-level review'}"
    )

    def promote(
        key: FactKey,
        reason: str,
        evidence: RequirementEvidence | None = None,
        customer_value: str | None = None,
    ) -> None:
        tiers[key] = FactTier.MUST_HAVE
        promotions.append(Promotion(key, f"{scope}: {reason}", evidence, customer_value))

    # Historic customer exception/citation selections stay decodable but never promote a
    # research key: the service does not research or argue a defence.
    for evidence in (verified_requirements.code_required, packet_plan.code_required):
        if evidence is not None:
            promote(
                FactKey.GOVERNING_CODE_SECTION, "required form or packet needs provision", evidence
            )
    if verified_requirements.amount_required is not None:
        promote(
            FactKey.FINE_AMOUNT,
            "process requires amount or deposit",
            verified_requirements.amount_required,
        )
    return EffectiveTiers(
        MappingProxyType(tiers), tuple(promotions), verified_requirements.compulsory_conditions
    )
