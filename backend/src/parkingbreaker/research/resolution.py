"""Map evidence gaps to the v6 resolution ladder (sections 5.2 and 5.3). Pure, no I/O.

The evidence gate remains critical_problems. Customer answers and disclosed defaults
will resolve operational requirements separately; they never become official findings.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum

from parkingbreaker import case_state
from parkingbreaker.json_types import JsonValue
from parkingbreaker.mail_config import MailClass
from parkingbreaker.mail_timing import (
    ClockStart,
    DeadlinePeriod,
    DeadlineRule,
    clock_start_date,
    deadline_for_period,
    latest_safe_send_date,
    parse_deadline_period,
)
from parkingbreaker.research.findings import (
    Finding,
    FindingStatus,
    apply_quote_guard,
    apply_value_bounds,
    consolidate,
    critical_problems,
    finding_in_scope,
    validate_value,
)
from parkingbreaker.research.fulfilment import (
    packet_fulfillment_pending,
    requirement_interpretable,
    standing_question_applies,
)
from parkingbreaker.research.ladder import (
    Ask,
    CountingChoice,
    CountingRule,
    CriticalFact,
    CustomerQuestion,
    DeadlineChoice,
    Default,
    Gap,
    GapKind,
    LadderStep,
    Recover,
    Stop,
    StopReason,
    earliest_deadline,
    hard_stop_for,
    match_address_choice,
    next_step,
    normalize_address,
)
from parkingbreaker.research.payment_effects import (
    PaymentAction,
    PaymentOutcome,
    PaymentRule,
    PaymentScope,
    evaluate_payment_rule,
)
from parkingbreaker.research.procedure import same_stage
from parkingbreaker.research.questions import (
    CRITICAL_FACT_FOR,
    DEFAULT_EFFECTIVE_TIERS,
    EMPTY_PACKET_PLAN,
    LIST_KEYS,
    NO_POSITION,
    NO_REQUIREMENTS,
    CustomerPosition,
    EffectiveTiers,
    FactKey,
    PacketPlan,
    Ticket,
    VerifiedRequirements,
    effective_tiers,
)
from parkingbreaker.research.sources import AuthorityClass, SourceRecord
from parkingbreaker.research.workflow import unsupported_workflow_requirements


@dataclass(frozen=True)
class RecoveryProgress:
    re_extraction_done: bool = False
    broadened_search_done: bool = False


@dataclass(frozen=True)
class PendingResearch:
    fact: CriticalFact
    keys: tuple[FactKey, ...]
    step: LadderStep
    official_addresses: tuple[Finding, ...] = ()


def official_address_candidates(
    candidates: Sequence[Finding],
    sources: Sequence[SourceRecord],
    *,
    ticket_context: Ticket | None = None,
) -> tuple[Finding, ...]:
    """Distinct addresses from guarded, bounded findings on actual official sources."""
    official = {s.sha256 for s in sources if s.authority_class is not AuthorityClass.SECONDARY}
    found: dict[str, Finding] = {}
    for finding in candidates:
        if (
            finding.key is FactKey.MAILING_ADDRESS
            and finding.status is FindingStatus.CONFIRMED
            and isinstance(finding.value, dict)
            and official.intersection(finding.source_sha256s)
            and (ticket_context is None or finding_in_scope(finding, ticket_context))
        ):
            key = json.dumps(finding.value, sort_keys=True)
            found.setdefault(key, finding)
    return tuple(found.values())


def resolution_steps(
    findings: Sequence[Finding],
    candidates: Sequence[Finding],
    sources: Sequence[SourceRecord],
    progress: RecoveryProgress,
    *,
    asked: frozenset[CustomerQuestion] = frozenset(),
    resolved_keys: frozenset[FactKey] = frozenset(),
    extra_unresolved: frozenset[FactKey] = frozenset(),
    tiers: EffectiveTiers = DEFAULT_EFFECTIVE_TIERS,
    ticket_context: Ticket | None = None,
    packet_plan: PacketPlan = EMPTY_PACKET_PLAN,
    notice_issue_date: date | None = None,
    vehicle_relationship: str | None = None,
    initial_review_completed: bool | None = None,
) -> tuple[PendingResearch, ...]:
    findings = consolidate(
        apply_value_bounds(apply_quote_guard(findings, sources, ticket_context=ticket_context))
    )
    problems = critical_problems(
        findings,
        sources,
        tiers=tiers,
        ticket_context=ticket_context,
        packet_plan=packet_plan,
        notice_issue_date=notice_issue_date,
        vehicle_relationship=vehicle_relationship,
        initial_review_completed=initial_review_completed,
    )
    customer_gaps = {p.key for p in problems if p.reason.endswith("customer notice date missing")}
    # Only fully guarded and scoped rules may establish inability to fulfil S3.
    # Interpret/fulfilment gaps need customer materials, not another official search.
    fulfilment_keys = {p.key for p in problems if ": unfulfilled requirements:" in p.reason}
    evidence_gaps = {p.key for p in problems} - fulfilment_keys
    unresolved = (evidence_gaps | set(extra_unresolved)) - resolved_keys
    by_key = {f.key: f for f in findings}
    addresses = official_address_candidates(candidates, sources, ticket_context=ticket_context)
    pending: list[PendingResearch] = []
    for key in FactKey:
        if key not in fulfilment_keys or key in resolved_keys:
            continue
        finding = by_key[key]
        quote = finding.verbatim_quote or ""
        assert isinstance(finding.value, list)
        interpretable = all(
            isinstance(item, str) and requirement_interpretable(key, item, quote)
            for item in finding.value
        )
        if packet_plan.frozen_pdf is None and all(
            isinstance(item, str)
            and packet_fulfillment_pending(
                key, item, quote, plan=packet_plan, sources=sources, ticket=ticket_context
            )
            for item in finding.value
        ):
            # Research may finish before the customer has a packet to fulfill. The
            # official requirement remains in findings and critical problems, so packet
            # authorization will recheck it against the frozen PDF.
            continue
        if (
            key is FactKey.CONTEST_ELIGIBILITY
            and standing_question_applies(quote)
            and vehicle_relationship is None
            and CustomerQuestion.VEHICLE_RELATIONSHIP not in asked
        ):
            step: LadderStep = Ask(CustomerQuestion.VEHICLE_RELATIONSHIP)
        elif (
            not interpretable
            or (key is FactKey.CONTEST_ELIGIBILITY and vehicle_relationship is None)
            or (key is FactKey.PRIOR_STEP_REQUIREMENT and initial_review_completed is None)
        ):
            step = Stop(StopReason.REQUIRED_ITEMS_NOT_CONFIRMED, case_state.HardStop.S3)
        else:
            step = Stop(
                StopReason.REQUIREMENT_UNFULFILLABLE,
                case_state.HardStop.S3,
                quote,
            )
        pending.append(PendingResearch(CriticalFact.REQUIRED_ITEMS, (key,), step))
    unresolved -= fulfilment_keys
    for fact in CriticalFact:
        keys = tuple(
            key
            for key in FactKey
            if key in unresolved and CRITICAL_FACT_FOR.get(key, CriticalFact.REQUIRED_ITEMS) is fact
        )
        if not keys:
            continue
        conflicted = any(
            by_key[key].status is FindingStatus.CONFLICTED for key in keys if key in by_key
        )
        quote_failed = any("quote guard:" in f.notes for f in candidates if f.key in keys)
        gap = Gap(
            fact,
            GapKind.CONFLICTING if conflicted else GapKind.UNCONFIRMED,
            quote_guard_failed=quote_failed,
            re_extraction_done=progress.re_extraction_done,
            # A verified notice-based rule needs customer notice data, not another
            # official search. Do not manufacture an unexecutable recovery step.
            broadened_search_done=progress.broadened_search_done
            or all(
                key in customer_gaps or (key in extra_unresolved and key not in evidence_gaps)
                for key in keys
            ),
            official_address_count=len(addresses),
            asked=asked,
        )
        step = next_step(gap)
        if isinstance(step, Default) and fact is CriticalFact.COUNTING_RULE:
            step = Stop(
                StopReason.DEADLINE_NOT_ESTABLISHED,
                case_state.HardStop.S7 if conflicted else case_state.HardStop.S4,
            )
        pending.append(
            PendingResearch(
                fact,
                keys,
                step,
                addresses if fact is CriticalFact.MAILING_ADDRESS else (),
            )
        )
    return tuple(pending)


class VehicleRelationship(StrEnum):
    """Closed customer choices for the S3 standing question; never official findings."""

    REGISTERED_OWNER = "REGISTERED_OWNER"
    DRIVER_NOT_OWNER = "DRIVER_NOT_OWNER"
    BUSINESS_OR_FLEET_VEHICLE = "BUSINESS_OR_FLEET_VEHICLE"
    RENTAL_OR_LEASE_RENTER = "RENTAL_OR_LEASE_RENTER"
    AUTHORISED_REPRESENTATIVE = "AUTHORISED_REPRESENTATIVE"
    OTHER = "OTHER"


@dataclass(frozen=True)
class NoticeFact:
    value: JsonValue
    verbatim_quote: str


@dataclass(frozen=True)
class TicketNoticeFacts:
    """Transcribed printed instructions, explicitly confirmed by the customer."""

    instructions: str
    confirmed: bool = False
    deadline_days: NoticeFact | None = None
    deadline_trigger: NoticeFact | None = None
    mailing_address: NoticeFact | None = None
    stage: NoticeFact | None = None

    def __post_init__(self) -> None:
        if type(self.confirmed) is not bool:
            raise ValueError("notice confirmation must be a boolean")
        if self.confirmed and not self.instructions.strip():
            raise ValueError("confirmed notice instructions must not be empty")
        if self.confirmed and any(
            fact is not None
            and (not fact.verbatim_quote.strip() or fact.verbatim_quote not in self.instructions)
            for fact in (
                self.deadline_days,
                self.deadline_trigger,
                self.mailing_address,
                self.stage,
            )
        ):
            raise ValueError("notice fact quotes must come from the confirmed printed text")


@dataclass(frozen=True)
class NoticeConflict:
    field: str
    printed_value: JsonValue
    official_value: JsonValue
    printed_quote: str
    official_quote: str


def detect_notice_conflicts(
    ticket_notice_facts: TicketNoticeFacts, findings: Sequence[Finding]
) -> tuple[NoticeConflict, ...]:
    """Compare confirmed notice facts to guarded official findings; never resolve a rule."""
    notice = ticket_notice_facts
    if not notice.confirmed:
        return ()
    conflicts: list[NoticeConflict] = []
    keys = {
        FactKey.DEADLINE_DAYS: ("deadline_days", notice.deadline_days),
        FactKey.DEADLINE_CLOCK_START: ("deadline_trigger", notice.deadline_trigger),
        FactKey.MAILING_ADDRESS: ("mailing_address", notice.mailing_address),
    }
    for f in findings:
        # Consolidated conflicts retain both individual official quotes.
        for finding in f.evidence or (f,):
            if (
                finding.status is not FindingStatus.CONFIRMED
                or not finding.source_sha256s
                or not finding.verbatim_quote
            ):
                continue
            field, printed = keys.get(finding.key, ("", None))
            if (
                printed is not None
                and printed.verbatim_quote.strip()
                and printed.verbatim_quote in notice.instructions
            ):
                left, right = printed.value, finding.value
                if finding.key is FactKey.MAILING_ADDRESS:
                    addresses = [right]
                    if isinstance(right, dict):
                        addresses = [
                            " ".join(str(right[k]) for k in fields if right.get(k))
                            for fields in (
                                (
                                    "recipient",
                                    "attention",
                                    "line1",
                                    "line2",
                                    "city",
                                    "state",
                                    "zip_code",
                                ),
                                ("line1", "line2", "city", "state", "zip_code"),
                            )
                        ]
                    if isinstance(left, dict):
                        left = " ".join(
                            str(left[k])
                            for k in (
                                "recipient",
                                "attention",
                                "line1",
                                "line2",
                                "city",
                                "state",
                                "zip_code",
                            )
                            if left.get(k)
                        )
                    same = isinstance(left, str) and any(
                        isinstance(address, str)
                        and normalize_address(left) == normalize_address(address)
                        for address in addresses
                    )
                else:
                    same = left == right
                if not same:
                    conflicts.append(
                        NoticeConflict(
                            field,
                            left,
                            finding.value,
                            printed.verbatim_quote,
                            finding.verbatim_quote,
                        )
                    )
            official_stage = finding.stage or (
                finding.procedure_assessment.stage if finding.procedure_assessment else None
            )
            if (
                notice.stage is not None
                and notice.stage.verbatim_quote.strip()
                and notice.stage.verbatim_quote in notice.instructions
                and isinstance(notice.stage.value, str)
                and official_stage
                and not same_stage(notice.stage.value, official_stage)
            ):
                conflicts.append(
                    NoticeConflict(
                        "stage",
                        notice.stage.value,
                        official_stage,
                        notice.stage.verbatim_quote,
                        finding.scope_quote or finding.verbatim_quote,
                    )
                )
    return tuple(conflict for i, conflict in enumerate(conflicts) if conflict not in conflicts[:i])


@dataclass(frozen=True)
class ResearchAnswers:
    """Customer-provided answers, with answered questions tracked separately.

    A None answer means the customer could not supply it. It spends that question;
    it does not silently become a fact or cause the same question to be asked again.
    """

    notice_issue_date: date | None = None
    printed_deadline: date | None = None
    address_choice: str | None = None
    prior_step_required: bool | None = None
    answered: frozenset[CustomerQuestion] = frozenset()
    # Verbatim text from the notice (agency name, stage heading). Intake inputs for the
    # jurisdiction resolver and stage-aware research; never researched facts.
    issuing_agency: str | None = None
    notice_stage: str | None = None
    vehicle_relationship: VehicleRelationship | None = None
    initial_review_completed: bool | None = None
    paid_fine: bool | None = field(default=None, metadata={"omit_if_none": True})
    paid_hearing_deposit: bool | None = field(default=None, metadata={"omit_if_none": True})
    entered_plan: bool | None = field(default=None, metadata={"omit_if_none": True})

    def __post_init__(self) -> None:
        for action_answer in (self.paid_fine, self.paid_hearing_deposit, self.entered_plan):
            if action_answer is not None and type(action_answer) is not bool:
                raise ValueError("payment action answers must be a boolean or None")
        if (
            self.initial_review_completed is not None
            and type(self.initial_review_completed) is not bool
        ):
            raise ValueError("initial review completion must be a boolean or None")
        for value in (self.notice_issue_date, self.printed_deadline):
            if value is not None and type(value) is not date:
                raise ValueError("notice dates must be date values or None")
        if self.address_choice is not None and (
            not isinstance(self.address_choice, str) or not self.address_choice.strip()
        ):
            raise ValueError("address choice must be a non-empty string or None")
        if self.prior_step_required is not None and not isinstance(self.prior_step_required, bool):
            raise ValueError("prior step answer must be a boolean or None")
        for text in (self.issuing_agency, self.notice_stage):
            if text is not None and (not isinstance(text, str) or not text.strip()):
                raise ValueError("notice text answers must be non-empty strings or None")
        if self.vehicle_relationship is not None and not isinstance(
            self.vehicle_relationship, VehicleRelationship
        ):
            raise ValueError("vehicle relationship must be a VehicleRelationship value or None")
        if any(not isinstance(question, CustomerQuestion) for question in self.answered):
            raise ValueError("answered must contain CustomerQuestion values")


def answer_question(
    answers: ResearchAnswers,
    question: CustomerQuestion,
    value: date | str | bool | VehicleRelationship | None,
) -> ResearchAnswers:
    """Validate one answer from the research question list, without interpreting prose."""
    answered = answers.answered | {question}
    match question:
        case CustomerQuestion.NOTICE_ISSUE_DATE | CustomerQuestion.NOTICE_PRINTED_DEADLINE:
            if value is not None and type(value) is not date:
                raise ValueError("notice dates must be date values or None")
            assert isinstance(value, date) or value is None
            if question is CustomerQuestion.NOTICE_ISSUE_DATE:
                return replace(answers, notice_issue_date=value, answered=answered)
            return replace(answers, printed_deadline=value, answered=answered)
        case CustomerQuestion.NOTICE_ADDRESS_CHOICE:
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError("address choice must be a non-empty string or None")
            assert isinstance(value, str) or value is None
            return replace(answers, address_choice=value, answered=answered)
        case CustomerQuestion.NOTICE_PRIOR_STEP:
            if value is not None and not isinstance(value, bool):
                raise ValueError("prior step answer must be a boolean or None")
            assert isinstance(value, bool) or value is None
            return replace(answers, prior_step_required=value, answered=answered)
        case CustomerQuestion.NOTICE_ISSUING_AGENCY | CustomerQuestion.NOTICE_STAGE:
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError("notice text answers must be non-empty strings or None")
            assert isinstance(value, str) or value is None
            if question is CustomerQuestion.NOTICE_ISSUING_AGENCY:
                return replace(answers, issuing_agency=value, answered=answered)
            return replace(answers, notice_stage=value, answered=answered)
        case CustomerQuestion.VEHICLE_RELATIONSHIP:
            if value is not None and not isinstance(value, VehicleRelationship):
                raise ValueError("vehicle relationship must be a VehicleRelationship value or None")
            return replace(answers, vehicle_relationship=value, answered=answered)
        case (
            CustomerQuestion.PAID_FINE
            | CustomerQuestion.PAID_HEARING_DEPOSIT
            | CustomerQuestion.ENTERED_PLAN
        ):
            if value is not None and type(value) is not bool:
                raise ValueError("payment action answers must be a boolean or None")
            assert isinstance(value, bool) or value is None
            if question is CustomerQuestion.PAID_FINE:
                return replace(answers, paid_fine=value, answered=answered)
            if question is CustomerQuestion.PAID_HEARING_DEPOSIT:
                return replace(answers, paid_hearing_deposit=value, answered=answered)
            return replace(answers, entered_plan=value, answered=answered)
        case _:
            raise ValueError(f"{question} is not a research-resolution answer")


@dataclass(frozen=True)
class ResearchResolution:
    """Operational requirements resolved without rewriting official research evidence."""

    mailing_address: Finding | None
    deadline: DeadlineChoice | None
    official_deadline_candidates: tuple[date, ...]
    counting: CountingChoice | None
    pending: tuple[PendingResearch, ...]
    customer_answers: ResearchAnswers
    timing_disclosures: tuple[str, ...] = ()
    fulfilled_requirements: tuple[Finding, ...] = ()
    notice_conflicts: tuple[NoticeConflict, ...] = ()
    awaiting_packet: bool = False

    def deadline_feasible(self, send_date: date, mail_class: MailClass) -> bool:
        """Recompute against an explicit civil date, using the guarded deadline/rule."""
        return (
            self.deadline is not None
            and self.counting is not None
            and not self.counting.disclose
            and send_date
            <= latest_safe_send_date(
                self.deadline.deadline, DeadlineRule(self.counting.rule.value.lower()), mail_class
            )
        )

    @property
    def research_complete(self) -> bool:
        """Whether research can hand off to drafting, independent of packet fulfillment."""
        return (
            not self.pending
            and self.mailing_address is not None
            and self.deadline is not None
            and self.counting is not None
            and not self.counting.disclose
        )

    @property
    def ready(self) -> bool:
        return self.research_complete and not self.awaiting_packet

    @property
    def stops(self) -> tuple[Stop, ...]:
        return tuple(p.step for p in self.pending if isinstance(p.step, Stop))

    @property
    def questions(self) -> tuple[Ask, ...]:
        if self.stops:
            return ()
        return tuple(p.step for p in self.pending if isinstance(p.step, Ask))


def _official_values(
    key: FactKey,
    candidates: Sequence[Finding],
    sources: Sequence[SourceRecord],
    ticket: Ticket,
) -> tuple[Finding, ...]:
    official = {s.sha256 for s in sources if s.authority_class is not AuthorityClass.SECONDARY}
    return tuple(
        f
        for f in candidates
        if (
            f.key is key
            and f.status is FindingStatus.CONFIRMED
            and validate_value(key, f.value) is None
            and official.intersection(f.source_sha256s)
            and finding_in_scope(f, ticket)
        )
    )


def _address_label(finding: Finding) -> str:
    assert isinstance(finding.value, dict)
    return ", ".join(
        item
        for key in ("recipient", "attention", "line1", "line2", "city", "state", "zip_code")
        if isinstance(item := finding.value.get(key), str) and item
    )


def resolve_research(
    ticket: Ticket,
    findings: Sequence[Finding],
    candidates: Sequence[Finding],
    sources: Sequence[SourceRecord],
    progress: RecoveryProgress,
    answers: ResearchAnswers,
    *,
    verified_requirements: VerifiedRequirements = NO_REQUIREMENTS,
    customer_position: CustomerPosition = NO_POSITION,
    packet_plan: PacketPlan = EMPTY_PACKET_PLAN,
    ticket_notice_facts: TicketNoticeFacts | None = None,
    payment_rules: Sequence[PaymentRule] = (),
    payment_scope: PaymentScope | None = None,
) -> ResearchResolution:
    """Apply customer facts and allowed timing defaults to a saved guarded result.

    No fetches, extraction, clocks or new retry budgets. Official findings remain intact.
    An absolute printed deadline can supply timing, but cannot waive another critical
    requirement. An address choice returns the original official finding, never a typed
    customer address. Defaults apply only after automatic recovery has been exhausted.
    """
    tiers = effective_tiers(ticket, verified_requirements, customer_position, packet_plan)
    findings = consolidate(
        apply_value_bounds(apply_quote_guard(findings, sources, ticket_context=ticket))
    )
    candidates = apply_value_bounds(apply_quote_guard(candidates, sources, ticket_context=ticket))
    by_key = {f.key: f for f in findings}
    unresolved = {
        p.key
        for p in critical_problems(
            findings,
            sources,
            tiers=tiers,
            ticket_context=ticket,
            packet_plan=packet_plan,
            notice_issue_date=answers.notice_issue_date,
            vehicle_relationship=answers.vehicle_relationship,
            initial_review_completed=answers.initial_review_completed,
        )
    }
    recovery_keys = {
        key
        for pending in resolution_steps(
            findings,
            candidates,
            sources,
            progress,
            tiers=tiers,
            ticket_context=ticket,
            packet_plan=packet_plan,
            notice_issue_date=answers.notice_issue_date,
            vehicle_relationship=answers.vehicle_relationship,
            initial_review_completed=answers.initial_review_completed,
        )
        if isinstance(pending.step, Recover)
        for key in pending.keys
    }
    resolved: set[FactKey] = set()
    extra: set[FactKey] = set()
    disclosures: list[str] = []
    stops: list[PendingResearch] = []
    mailing: Finding | None = None
    addresses = official_address_candidates(candidates, sources, ticket_context=ticket)
    if FactKey.MAILING_ADDRESS not in unresolved:
        mailing = by_key.get(FactKey.MAILING_ADDRESS)
    elif (
        len(addresses) >= 2
        and answers.address_choice is not None
        and progress.broadened_search_done
        and FactKey.MAILING_ADDRESS not in recovery_keys
    ):
        selected = match_address_choice(
            answers.address_choice, [_address_label(f) for f in addresses]
        )
        if isinstance(selected, Stop):
            if by_key.get(FactKey.MAILING_ADDRESS) is not None and (
                by_key[FactKey.MAILING_ADDRESS].status is FindingStatus.CONFLICTED
            ):
                selected = replace(selected, hard_stop=case_state.HardStop.S7)
            stops.append(
                PendingResearch(CriticalFact.MAILING_ADDRESS, (FactKey.MAILING_ADDRESS,), selected)
            )
        else:
            mailing = next(f for f in addresses if _address_label(f) == selected)
            resolved.add(FactKey.MAILING_ADDRESS)

    rule: CountingChoice | None = None
    if FactKey.DEADLINE_RULE not in unresolved:
        value = by_key[FactKey.DEADLINE_RULE].value
        assert isinstance(value, str)
        rule = CountingChoice(CountingRule[value], disclose=False)
    elif progress.broadened_search_done and FactKey.DEADLINE_RULE not in recovery_keys:
        rule = CountingChoice(CountingRule.RECEIPT, disclose=True)
        disclosures.append(
            "The counting rule could not be confirmed; we planned for arrival by the deadline."
        )

    # An integer is a calendar-day period; an object carries an explicit business-day unit
    # and/or a stated time cutoff (mail_timing.parse_deadline_period).
    periods: set[DeadlinePeriod] = {
        period
        for f in _official_values(FactKey.DEADLINE_DAYS, candidates, sources, ticket)
        if (period := parse_deadline_period(f.value)) is not None
    }
    starts = {
        f.value
        for f in _official_values(FactKey.DEADLINE_CLOCK_START, candidates, sources, ticket)
        if isinstance(f.value, str)
    }
    triggers: set[date] = set()
    if "ISSUE_DATE" in starts:
        triggers.add(ticket.issue_date)
    if "NOTICE_DATE" in starts and answers.notice_issue_date is not None:
        triggers.add(
            clock_start_date(ticket.issue_date, ClockStart.NOTICE_DATE, answers.notice_issue_date)
        )
    missing_trigger = "NOTICE_DATE" in starts and answers.notice_issue_date is None
    absolute = by_key.get(FactKey.DEADLINE_ABSOLUTE)
    absolute_date: date | None = None
    if (
        absolute is not None
        and FactKey.DEADLINE_ABSOLUTE not in unresolved
        and isinstance(absolute.value, dict)
        and absolute.value.get("applicable") is True
    ):
        raw_date = absolute.value.get("date")
        if isinstance(raw_date, str):
            absolute_date = date.fromisoformat(raw_date)
    official_dates = tuple(
        sorted(
            {
                deadline_for_period(trigger, period, ClockStart.ISSUE_DATE)
                for period in periods
                for trigger in triggers
            }
        )
    )
    if absolute_date is not None:
        official_dates = (absolute_date,)
        missing_trigger = False
    deadline: DeadlineChoice | None = None
    deadline_keys = {FactKey.DEADLINE_DAYS, FactKey.DEADLINE_CLOCK_START}
    timing_recovery_complete = (
        progress.broadened_search_done
        or not unresolved & deadline_keys
        or (
            unresolved & deadline_keys == {FactKey.DEADLINE_CLOCK_START}
            and by_key[FactKey.DEADLINE_CLOCK_START].status is FindingStatus.CONFIRMED
            and starts == {"NOTICE_DATE"}
        )
    )
    ambiguous = len(official_dates) > 1 or bool(unresolved & deadline_keys) or missing_trigger
    if not ambiguous and len(official_dates) == 1:
        deadline = earliest_deadline(official_dates[0], answers.printed_deadline)
    elif (
        answers.printed_deadline is not None
        and timing_recovery_complete
        and not recovery_keys & deadline_keys
    ):
        # Every candidate comes from saved verified evidence. Choose the earliest known
        # official candidate and the printed notice, retaining all candidates for display.
        deadline = earliest_deadline(
            min(official_dates) if official_dates else None, answers.printed_deadline
        )
        disclosures.append(
            "We used your printed notice deadline and the earliest verified official "
            "candidate, when available."
        )
    if deadline is not None:
        resolved.update(deadline_keys)
        if deadline.disclose:
            disclosures.append(
                "Your notice and the official rule give different deadlines; "
                "we used the earlier date."
            )
    elif not unresolved & deadline_keys:
        # Official NOTICE_DATE rules still need the customer's actual notice date.
        extra.add(FactKey.DEADLINE_CLOCK_START)

    validated_findings = tuple(
        mailing if f.key is FactKey.MAILING_ADDRESS and mailing is not None else f for f in findings
    )
    remaining = {
        p.key
        for p in critical_problems(
            validated_findings,
            sources,
            tiers=tiers,
            ticket_context=ticket,
            packet_plan=packet_plan,
            printed_deadline=answers.printed_deadline if deadline is not None else None,
            notice_issue_date=answers.notice_issue_date,
            vehicle_relationship=answers.vehicle_relationship,
            initial_review_completed=answers.initial_review_completed,
        )
    }
    resolved = unresolved - remaining
    pending = list(
        resolution_steps(
            findings,
            candidates,
            sources,
            progress,
            asked=answers.answered,
            resolved_keys=frozenset(resolved),
            extra_unresolved=frozenset(extra),
            tiers=tiers,
            ticket_context=ticket,
            packet_plan=packet_plan,
            notice_issue_date=answers.notice_issue_date,
            vehicle_relationship=answers.vehicle_relationship,
            initial_review_completed=answers.initial_review_completed,
        )
    )
    if stops:
        pending = [p for p in pending if p.fact is not CriticalFact.MAILING_ADDRESS] + stops
    workflow_limits = unsupported_workflow_requirements(ticket, findings, sources)
    if workflow_limits:
        keys = {key for key, _quote in workflow_limits}
        pending = [
            PendingResearch(
                CriticalFact.REQUIRED_ITEMS,
                (key,),
                Stop(StopReason.REQUIREMENT_UNFULFILLABLE, case_state.HardStop.S3, quote),
            )
            for key, quote in workflow_limits
        ] + [p for p in pending if not keys.intersection(p.keys)]
    if answers.prior_step_required and answers.initial_review_completed is not True:
        pending.append(
            PendingResearch(
                CriticalFact.DEADLINE,
                (),
                Stop(StopReason.PRIOR_STEP_REQUIRED, case_state.HardStop.S3),
            )
        )
    awaiting_packet = packet_plan.frozen_pdf is None and any(
        (finding := by_key.get(key)) is not None
        and finding.status is FindingStatus.CONFIRMED
        and isinstance(finding.value, list)
        and bool(finding.value)
        and (
            key in remaining
            or any(
                isinstance(item, str) and re.search(r"\bforms?\b", item, re.I)
                for item in finding.value
            )
        )
        and all(
            isinstance(item, str)
            and packet_fulfillment_pending(
                key,
                item,
                finding.verbatim_quote or "",
                plan=packet_plan,
                sources=sources,
                ticket=ticket,
            )
            for item in finding.value
        )
        for key in set(remaining) | {FactKey.MANDATORY_FORMS, FactKey.REQUIRED_ITEMS}
    )
    # Optional action rules use the existing pending path. They are never packet
    # contents or an inferred fact about what the customer has done.
    for payment_rule in payment_rules:
        conflicting_effects = (
            len(
                {
                    candidate.effect
                    for candidate in payment_rules
                    if candidate.action == payment_rule.action
                }
            )
            > 1
        )
        scope_matches_ticket = (
            payment_scope is not None
            and ticket.jurisdiction == payment_scope.issuer
            and ticket.stage is not None
            and same_stage(ticket.stage, payment_scope.stage)
        )
        if (
            conflicting_effects
            or not scope_matches_ticket
            or payment_scope is None
            or not isinstance(payment_rule.action, PaymentAction)
        ):
            payment_step: LadderStep = Stop(
                StopReason.REQUIRED_ITEMS_NOT_CONFIRMED, case_state.HardStop.S3
            )
        else:
            question, occurred = {
                PaymentAction.FINE_PAYMENT: (CustomerQuestion.PAID_FINE, answers.paid_fine),
                PaymentAction.HEARING_DEPOSIT: (
                    CustomerQuestion.PAID_HEARING_DEPOSIT,
                    answers.paid_hearing_deposit,
                ),
                PaymentAction.PLAN_ENROLLMENT: (
                    CustomerQuestion.ENTERED_PLAN,
                    answers.entered_plan,
                ),
            }[payment_rule.action]
            payment_decision = evaluate_payment_rule(
                payment_rule,
                sources,
                payment_scope,
                action_occurred=occurred,
                answered=question in answers.answered,
            )
            if payment_decision.outcome is PaymentOutcome.NO_ADDITIONAL_GATE:
                continue
            if payment_decision.outcome is PaymentOutcome.ASK_CUSTOMER:
                payment_step = Ask(question)
            else:
                payment_step = Stop(
                    StopReason.REQUIREMENT_UNFULFILLABLE
                    if payment_decision.outcome is PaymentOutcome.REVIEW_BAR
                    else StopReason.REQUIRED_ITEMS_NOT_CONFIRMED,
                    case_state.HardStop.S3,
                    payment_decision.official_quote,
                )
        pending.append(
            PendingResearch(
                CriticalFact.REQUIRED_ITEMS, (FactKey.TICKET_SPECIFIC_REQUIREMENTS,), payment_step
            )
        )
    return ResearchResolution(
        mailing,
        deadline,
        official_dates,
        rule,
        tuple(pending),
        answers,
        tuple(disclosures),
        tuple(
            f
            for f in validated_findings
            if f.key in LIST_KEYS - {FactKey.CODE_EXCEPTIONS}
            and f.status is FindingStatus.CONFIRMED
            and f.key not in remaining
        ),
        detect_notice_conflicts(
            ticket_notice_facts,
            tuple(
                f
                for f in apply_value_bounds(
                    apply_quote_guard(findings, sources, ticket_context=ticket)
                )
                if any(
                    s.sha256 in f.source_sha256s
                    and s.authority_class is not AuthorityClass.SECONDARY
                    and s.http_status == 200
                    for s in sources
                )
                and (
                    f.jurisdiction is None
                    or ticket.jurisdiction is None
                    or f.jurisdiction == ticket.jurisdiction
                )
            ),
        )
        if ticket_notice_facts is not None
        else (),
        awaiting_packet,
    )


def resolution_event(
    resolution: ResearchResolution,
) -> case_state.ResearchComplete | case_state.CustomerNeeded | case_state.Stopped | None:
    """The existing letter-machine event to apply in RESEARCHING; no provider effects.

    A stop wins over other questions. None means automatic recovery is still pending.
    On a customer reply the caller applies CustomerAnswered first, then this event.
    """
    if resolution.stops:
        pending = next(p for p in resolution.pending if isinstance(p.step, Stop))
        assert isinstance(pending.step, Stop)
        stop = pending.step
        # Legacy saved steps omit the identifier; requirements still map to S3.
        hard_stop = stop.hard_stop or (
            case_state.HardStop.S3
            if stop.reason is StopReason.PRIOR_STEP_REQUIRED
            else hard_stop_for(pending.fact)
        )
        incompatible = (
            stop.reason is StopReason.REQUIREMENT_UNFULFILLABLE
            and stop.official_quote is not None
            and bool(stop.official_quote.strip())
        )
        return case_state.Stopped(
            case_state.StopReason.PROCESS_INCOMPATIBLE
            if incompatible
            else case_state.StopReason.NOT_YET_VERIFIED,
            stop.official_quote
            if incompatible and stop.official_quote is not None
            else stop.reason.value,
            hard_stop,
        )
    if resolution.research_complete:
        return case_state.ResearchComplete()
    if resolution.questions:
        return case_state.CustomerNeeded(resolution.questions[0].question.value)
    return None
