"""Saved model judgment of procedure completeness, with independent conservative vetoes."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields
from urllib.parse import urlsplit

from parkingbreaker.research.questions import RULE_KEYS, FactKey
from parkingbreaker.research.route_requirements import (
    compulsory_route_stage,
    route_local_requirement,
)
from parkingbreaker.research.sources import AuthorityClass, SourceRecord
from parkingbreaker.research.support import value_supported_by_quote

SILENCE_KEYS = RULE_KEYS | {FactKey.REQUIRED_DECLARATIONS}
MAX_PROCEDURE_CHARS = 30_000
REVIEW_ROUTE_TEXT = (
    r"(?:an? |the |your )?(?:initial review|first-level review|administrative hearing|appeal)"
)


@dataclass(frozen=True)
class ProcedureAssessment:
    complete: bool
    jurisdiction: str
    stage: str
    ticket_type: str
    source_sha256: str
    procedure_text: str
    scope_quote: str
    how_quote: str
    where_quote: str
    when_quote: str
    judgment: str


def normal(text: str) -> str:
    return " ".join(text.split())


def procedure_stage(text: str) -> str | None:
    """Recognise a stage from source text, never from the requested ticket stage."""
    lowered = text.lower()
    if re.search(r"administrative hearing|hearing request|second.level", lowered):
        return "administrative hearing"
    if re.search(r"first.level review|initial review|administrative review", lowered):
        return "first-level review"
    if re.search(r"delinquen", lowered):
        return "delinquency review"
    return None


@dataclass(frozen=True)
class ProcedureSection:
    stage: str | None
    text: str
    issuer_heading: str | None = None


def procedure_sections(text: str) -> tuple[ProcedureSection, ...]:
    """Recognise bounded standalone stage headings; prose never starts a section.

    Saved source text is retained intact. An unrecognised stage-heading shape ends
    the preceding scope without establishing a new stage. With no section boundaries,
    retain the legacy conservative check; never borrow a stage from the ticket.
    """
    heading = re.compile(
        r"(?:(?P<issuer>[\w()&/' ,-]+ )?parking citations? )?"
        r"(?:(?:optional|later) )?"
        r"(?:initial review|first-level review|administrative review|administrative hearing"
        r"|hearing request|second.level review|delinquency review)(?: requests?)?[.:]?",
        re.I,
    )
    sections: list[ProcedureSection] = []
    current: list[str] = []
    stage = None
    issuer_heading = None
    found = False
    for line in text.splitlines(keepends=True):
        candidate = line.strip()
        match = heading.fullmatch(candidate)
        known = match is not None
        possible_heading = (
            len(candidate) <= 240
            and procedure_stage(candidate) is not None
            and not re.search(
                r"[.!?;]|\b(?:you|your|may|must|shall|can|if|when|after|before|within|from"
                r"|is|are|was|were|required|requires|will|would|should)\b",
                candidate.rstrip(".:"),
                re.I,
            )
        )
        if known or possible_heading:
            if current:
                sections.append(ProcedureSection(stage, "".join(current), issuer_heading))
            stage = procedure_stage(line) if known else None
            if match is not None and match["issuer"]:
                issuer_heading = candidate
            elif not known:
                issuer_heading = None
            current = []
            found = True
        current.append(line)
    if not found:
        return (ProcedureSection(procedure_stage(text), text),)
    if current:
        sections.append(ProcedureSection(stage, "".join(current), issuer_heading))
    return tuple(sections)


def quote_procedure_section(text: str, quote: str) -> ProcedureSection | None:
    """One unique source section must contain the whole quote, even at the same stage."""
    q = normal(quote)
    if not q:
        return None
    matches = [section for section in procedure_sections(text) if q in normal(section.text)]
    return matches[0] if len(matches) == 1 else None


def quote_procedure_stage(text: str, quote: str) -> str | None:
    section = quote_procedure_section(text, quote)
    # A deadline may start at a delinquency notice, and a hearing may require a
    # completed initial review. Mentioning a different stage is not a section.
    return section.stage if section is not None else None


def quote_jurisdiction_matches(text: str, quote: str, jurisdiction: str) -> bool:
    """Named source sections cannot borrow issuer identity from elsewhere on a page.

    Generic stage headings inherit the preceding explicit issuer. An ambiguous
    heading ends that context; unsectioned legacy pages keep their existing checks.
    """
    section = quote_procedure_section(text, quote)
    if section is None:
        return False
    if section.issuer_heading is not None:
        return jurisdiction_in_text(jurisdiction, section.issuer_heading)
    return not any(s.issuer_heading is not None for s in procedure_sections(text))


def explicit_form_requirement_stage(quote: str) -> str | None:
    """Only a complete, single-form instruction with an explicit stage qualifier."""
    match = re.fullmatch(
        r"(?:all|every|each) (?:"
        r"(?:parking citation )?(?:appeals?|reviews?|contests?) for (?:an? |the )?"
        r"(?P<post>initial review|first-level review|administrative hearing)"
        r"|(?P<pre>initial reviews?|first-level reviews?|administrative hearings?)"
        r"(?: (?:appeals?|reviews?|contests?))?) "
        r"must include (?:agency )?form [\w-]+[.]?",
        normal(quote),
        re.I,
    )
    return procedure_stage(match["post"] or match["pre"]) if match is not None else None


def same_stage(left: str, right: str) -> bool:
    return (procedure_stage(left) or left) == (procedure_stage(right) or right)


def jurisdiction_in_text(jurisdiction: str, text: str) -> bool:
    locality = jurisdiction.rsplit(":", 1)[-1].removesuffix(".gov")
    locality = re.sub(r"^[a-z]{2}-[a-z]{2}-", "", locality.lower())
    token = re.sub(r"[^a-z0-9]", "", locality)
    return bool(token) and token in re.sub(r"[^a-z0-9]", "", text.lower())


def unread_linked_artifacts(sources: tuple[SourceRecord, ...]) -> tuple[str, ...]:
    """Only allowlisted links require a successful readable saved artifact."""
    fetched = {
        url
        for source in sources
        if source.http_status == 200
        and source.text.strip()
        and re.fullmatch(r"[0-9a-f]{64}", source.sha256)
        and source.authority_class is not AuthorityClass.SECONDARY
        for url in (source.url, source.final_url)
    }
    return tuple(
        dict.fromkeys(
            url
            for source in sources
            if source.authority_class is not AuthorityClass.SECONDARY
            for url in source.linked_urls
            if url not in fetched
        )
    )


def assessment_problem(a: ProcedureAssessment, source: SourceRecord) -> str | None:
    if not a.complete or not a.judgment.strip():
        return "missing complete-procedure judgment"
    if (
        source.authority_class is not AuthorityClass.OFFICIAL_PROCEDURE
        or source.http_status != 200
        or not re.fullmatch(r"[0-9a-f]{64}", source.sha256)
        or a.source_sha256 != source.sha256
        or urlsplit(source.final_url).scheme != "https"
    ):
        return "procedure lacks official authority"
    text = normal(a.procedure_text)
    # Extraction labels are metadata, not an official sentence ending or heading.
    substantive = normal(
        re.sub(
            r"(?im)^\[(?:end )?(?:collapsed content|tab/accordion content)\]\.?[ \t]*$",
            "",
            a.procedure_text,
        )
    )
    # Require the whole saved page, bounded in size. A model cannot trim away
    # a condition, continuation, or stage heading before asserting completeness.
    if (
        not text
        or len(text) > MAX_PROCEDURE_CHARS
        or text != normal(source.text)
        or not substantive
        or substantive[-1] not in ".!?0123456789"
    ):
        return "incomplete or truncated procedure text"
    quotes = (a.scope_quote, a.how_quote, a.where_quote, a.when_quote)
    if any(len(normal(q)) < 12 or normal(q) not in text for q in quotes):
        return "unverified procedure quote"
    lowered = text.lower()
    heading = substantive.lower().split(".", 1)[0]
    if (
        re.search(r"(?:faq|frequently asked|payments?|pay-a|pay/)", source.final_url.lower())
        or re.search(r"frequently asked|^faq\b|^pay(?:ment| your)|^payment", heading)
        or re.search(
            r"\.{3}|…|\[truncated\]|continued on|see (?:the )?(?:next|following) page", lowered
        )
    ):
        return "payment, fragment or truncated page"
    if a.ticket_type != "parking citation" or "parking citation" not in a.scope_quote.lower():
        return "ticket type not established by procedure"
    section = quote_procedure_section(source.text, a.scope_quote)
    if (
        section is None
        or section.stage is None
        or not same_stage(section.stage, a.stage)
        or not same_stage(a.scope_quote, a.stage)
        or any(quote_procedure_section(source.text, q) != section for q in quotes)
    ):
        return "procedure stage not established or disagrees"
    # The jurisdiction token is an identifier; ground its locality in the quoted text.
    if not jurisdiction_in_text(a.jurisdiction, a.scope_quote) or not quote_jurisdiction_matches(
        source.text, a.scope_quote, a.jurisdiction
    ):
        return "jurisdiction not established by procedure"
    if not value_supported_by_quote(FactKey.MAIL_IN_ACCEPTED, True, a.how_quote):
        return "procedure does not explain how to contest by mail"
    if not (
        re.search(r"\b(?:mail|send|review|contest|appeal)\b", a.where_quote, re.I)
        and re.search(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", a.where_quote)
        and not re.search(r"\b(?:payments?|pay|refund|waiver)\b", a.where_quote, re.I)
    ):
        return "procedure does not give a contest destination"
    if not re.search(
        r"\b(?:within .+ days? .*(?:issu\w*|notice|citation date)|by .+\d{4})\b",
        a.when_quote,
        re.I,
    ):
        return "procedure does not state when to contest"
    return None


def is_hearing_deposit_requirement(text: str) -> bool:
    """Recognise only the complete deposit sentence used by payment evidence.

    This is not proof of scope, payment or eligibility. Those gates stay with the
    payment-rule validator; a mixed receipt/check instruction is still a packet item.
    """
    return (
        re.fullmatch(
            rf"(?:a |the )?hearing deposit is required to request {REVIEW_ROUTE_TEXT}",
            normal(text).removesuffix("."),
            re.I,
        )
        is not None
    )


def requirement_sentences(key: FactKey, text: str) -> tuple[str, ...]:
    """Conservative positive-condition detector; never manufacture negative evidence."""
    topics = {
        FactKey.CONTEST_ELIGIBILITY: r"owner|driver|standing|eligible|representative|renter",
        FactKey.MANDATORY_FORMS: r"forms?",
        FactKey.SIGNATURE_REQUIREMENT: r"signature|signed|sign",
        FactKey.PRIOR_STEP_REQUIREMENT: (
            r"(?:first|previously|prior|before).*request|initial review.*completed"
        ),
        FactKey.REQUIRED_DECLARATIONS: r"declaration|sworn statement|affidavit",
        FactKey.TICKET_SPECIFIC_REQUIREMENTS: r".+",
    }
    if key not in topics:
        return ()
    if key is FactKey.REQUIRED_DECLARATIONS:
        direct = re.search(r"^I declare that the foregoing is true and correct[.]?$", text, re.M)
        if direct:
            return (direct.group(),)
    sentences = re.split(r"(?<=[.!?])\s+|\n+|;\s*", text)
    return tuple(
        s.strip()
        for s in sentences
        if not route_local_requirement(s)
        and compulsory_route_stage(s) is None
        # Keep a required declaration's full literal after ':', while an item
        # description cannot borrow compulsion from an unrelated following clause.
        and any(
            re.search(
                r"\b(?:must|required|shall|only|eligible)\b"
                r"|^(?:include|complete|enclose|attach|submit|provide|sign|return)\b",
                clause,
                re.I,
            )
            and re.search(rf"\b(?:{topics[key]})\b", clause, re.I)
            for clause in re.split(r":\s*", s)
        )
        and (
            key is not FactKey.TICKET_SPECIFIC_REQUIREMENTS
            or (
                (
                    procedure_stage(text) is not None
                    or re.search(r"\b(?:contest|appeal|review)\b", s, re.I)
                )
                and not re.search(
                    r"\b(?:deadline|days?|postmark|received|citation number|written explanation|"
                    r"supporting evidence|forms?|signature|signed|sign|owner|initial review)\b",
                    s,
                    re.I,
                )
                and not is_hearing_deposit_requirement(s)
            )
        )
        and (
            key is not FactKey.CONTEST_ELIGIBILITY
            or re.search(r"\b(?:contest|review|appeal|standing|eligible)\b", s, re.I)
        )
        and not re.search(r"^no\b|\bnot (?:required|needed|mandatory)\b|\boptional\b", s, re.I)
    )


def assessment_data(a: ProcedureAssessment) -> dict[str, object]:
    return asdict(a)


def parse_assessment(value: object) -> ProcedureAssessment | None:
    if not isinstance(value, dict) or set(value) != {f.name for f in fields(ProcedureAssessment)}:
        return None
    if type(value["complete"]) is not bool:
        return None
    if any(not isinstance(v, str) or not v.strip() for k, v in value.items() if k != "complete"):
        return None
    return ProcedureAssessment(**value)
