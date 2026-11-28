"""Research findings and the gates that decide whether research may be released.

A finding is a claimed answer to one FactKey. Nothing an extractor (or a page) says is
trusted on its own:

1. apply_quote_guard: a CONFIRMED finding must quote one of its cited sources verbatim
   (after whitespace normalisation only), and the quote must state the value
   (support.value_supported_by_quote). Otherwise it becomes UNKNOWN. Cited sources where
   the quote does not appear are dropped from the finding.
2. apply_value_bounds: a CONFIRMED value must have the right shape and pass sanity bounds
   (for example DEADLINE_DAYS within 1..365). Otherwise it becomes UNKNOWN.
3. consolidate: one finding per key. Disagreeing CONFIRMED values become CONFLICTED.
4. release_decision: every critical key must be CONFIRMED, backed by at least one
   non-SECONDARY source. Absence of requirements needs verified evidence too.

Pure: no I/O.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from typing import NamedTuple

from parkingbreaker.json_types import JsonValue
from parkingbreaker.mail_timing import ClockStart, DeadlineRule, parse_deadline_period
from parkingbreaker.research.fulfilment import requirement_fulfilled
from parkingbreaker.research.procedure import (
    SILENCE_KEYS,
    ProcedureAssessment,
    assessment_problem,
    explicit_form_requirement_stage,
    jurisdiction_in_text,
    procedure_sections,
    procedure_stage,
    quote_jurisdiction_matches,
    quote_procedure_section,
    quote_procedure_sections,
    quote_procedure_stage,
    requirement_sentences,
    same_stage,
    unread_linked_artifacts,
)
from parkingbreaker.research.questions import (
    DEFAULT_EFFECTIVE_TIERS,
    DEFAULT_TIERS,
    EMPTY_PACKET_PLAN,
    LIST_KEYS,
    EffectiveTiers,
    FactKey,
    FactTier,
    PacketPlan,
    Ticket,
)
from parkingbreaker.research.route_requirements import (
    compulsory_route_stage,
    nonlocal_route_statements,
    possible_route_requirement,
    quoted_route_stage,
    route_local_requirement,
)
from parkingbreaker.research.sources import AuthorityClass, SourceRecord, is_code_publisher
from parkingbreaker.research.support import (
    address_purpose_problem,
    clock_quote_supported_by_source,
    value_supported_by_quote,
)
from parkingbreaker.research.veto_codes import VetoCode

MIN_QUOTE_CHARS = 12
DEADLINE_DAYS_BOUNDS = (1, 365)
FINE_AMOUNT_MAX = 10_000


class EvidenceBasis(StrEnum):
    NONE_STATED_IN_OFFICIAL_PROCEDURE = "NONE_STATED_IN_OFFICIAL_PROCEDURE"


class FindingStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"
    CONFLICTED = "CONFLICTED"


class UnknownReason(StrEnum):
    MISSING_ANSWER = "MISSING_ANSWER"
    MALFORMED_ANSWER = "MALFORMED_ANSWER"
    INVALID_JSON = "INVALID_JSON"
    NO_SOURCES = "NO_SOURCES"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    TIME_EXHAUSTED = "TIME_EXHAUSTED"
    QUOTE_FAILURE = "QUOTE_FAILURE"
    INVALID_VALUE = "INVALID_VALUE"
    UNANSWERABLE = "UNANSWERABLE"


@dataclass(frozen=True)
class Finding:
    key: FactKey
    value: JsonValue
    status: FindingStatus
    verbatim_quote: str | None
    source_sha256s: tuple[str, ...]
    notes: str = ""
    unknown_reason: UnknownReason | None = None
    jurisdiction: str | None = None
    stage: str | None = None
    evidence: tuple[Finding, ...] = ()
    evidence_basis: EvidenceBasis | None = None
    procedure_assessment: ProcedureAssessment | None = None
    scope_quote: str | None = None


class ReleaseDecision(NamedTuple):
    ok: bool
    reasons: tuple[str, ...]


def _add_note(finding: Finding, note: str) -> str:
    return f"{finding.notes}; {note}" if finding.notes else note


def _downgrade(finding: Finding, note: str) -> Finding:
    return replace(
        finding,
        status=FindingStatus.UNKNOWN,
        value=None,
        unknown_reason=UnknownReason.QUOTE_FAILURE
        if note.startswith("quote guard:")
        else UnknownReason.INVALID_VALUE,
        notes=_add_note(finding, note),
    )


# --- Quote guard ---------------------------------------------------------------------


def normalize_whitespace(text: str) -> str:
    """Collapse every run of Unicode whitespace to one space and trim. Nothing else."""
    return " ".join(text.split())


def verify_quote(quote: str, source_text: str) -> bool:
    """Exact substring match after whitespace normalisation of both sides.

    No case folding, no Unicode normalisation, no punctuation changes: a lookalike
    character or a changed word fails. Quotes shorter than MIN_QUOTE_CHARS fail.
    """
    q = normalize_whitespace(quote)
    if len(q) < MIN_QUOTE_CHARS:
        return False
    return q in normalize_whitespace(source_text)


def apply_quote_guard(
    findings: Iterable[Finding],
    sources: Sequence[SourceRecord],
    *,
    ticket_context: Ticket | None = None,
) -> tuple[Finding, ...]:
    """Force CONFIRMED findings to UNKNOWN unless the quote is verbatim in a cited source
    and states the claimed value.

    Surviving findings keep only the cited sources that actually contain the quote.
    NOT_APPLICABLE is accepted only for list facts, with a verified quote stating
    there are none; it becomes CONFIRMED with an empty list. Notes are never evidence.
    UNKNOWN and CONFLICTED findings pass through unchanged.
    """
    by_sha = {s.sha256: s for s in sources}
    guarded: list[Finding] = []
    for f in findings:
        if f.evidence:
            guarded.extend(consolidate(apply_value_bounds(apply_quote_guard(f.evidence, sources))))
            continue
        if f.evidence_basis is not None and f.status is not FindingStatus.CONFIRMED:
            guarded.append(_downgrade(f, "quote guard: procedural silence must be CONFIRMED"))
            continue
        if f.status is FindingStatus.NOT_APPLICABLE:
            # A model cannot waive a deadline, address or mail-in route. Only explicit
            # absence of list requirements can resolve through the ordinary quote guard.
            if f.key not in LIST_KEYS or (f.value is not None and f.value != []):
                guarded.append(_downgrade(f, "quote guard: invalid NOT_APPLICABLE claim"))
                continue
            f = replace(f, status=FindingStatus.CONFIRMED, value=[])
        if f.status is not FindingStatus.CONFIRMED:
            guarded.append(f)
            continue
        if (
            f.key in LIST_KEYS - {FactKey.CODE_EXCEPTIONS}
            and isinstance(f.value, list)
            and f.value
            and f.verbatim_quote is not None
            and " ".join(nonlocal_statements := nonlocal_route_statements(f.verbatim_quote))
            != normalize_whitespace(f.verbatim_quote)
            and not all(
                any(value_supported_by_quote(f.key, [item], s) for s in nonlocal_statements)
                for item in f.value
            )
        ):
            guarded.append(
                _downgrade(f, "quote guard: channel-local wording is not a global requirement")
            )
            continue
        if f.evidence_basis is EvidenceBasis.NONE_STATED_IN_OFFICIAL_PROCEDURE:
            a = f.procedure_assessment
            page = by_sha.get(a.source_sha256) if a is not None else None
            problem = (
                "basis only supports additional-requirement questions with value=[]"
                if f.key not in SILENCE_KEYS or f.value != []
                else "missing complete-procedure assessment"
                if a is None or page is None
                else assessment_problem(a, page, sources)
            )
            if (
                problem is None
                and a is not None
                and page is not None
                and (
                    a.source_sha256 not in f.source_sha256s
                    or not f.verbatim_quote
                    or not verify_quote(f.verbatim_quote, a.procedure_text)
                    or (f.jurisdiction is not None and f.jurisdiction != a.jurisdiction)
                    or (f.stage is not None and not same_stage(f.stage, a.stage))
                )
            ):
                problem = "procedure quote, hash or scope disagrees"
            if problem is None and unread_linked_artifacts(tuple(sources)):
                problem = VetoCode.UNREAD_LINKED_ARTIFACT.value
            if problem:
                guarded.append(_downgrade(f, f"quote guard: {problem}"))
                continue
            assert a is not None
            assert page is not None
            silence_index = len(guarded)
            guarded.append(replace(f, source_sha256s=(page.sha256,)))
            # Re-scan ALL saved official pages every time this guard runs. A new form
            # page can override an older completeness judgment without model re-extraction.
            for other in sources:
                if other.authority_class is AuthorityClass.SECONDARY or other.http_status != 200:
                    continue
                if is_code_publisher(other.final_url) and other.jurisdiction != a.jurisdiction:
                    continue
                for section in procedure_sections(other.text):
                    if "[Tab/accordion content]" in section.text and not re.search(
                        r"\b(?:parking|citations?|tickets?)\b", section.text, re.I
                    ):
                        continue
                    for quote in requirement_sentences(f.key, section.text):
                        other_stage = section.stage
                        if other_stage is not None and not same_stage(other_stage, a.stage):
                            qualified_stage = explicit_form_requirement_stage(quote)
                            if qualified_stage is not None:
                                if same_stage(qualified_stage, other_stage):
                                    continue
                                guarded[silence_index] = _downgrade(
                                    f,
                                    "quote guard: form stage disagrees with source section",
                                )
                                continue
                            if re.search(
                                r"\b(?:all|every|each) (?:parking citation )?"
                                r"(?:appeals?|reviews?|contests?)\b",
                                quote,
                                re.I,
                            ):
                                guarded[silence_index] = _downgrade(
                                    f, "quote guard: global requirement outside assessed section"
                                )
                            continue
                        stated_stage = procedure_stage(quote)
                        if (
                            f.key is not FactKey.PRIOR_STEP_REQUIREMENT
                            and stated_stage is not None
                            and not same_stage(stated_stage, a.stage)
                        ):
                            guarded[silence_index] = _downgrade(
                                f, "quote guard: cross-stage condition lacks its own section"
                            )
                            continue
                        if other_stage is None:
                            guarded[silence_index] = _downgrade(
                                f, "quote guard: possible requirement lacks procedure scope"
                            )
                            continue
                        guarded.append(
                            Finding(
                                f.key,
                                [quote],
                                FindingStatus.CONFIRMED,
                                quote,
                                (other.sha256,),
                                jurisdiction=a.jurisdiction,
                                stage=a.stage,
                            )
                        )
            continue
        if not f.verbatim_quote:
            guarded.append(_downgrade(f, "quote guard: no quote given"))
            continue
        if len(normalize_whitespace(f.verbatim_quote)) < MIN_QUOTE_CHARS:
            guarded.append(_downgrade(f, f"quote guard: quote under {MIN_QUOTE_CHARS} chars"))
            continue
        cited = [by_sha[sha] for sha in f.source_sha256s if sha in by_sha]
        if not cited:
            guarded.append(_downgrade(f, "quote guard: no cited source was fetched"))
            continue
        verified = tuple(s.sha256 for s in cited if verify_quote(f.verbatim_quote, s.text))
        if not verified:
            guarded.append(
                _downgrade(f, "quote guard: quote not found verbatim in any cited source")
            )
            continue
        if f.key is FactKey.MAILING_ADDRESS and all(
            by_sha[sha].authority_class in {AuthorityClass.STATUTE, AuthorityClass.SECONDARY}
            for sha in verified
        ):
            guarded.append(_downgrade(f, f"quote guard: {VetoCode.STATUTE_ONLY_ADDRESS}"))
            continue
        if any(
            is_code_publisher(by_sha[sha].final_url)
            and (
                by_sha[sha].authority_class is not AuthorityClass.STATUTE
                or by_sha[sha].jurisdiction is None
                or by_sha[sha].jurisdiction != f.jurisdiction
            )
            for sha in verified
        ):
            guarded.append(_downgrade(f, "quote guard: statute jurisdiction not established"))
            continue
        # Reject source-stage laundering independently of extraction's claimed labels.
        if f.jurisdiction is not None and any(
            by_sha[sha].authority_class not in {AuthorityClass.STATUTE, AuthorityClass.REGULATION}
            and not quote_jurisdiction_matches(by_sha[sha].text, f.verbatim_quote, f.jurisdiction)
            for sha in verified
        ):
            guarded.append(
                _downgrade(f, "quote guard: source section has another or unknown issuer")
            )
            continue
        if f.stage is not None:
            stages = {quote_procedure_stage(by_sha[sha].text, f.verbatim_quote) for sha in verified}
            timeless = all(
                by_sha[sha].authority_class
                in {
                    AuthorityClass.STATUTE,
                    AuthorityClass.REGULATION,
                }
                for sha in verified
            )
            if stages == {None} and not timeless:
                guarded.append(
                    _downgrade(f, "quote guard: source does not establish procedure stage")
                )
                continue
            if any(stage is not None and not same_stage(stage, f.stage) for stage in stages):
                guarded.append(_downgrade(f, "quote guard: source procedure has another stage"))
                continue
            if f.scope_quote is not None and not any(
                verify_quote(f.scope_quote, by_sha[sha].text)
                and (scope_section := quote_procedure_section(by_sha[sha].text, f.scope_quote))
                is not None
                and scope_section in quote_procedure_sections(by_sha[sha].text, f.verbatim_quote)
                for sha in verified
            ):
                guarded.append(_downgrade(f, "quote guard: scope quote not in fact's section"))
                continue
        supported = value_supported_by_quote(f.key, f.value, f.verbatim_quote)
        if f.key is FactKey.DEADLINE_CLOCK_START:
            supported = all(
                bool(clock_sections := quote_procedure_sections(by_sha[sha].text, f.verbatim_quote))
                and all(
                    clock_quote_supported_by_source(f.value, f.verbatim_quote, clock_section.text)
                    for clock_section in clock_sections
                )
                for sha in verified
            )
        if not supported:
            purpose = (
                address_purpose_problem(f.value, f.verbatim_quote)
                if f.key is FactKey.MAILING_ADDRESS
                else None
            )
            note = f"quote guard: {purpose}" if purpose else "quote guard: value not in quote"
            guarded.append(_downgrade(f, note))
            continue
        guarded.append(replace(f, source_sha256s=verified))
    if ticket_context is not None:
        guarded.extend(apply_quote_guard(_route_source_findings(sources, ticket_context), sources))
    return tuple(guarded)


def _route_source_findings(sources: Sequence[SourceRecord], ticket: Ticket) -> tuple[Finding, ...]:
    """Retain visible route claims independently of model output.

    Adjacent issuer/type/stage headings and complete supported sentences prove a
    limitation. Other possible claims remain conflicts rather than permission.
    """
    issuer, stage = ticket.jurisdiction or ticket.city_id, ticket.stage or "first-level review"
    result: list[Finding] = []
    for page in sources:
        if (
            page.http_status != 200
            or page.authority_class is AuthorityClass.SECONDARY
            or (page.jurisdiction is not None and page.jurisdiction != issuer)
        ):
            continue
        pieces = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+|;\s*", page.text) if s.strip()]
        for index, sentence in enumerate(pieces):
            if route_local_requirement(sentence) or not possible_route_requirement(sentence):
                continue
            stated_stage = compulsory_route_stage(sentence) or procedure_stage(sentence)
            if stated_stage is not None and not same_stage(stated_stage, stage):
                continue
            heading = pieces[index - 1] if index else ""
            heading_stage = procedure_stage(heading)
            if (
                stated_stage is None
                and heading_stage is not None
                and not same_stage(heading_stage, stage)
            ):
                continue
            quote = heading + "\n" + sentence
            proved = (
                quoted_route_stage(sentence, quote) is not None
                and page.authority_class
                in {
                    AuthorityClass.OFFICIAL_PROCEDURE,
                    AuthorityClass.OFFICIAL_FORM,
                    AuthorityClass.STATUTE,
                    AuthorityClass.REGULATION,
                }
                and re.fullmatch(r"[0-9a-f]{64}", page.sha256) is not None
                and jurisdiction_in_text(issuer, heading)
                and same_stage(procedure_stage(heading) or "", stage)
                and verify_quote(quote, page.text)
            )
            result.append(
                Finding(
                    FactKey.TICKET_SPECIFIC_REQUIREMENTS,
                    [sentence] if proved else None,
                    FindingStatus.CONFIRMED if proved else FindingStatus.CONFLICTED,
                    quote if proved else sentence,
                    (page.sha256,),
                    notes="source-bound compulsory route"
                    if proved
                    else "route condition not established",
                    jurisdiction=issuer,
                    stage=stage,
                )
            )
    return tuple(result)


# --- Value bounds --------------------------------------------------------------------

_ZIP = re.compile(r"\d{5}(?:-\d{4})?")
_STATE = re.compile(r"[A-Z]{2}")


def _is_int(value: JsonValue) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str_list(value: JsonValue) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)


def _address_problem(value: JsonValue) -> str | None:
    if not isinstance(value, dict):
        return "must be an object"
    for field in ("recipient", "line1", "city", "state", "zip_code"):
        item = value.get(field)
        if not isinstance(item, str) or not item.strip():
            return f"{field} must be a non-empty string"
    for field in ("attention", "line2"):
        item = value.get(field)
        if item is not None and not isinstance(item, str):
            return f"{field} must be a string or null"
    state, zip_code = value["state"], value["zip_code"]
    if not isinstance(state, str) or not _STATE.fullmatch(state):
        return "state must be two capital letters"
    if not isinstance(zip_code, str) or not _ZIP.fullmatch(zip_code):
        return "zip_code must be 5 or 9 digits"
    return None


def validate_value(key: FactKey, value: JsonValue) -> str | None:
    """None if `value` has the shape and bounds required for `key`, else the problem."""
    match key:
        case FactKey.MAIL_IN_ACCEPTED:
            return None if isinstance(value, bool) else "must be a boolean"
        case FactKey.GOVERNING_CODE_SECTION:
            ok = isinstance(value, str) and 0 < len(value.strip()) <= 200
            return None if ok else "must be a non-empty string of at most 200 chars"
        case FactKey.DEADLINE_DAYS:
            low, high = DEADLINE_DAYS_BOUNDS
            period = parse_deadline_period(value)
            ok = period is not None and low <= period.days <= high
            return (
                None
                if ok
                else f"must be an integer from {low} to {high}, or an object with days, "
                "unit (CALENDAR or BUSINESS) and time_cutoff"
            )
        case FactKey.DEADLINE_CLOCK_START:
            ok = isinstance(value, str) and value in ClockStart.__members__
            return None if ok else f"must be one of {sorted(ClockStart.__members__)}"
        case FactKey.DEADLINE_RULE:
            ok = isinstance(value, str) and value in DeadlineRule.__members__
            return None if ok else f"must be one of {sorted(DeadlineRule.__members__)}"
        case FactKey.MAILING_ADDRESS:
            return _address_problem(value)
        case key if key in LIST_KEYS:
            return None if _is_str_list(value) else "must be a list of non-empty strings"
        case FactKey.DEADLINE_ABSOLUTE:
            if not isinstance(value, dict) or set(value) != {"applicable", "date"}:
                return "must be an applicability/date object"
            if value["applicable"] is False and value["date"] is None:
                return None
            if value["applicable"] is True and isinstance(value["date"], str):
                try:
                    parsed = date.fromisoformat(value["date"])
                    if parsed.isoformat() == value["date"]:
                        return None
                except ValueError:
                    pass
            return "must contain explicit applicability and a canonical date or null"
        case FactKey.FINE_AMOUNT:
            number = isinstance(value, int | float) and not isinstance(value, bool)
            ok = number and isinstance(value, int | float) and 0 <= value <= FINE_AMOUNT_MAX
            return None if ok else f"must be a number from 0 to {FINE_AMOUNT_MAX}"

    return "unrecognized fact key"


def apply_value_bounds(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    """Force CONFIRMED findings with a malformed or out-of-bounds value to UNKNOWN."""
    bounded: list[Finding] = []
    for f in findings:
        problem = validate_value(f.key, f.value) if f.status is FindingStatus.CONFIRMED else None
        bounded.append(_downgrade(f, f"value bound: {problem}") if problem else f)
    return tuple(bounded)


# --- Consolidation and release -------------------------------------------------------


def _canonical(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _merge_notes(findings: Iterable[Finding]) -> str:
    return "; ".join(_unique(f.notes for f in findings if f.notes))


def _evidence(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    # Rechecks can discover the same requirement repeatedly. Retain each original
    # assessment/quote once rather than multiplying its evidence on every freeze.
    result: list[Finding] = []
    for finding in findings:
        for item in finding.evidence or (finding,):
            if item not in result:
                result.append(item)
    return tuple(result)


def consolidate(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    """One finding per key present, in FactKey order.

    - Any CONFLICTED input, CONFIRMED findings with different values, or a CONFIRMED and a
      NOT_APPLICABLE finding for the same key -> CONFLICTED.
    - Otherwise CONFIRMED wins over UNKNOWN (an unhelpful page does not refute a quote),
      and equal CONFIRMED findings merge their sources.
    - NOT_APPLICABLE next, then UNKNOWN.
    Values are compared exactly (canonical JSON), with no normalisation.
    """
    by_key: dict[FactKey, list[Finding]] = {}
    for f in findings:
        by_key.setdefault(f.key, []).append(f)
    merged: list[Finding] = []
    for key in FactKey:
        group = by_key.get(key)
        if not group:
            continue
        if len(group) == 1 and group[0].status is FindingStatus.CONFLICTED:
            merged.append(group[0])
            continue
        confirmed = [f for f in group if f.status is FindingStatus.CONFIRMED]
        explicit = [f for f in confirmed if f.evidence_basis is None]
        # Procedural silence says nothing about requirements stated in another saved
        # source. Explicit requirements win; opposing explicit claims still conflict.
        if any(isinstance(f.value, list) and f.value for f in explicit):
            confirmed = explicit
        not_applicable = [f for f in group if f.status is FindingStatus.NOT_APPLICABLE]
        conflicted = [f for f in group if f.status is FindingStatus.CONFLICTED]
        values = _unique(_canonical(f.value) for f in confirmed)
        shas = _unique(sha for f in group for sha in f.source_sha256s)
        if conflicted or len(values) > 1 or (confirmed and not_applicable):
            parts = [*values, *(["NOT_APPLICABLE"] if not_applicable else [])]
            described = f"conflicting values: {' vs '.join(parts)}" if parts else ""
            merged.append(
                Finding(
                    key=key,
                    value=None,
                    status=FindingStatus.CONFLICTED,
                    verbatim_quote=group[0].verbatim_quote,
                    source_sha256s=shas,
                    evidence=_evidence(group),
                    notes="; ".join(_unique(n for n in (_merge_notes(group), described) if n)),
                )
            )
        elif confirmed:
            first = confirmed[0]
            merged.append(
                replace(
                    first,
                    source_sha256s=_unique(s for f in confirmed for s in f.source_sha256s),
                    notes=_merge_notes(confirmed),
                    evidence=_evidence(f for f in group if f.status is FindingStatus.CONFIRMED)
                    if len(confirmed) > 1 or len(confirmed) < len(group)
                    else first.evidence,
                )
            )
        elif not_applicable:
            merged.append(replace(not_applicable[0], notes=_merge_notes(not_applicable)))
        else:
            merged.append(
                replace(
                    group[0],
                    value=None,
                    unknown_reason=group[0].unknown_reason or UnknownReason.UNANSWERABLE,
                    notes=_merge_notes(group),
                )
            )
    return tuple(merged)


@dataclass(frozen=True)
class CriticalProblem:
    key: FactKey
    reason: str


def complete_findings(
    findings: Iterable[Finding],
    keys: Iterable[FactKey] = FactKey,
    reason: UnknownReason = UnknownReason.MISSING_ANSWER,
) -> tuple[Finding, ...]:
    """Exactly one explicit answer per requested key, including failed extraction."""
    by_key = {f.key: f for f in consolidate(findings)}
    return tuple(
        by_key.get(key, Finding(key, None, FindingStatus.UNKNOWN, None, (), unknown_reason=reason))
        for key in dict.fromkeys(keys)
    )


@dataclass(frozen=True)
class TierValidation:
    proceed: bool
    reasons: tuple[CriticalProblem, ...]


def validate_tier1(
    findings: Iterable[Finding],
    sources: Sequence[SourceRecord],
    *,
    tiers: EffectiveTiers = DEFAULT_EFFECTIVE_TIERS,
    ticket_context: Ticket | None = None,
    packet_plan: PacketPlan = EMPTY_PACKET_PLAN,
    printed_deadline: date | None = None,
    notice_issue_date: date | None = None,
    vehicle_relationship: str | None = None,
    initial_review_completed: bool | None = None,
) -> TierValidation:
    """The sole Tier 1 validator. It rechecks evidence even for decoded records.

    Printed notice timing is the closed customer-evidence seam. It cannot establish
    counting, waive another rule or make a secondary source official.
    """
    raw = tuple(findings)
    guarded = apply_value_bounds(apply_quote_guard(raw, sources, ticket_context=ticket_context))
    by_key = {f.key: f for f in complete_findings(guarded)}
    original = {f.key: f for f in consolidate(raw)}
    official = {
        s.sha256
        for s in sources
        if s.authority_class is not AuthorityClass.SECONDARY
        and re.fullmatch(r"[0-9a-f]{64}", s.sha256)
        and s.http_status == 200
    }
    problems: list[CriticalProblem] = []
    absolute = by_key[FactKey.DEADLINE_ABSOLUTE]
    absolute_valid = absolute.status is FindingStatus.CONFIRMED and bool(
        official.intersection(absolute.source_sha256s)
    )
    if ticket_context is not None:
        absolute_valid = absolute_valid and finding_in_scope(absolute, ticket_context)
    absolute_applies = (
        absolute_valid
        and isinstance(absolute.value, dict)
        and absolute.value.get("applicable") is True
    )
    for key in FactKey:
        if (
            DEFAULT_TIERS[key] is not FactTier.MUST_HAVE
            and tiers.tiers.get(key) is not FactTier.MUST_HAVE
        ):
            continue
        # Only an evidenced absolute replacement makes the day/trigger questions inapplicable.
        if absolute_applies and key in {FactKey.DEADLINE_DAYS, FactKey.DEADLINE_CLOCK_START}:
            continue
        if type(printed_deadline) is date and key in {
            FactKey.DEADLINE_DAYS,
            FactKey.DEADLINE_CLOCK_START,
        }:
            continue
        f = by_key[key]
        reason: str | None = None
        if key not in original:
            reason = "missing"
        elif f.status is FindingStatus.UNKNOWN:
            bound = (
                validate_value(key, original[key].value)
                if original[key].status is FindingStatus.CONFIRMED
                else None
            )
            reason = (
                "missing"
                if f.unknown_reason is UnknownReason.MISSING_ANSWER
                else f"value {bound}"
                if bound
                else "unknown"
            )
        elif f.status is FindingStatus.CONFLICTED:
            reason = f"conflicted ({f.notes})"
        elif f.status is FindingStatus.NOT_APPLICABLE:
            reason = "NOT_APPLICABLE is not verified evidence"
        elif not official.intersection(f.source_sha256s):
            reason = "only secondary sources"
        elif ticket_context is not None and not finding_in_scope(f, ticket_context):
            reason = "stale jurisdiction or stage"
        elif key is FactKey.MAIL_IN_ACCEPTED and f.value is not True:
            reason = "mail-in contests not accepted"
        elif (
            key is FactKey.DEADLINE_CLOCK_START
            and f.value == "NOTICE_DATE"
            and type(notice_issue_date) is not date
        ):
            reason = "customer notice date missing"
        elif key in LIST_KEYS - {FactKey.CODE_EXCEPTIONS} and isinstance(f.value, list):
            missing = [
                item
                for item in f.value
                if isinstance(item, str)
                and not requirement_fulfilled(
                    key,
                    item,
                    f.verbatim_quote or "",
                    packet_plan,
                    vehicle_relationship=vehicle_relationship,
                    initial_review_completed=initial_review_completed,
                    sources=sources,
                    ticket=ticket_context,
                )
            ]
            if key is FactKey.REQUIRED_DECLARATIONS and len(f.value) > 1:
                reason = "unfulfilled requirements: declaration wording is ambiguous"
            elif missing:
                reason = "unfulfilled requirements: " + ", ".join(missing)
        if reason:
            problems.append(CriticalProblem(key, f"{key}: {reason}"))
    # An unclassified compulsory condition stays a Tier 1 gap until explicitly inventoried
    # in the ticket-specific answer and fulfilled. A confirmed empty list cannot erase it.
    specific = by_key[FactKey.TICKET_SPECIFIC_REQUIREMENTS]
    for condition in tiers.compulsory_gaps:
        if (
            specific.status is not FindingStatus.CONFIRMED
            or not isinstance(specific.value, list)
            or condition.verbatim_quote not in specific.value
            or not requirement_fulfilled(
                FactKey.TICKET_SPECIFIC_REQUIREMENTS,
                condition.verbatim_quote,
                condition.verbatim_quote,
                packet_plan,
                sources=sources,
                ticket=ticket_context,
            )
        ):
            key = FactKey.TICKET_SPECIFIC_REQUIREMENTS
            problems.append(
                CriticalProblem(key, f"{key}: discovered compulsory condition unchecked")
            )
    return TierValidation(not problems, tuple(problems))


def finding_in_scope(finding: Finding, ticket: Ticket) -> bool:
    a = finding.procedure_assessment
    jurisdiction = finding.jurisdiction or (a.jurisdiction if a else None)
    stage = finding.stage or (a.stage if a else None)
    if ticket.jurisdiction is not None and stage is None:
        return (
            finding.key
            in {FactKey.GOVERNING_CODE_SECTION, FactKey.CODE_EXCEPTIONS, FactKey.FINE_AMOUNT}
            and jurisdiction == ticket.jurisdiction
        )
    return (ticket.jurisdiction is None or jurisdiction == ticket.jurisdiction) and same_stage(
        stage or "first-level review", ticket.stage or "first-level review"
    )


def critical_problems(
    findings: Iterable[Finding],
    sources: Sequence[SourceRecord],
    *,
    tiers: EffectiveTiers = DEFAULT_EFFECTIVE_TIERS,
    ticket_context: Ticket | None = None,
    packet_plan: PacketPlan = EMPTY_PACKET_PLAN,
    printed_deadline: date | None = None,
    notice_issue_date: date | None = None,
    vehicle_relationship: str | None = None,
    initial_review_completed: bool | None = None,
) -> tuple[CriticalProblem, ...]:
    return validate_tier1(
        findings,
        sources,
        tiers=tiers,
        ticket_context=ticket_context,
        packet_plan=packet_plan,
        printed_deadline=printed_deadline,
        notice_issue_date=notice_issue_date,
        vehicle_relationship=vehicle_relationship,
        initial_review_completed=initial_review_completed,
    ).reasons


def release_decision(
    findings: Iterable[Finding],
    sources: Sequence[SourceRecord],
    *,
    tiers: EffectiveTiers = DEFAULT_EFFECTIVE_TIERS,
    ticket_context: Ticket | None = None,
    packet_plan: PacketPlan = EMPTY_PACKET_PLAN,
) -> ReleaseDecision:
    """Compatibility wrapper around the single validator."""
    result = validate_tier1(
        findings, sources, tiers=tiers, ticket_context=ticket_context, packet_plan=packet_plan
    )
    return ReleaseDecision(result.proceed, tuple(p.reason for p in result.reasons))
