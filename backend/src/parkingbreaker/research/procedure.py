"""Saved model judgment of procedure completeness, with independent conservative vetoes."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields
from functools import lru_cache
from urllib.parse import urlsplit

from parkingbreaker.research.questions import RULE_KEYS, FactKey
from parkingbreaker.research.route_requirements import (
    compulsory_route_stage,
    route_local_requirement,
)
from parkingbreaker.research.sources import AuthorityClass, SourceRecord
from parkingbreaker.research.support import (
    direct_declaration,
    signed_declaration_block,
    value_supported_by_quote,
)

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


_FAQ_QUESTION = re.compile(
    r"(?m)^(?P<title>[^\n?]{1,240}\?(?:[^\n]{0,160})?)\n+(?:\[Tab/accordion content\]\n+)+"
)
_PANEL_END = "[End tab/accordion content]."
_POST_DECISION_BLOCK = re.compile(
    r"(?m)^[ \t]*(?:If you are not satisfied with (?:your|the) (?:appeal|review) "
    r"(?:result|decision)|Once this has been reviewed, and a judge?ment issued)\b"
)
_INITIAL_APPEAL_FORM = re.compile(
    r"Parking (?:Ticket|Citation) Appeal Form\s+Complete the required information below "
    r"and your (?:dispute|appeal) will be forwarded\b[^\n]*(?:\n[^\n]+){0,3}?\bfor review\.",
    re.I,
)


def _faq_stage(title: str, answer: str) -> str | None:
    if "[Tab/accordion content]" in answer or "[Collapsed content]" in answer:
        return None
    first_question = bool(
        re.search(r"\bparking (?:citations?|tickets?)\b", title, re.I)
        and re.search(r"\b(?:dispute|contest|appeal)\b", title, re.I)
        and not re.search(
            r"\b(?:hearing|upheld|denied|delinquen|marked|non-renewal|"
            r"second[- ]level|late|boot|tow|lien)\b",
            title,
            re.I,
        )
    )
    initial_request = re.search(
        r"\b(?:can|may) (?:request (?:a|an initial) review of (?:the|your) (?:citation|ticket)"
        r"|mail (?:your |an? |the )?(?:appeal|contest|dispute)(?: letter)?"
        r"|make a hand ?written appeal)\b",
        answer,
        re.I,
    )
    if (
        first_question
        and initial_request
        and not re.search(
            r"\b(?:administrative|second[- ]level|formal|in[- ]person|court) hearings?\b"
            r"|\bhearing (?:requests?|decisions?|results?)\b"
            r"|\bfollowing an? [^\n.]{0,30}hearing\b"
            r"|\bupheld\b|\bdenied\b|\bdelinquen|\bsecond[- ]level",
            answer,
            re.I,
        )
    ):
        return "first-level review"
    if (
        re.search(r"\b(?:citation|ticket|review) (?:is|was) (?:upheld|denied)\b", title, re.I)
        and re.search(r"\bsecond[- ]level hearing\b", answer, re.I)
        and re.search(r"\bresults? of (?:the|your) (?:initial )?review\b", answer, re.I)
        and not initial_request
    ):
        return "administrative hearing"
    return None


@lru_cache(maxsize=64)
def _faq_sections(text: str) -> tuple[ProcedureSection, ...]:
    """Scope a question to its own answer using the saved HTML extractor boundaries."""
    sections: list[ProcedureSection] = []
    cursor = 0
    for question in _FAQ_QUESTION.finditer(text):
        if question.start() < cursor:
            continue
        if question.start() > cursor:
            sections.append(ProcedureSection(None, text[cursor : question.start()]))
        cursor = question.end()
        end = text.find(_PANEL_END, cursor)
        if end == -1:
            sections.append(ProcedureSection(None, question.group()))
            continue
        answer = text[cursor:end]
        panel_end = end + len(_PANEL_END)
        post_decision = _POST_DECISION_BLOCK.search(answer)
        if post_decision is not None:
            initial_stage = _faq_stage(question["title"], answer[: post_decision.start()])
            if initial_stage == "first-level review":
                split_at = cursor + post_decision.start()
                sections.append(ProcedureSection(initial_stage, text[question.start() : split_at]))
                sections.append(
                    ProcedureSection("administrative hearing", text[split_at:panel_end])
                )
                cursor = panel_end
                continue
        stage = _faq_stage(question["title"], answer)
        cursor = panel_end
        sections.append(ProcedureSection(stage, text[question.start() : cursor]))
    if sections and cursor < len(text):
        sections.append(ProcedureSection(None, text[cursor:]))
    return tuple(sections)


def _form_sections(text: str) -> tuple[ProcedureSection, ...]:
    """Scope a first-stage appeal form while isolating post-decision judicial-review footers."""
    if _INITIAL_APPEAL_FORM.search(text) is None or re.search(
        r"\b(?:administrative|second[- ]level) hearings?\b|\bupheld\b|\bdenied\b", text, re.I
    ):
        return ()
    pages = text.split("\f")
    sections: list[ProcedureSection] = []
    for index, page in enumerate(pages):
        suffix = "\f" if index + 1 < len(pages) else ""
        full_page = page + suffix
        if not full_page:
            continue
        post_decision = _POST_DECISION_BLOCK.search(full_page)
        if post_decision is None:
            sections.append(ProcedureSection("first-level review", full_page))
        else:
            if post_decision.start() > 0:
                sections.append(
                    ProcedureSection("first-level review", full_page[: post_decision.start()])
                )
            sections.append(
                ProcedureSection("administrative hearing", full_page[post_decision.start() :])
            )
    return tuple(sections)


@lru_cache(maxsize=64)
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
    known_found = False
    for line in text.splitlines(keepends=True):
        candidate = line.strip()
        match = heading.fullmatch(candidate)
        known = match is not None
        if known:
            known_found = True
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
    if not known_found and "[Tab/accordion content]" in text and _PANEL_END in text:
        faq = _faq_sections(text)
        if faq:
            return faq
    if not found:
        return (
            _faq_sections(text)
            or _form_sections(text)
            or (ProcedureSection(procedure_stage(text), text),)
        )
    if current:
        sections.append(ProcedureSection(stage, "".join(current), issuer_heading))
    return tuple(sections)


def quote_procedure_sections(text: str, quote: str) -> tuple[ProcedureSection, ...]:
    """Every source section containing the whole quote verbatim."""
    q = normal(quote)
    if not q:
        return ()
    return tuple(section for section in procedure_sections(text) if q in normal(section.text))


def quote_procedure_section(text: str, quote: str) -> ProcedureSection | None:
    """One unique source section must contain the whole quote, even at the same stage."""
    matches = quote_procedure_sections(text, quote)
    return matches[0] if len(matches) == 1 else None


def _attributable_quote_sections(text: str, quote: str) -> tuple[ProcedureSection, ...]:
    matches = quote_procedure_sections(text, quote)
    if len(matches) <= 1:
        return matches

    faq = _faq_sections(text)
    stage = matches[0].stage
    if stage is None or any(section.stage != stage or section not in faq for section in matches):
        return ()
    if len({normal(section.text) for section in matches}) == 1:
        return matches

    # Repeated receipt deadlines can describe one filing route in two FAQ answers.
    # Require both answers to repeat the same first-stage mail instruction.
    if stage != "first-level review" or not value_supported_by_quote(
        FactKey.DEADLINE_RULE, "RECEIPT", quote
    ):
        return ()
    contexts = set()
    literal = normal(quote)
    for section in matches:
        before, _, after = normal(section.text).partition(literal)
        if not before or literal in after:
            return ()
        sentence = re.match(r"\s+([^.!?;]+[.])", after)
        if sentence is None:
            return ()
        mail_instruction = sentence[1].translate(str.maketrans("\u2013\u2014", "--"))
        if not value_supported_by_quote(FactKey.MAIL_IN_ACCEPTED, True, mail_instruction):
            return ()
        contexts.add(mail_instruction.casefold())
    return matches if len(contexts) == 1 else ()


def quote_procedure_stage(text: str, quote: str) -> str | None:
    matches = _attributable_quote_sections(text, quote)
    # A deadline may start at a delinquency notice, and a hearing may require a
    # completed initial review. Mentioning a different stage is not a section.
    return matches[0].stage if matches else None


def quote_jurisdiction_matches(text: str, quote: str, jurisdiction: str) -> bool:
    """Named source sections cannot borrow issuer identity from elsewhere on a page.

    Generic stage headings inherit the preceding explicit issuer. An ambiguous
    heading ends that context; unsectioned legacy pages keep their existing checks.
    """
    matches = _attributable_quote_sections(text, quote)
    if not matches:
        return False
    has_any_issuer = any(s.issuer_heading is not None for s in procedure_sections(text))
    return all(
        jurisdiction_in_text(jurisdiction, section.issuer_heading)
        if section.issuer_heading is not None
        else not has_any_issuer
        for section in matches
    )


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


def assessment_problem(
    a: ProcedureAssessment,
    source: SourceRecord,
    sources: Sequence[SourceRecord] = (),
) -> str | None:
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
    paired_official_form = (
        bool(sources)
        and not unread_linked_artifacts(tuple(sources))
        and bool(_faq_sections(source.text))
        and any(
            other.sha256 != source.sha256
            and (
                other.authority_class is AuthorityClass.OFFICIAL_FORM
                or (
                    other.authority_class is AuthorityClass.OFFICIAL_PROCEDURE
                    and other.content_type == "application/pdf"
                    and _INITIAL_APPEAL_FORM.search(other.text) is not None
                )
            )
            and other.http_status == 200
            and bool(other.text.strip())
            and len(normal(other.text)) <= MAX_PROCEDURE_CHARS
            and re.fullmatch(r"[0-9a-f]{64}", other.sha256) is not None
            and jurisdiction_in_text(a.jurisdiction, other.text)
            for other in sources
        )
    )
    section = quote_procedure_section(source.text, a.scope_quote)
    section_substantive = (
        normal(
            re.sub(
                r"(?im)^\[(?:end )?(?:collapsed content|tab/accordion content)\]\.?[ \t]*$",
                "",
                section.text,
            )
        )
        if section is not None
        else ""
    )
    ending_target = section_substantive if paired_official_form else substantive
    max_chars = 2 * MAX_PROCEDURE_CHARS if paired_official_form else MAX_PROCEDURE_CHARS
    # Require the whole saved page, bounded in size. A model cannot trim away
    # a condition, continuation, or stage heading before asserting completeness.
    if (
        not text
        or len(text) > max_chars
        or text != normal(source.text)
        or not ending_target
        or ending_target[-1] not in ".!?0123456789"
    ):
        return "incomplete or truncated procedure text"
    quotes = (a.scope_quote, a.how_quote, a.where_quote, a.when_quote)
    if any(len(normal(q)) < 12 or normal(q) not in text for q in quotes):
        return "unverified procedure quote"
    lowered = text.lower()
    heading = substantive.lower().split(".", 1)[0]
    url_veto = (
        r"(?:payments?|pay-a|pay/)"
        if paired_official_form
        else r"(?:faq|frequently asked|payments?|pay-a|pay/)"
    )
    heading_veto = (
        r"^pay(?:ment| your)|^payment"
        if paired_official_form
        else r"frequently asked|^faq\b|^pay(?:ment| your)|^payment"
    )
    if (
        re.search(url_veto, source.final_url.lower())
        or re.search(heading_veto, heading)
        or re.search(
            r"\.{3}|…|\[truncated\]|continued on|see (?:the )?(?:next|following) page", lowered
        )
    ):
        return "payment, fragment or truncated page"
    if a.ticket_type != "parking citation" or not re.search(
        r"\bparking (?:citations?|tickets?)\b", a.scope_quote, re.I
    ):
        return "ticket type not established by procedure"
    scope_stage = procedure_stage(a.scope_quote)
    faq_scoped = section is not None and "[Tab/accordion content]" in section.text
    if (
        section is None
        or section.stage is None
        or len(normal(section.text)) > MAX_PROCEDURE_CHARS
        or not same_stage(section.stage, a.stage)
        or (
            not same_stage(a.scope_quote, a.stage)
            if not faq_scoped
            else (scope_stage is not None and not same_stage(scope_stage, a.stage))
        )
        or any(normal(q) not in normal(section.text) for q in quotes)
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
        r"\b(?:within .+ days? .*(?:issu\w*|notice|(?:citation|ticket) date)|by .+\d{4})\b",
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


@lru_cache(maxsize=512)
def requirement_sentences(key: FactKey, text: str) -> tuple[str, ...]:
    """Conservative positive-condition detector; never manufacture negative evidence."""
    topics = {
        FactKey.CONTEST_ELIGIBILITY: r"owner|driver|standing|eligible|representative|renter",
        FactKey.MANDATORY_FORMS: r"(?<!in the )forms?(?! of\b)",
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
        direct = direct_declaration(text)
        if direct:
            return (direct,)
    if key is FactKey.SIGNATURE_REQUIREMENT:
        block = signed_declaration_block(text)
        if block:
            return (block,)
    unwrapped = re.sub(
        r"(\b(?:to|from|for|of|and|or|with|that))[ \t]*\n+[ \t]*(?=[A-Za-z])",
        r"\1 ",
        text,
        flags=re.I,
    )
    sentences = re.split(r"(?<=[.!?])\s+|\n+|;\s*", unwrapped)
    return tuple(
        s.strip()
        for s in sentences
        if not route_local_requirement(s)
        and compulsory_route_stage(s) is None
        and not re.search(r"\bcontinue on back if additional space (?:is )?required\b", s, re.I)
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
