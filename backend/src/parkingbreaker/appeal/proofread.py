"""Pass 3: deterministic clerical fidelity checks, with no LLM or I/O.

Resolved tags alone never establish support. Service-created substantive wording must
copy complete original customer note units, in order, without losing distinct facts.
Only identical repetition may be deduplicated. Narrow clerical allowances cover exact
selected reference data, ticket values and a neutral opted-in notice quotation; none
can establish new conduct or compliance by the customer.

Dates, identifiers, numbers and names are also checked against resolved support.
Service-created advice and outcome promises are rejected. Verbatim customer statements
and exact selected data are retained without judging their vocabulary or reason.
The bounded retry and customer-edit policy lives in appeal.correction.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from parkingbreaker.appeal.drafting import (
    FACT_FIELDS,
    NOTICE_PREFIX,
    NOTICE_SUPPORT,
    Draft,
    DraftSentence,
    FactTag,
    NoteSpan,
    ReferenceTag,
    TicketFacts,
    fact_text,
    note_segments,
    notice_sentence,
    parse_tag,
)
from parkingbreaker.appeal.references import Reference

# Service-created advice/promise phrases. Literal customer statements and exact
# selected data are exempt; the tool never judges the customer's own words.
BANNED_PHRASES: tuple[str, ...] = (
    "due process",
    "void",
    "unconstitutional",
    "legally bound",
    "legal advice",
    "legal opinion",
    "legal strategy",
    "guarantee",
    "guaranteed",
    "guarantees",
    "will win",
    "you will win",
    "sure to win",
    "certain to win",
    "will be dismissed",
    "will be overturned",
    "win your case",
    "beat your ticket",
    "beat the ticket",
    "robot lawyer",
    "ai lawyer",
    "attorney",
    "attorneys",
    "lawyer",
    "counsel",
    "we recommend",
    "we advise",
    "we suggest",
    "you should argue",
    "you should claim",
    "your best argument",
    "loophole",
)


@dataclass(frozen=True)
class Problem:
    sentence: int | None  # 0-based index, None for draft-level problems
    kind: str
    detail: str

    def __str__(self) -> str:
        where = "draft" if self.sentence is None else f"sentence {self.sentence + 1}"
        return f"{where}: {self.kind}: {self.detail}"


# --- Token patterns ------------------------------------------------------------------

_MONTHS = {
    name: i
    for i, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ORD = r"(?:st|nd|rd|th)?"
_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b"),
    re.compile(r"\b(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2}|\d{4}))?\b"),
    re.compile(
        rf"\b(?P<mon>{_MONTH_RE})\.?\s+(?P<d>\d{{1,2}}){_ORD}(?:,?\s+(?P<y>\d{{4}}))?\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?P<d>\d{{1,2}}){_ORD}\s+(?:of\s+)?(?P<mon>{_MONTH_RE})\.?(?:,?\s+(?P<y>\d{{4}}))?\b",
        re.IGNORECASE,
    ),
)
# Most specific first: "CVC \u00a7 22500" must be read as one citation, not as a bare section sign.
_CODE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:CVC|VC|TC)\b\.?\s*\u00a7*\s*(?P<n>\d[\w.\-()]*)?"),
    re.compile(r"\bSec(?:tion)?s?\b\.?\s*(?P<n>\d[\w.\-()]*)", re.IGNORECASE),
    re.compile(
        r"\b(?:[A-Z][a-z]+\s+)+Code\b(?:\s*(?:\u00a7+|Sec(?:tion)?\.?)?\s*(?P<n>\d[\w.\-()]*))?"
    ),
    re.compile(r"\u00a7+\s*(?P<n>\d[\w.\-()]*)?"),
)
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:[.:]\d+)*(?=(?:st|nd|rd|th|am|pm)?\b)")
_ALNUM = re.compile(r"\b(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9][A-Za-z0-9-]{3,}\b")
_ORDINAL_OR_TIME = re.compile(r"\d+(?:st|nd|rd|th|am|pm)", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z][A-Za-z'\u2019-]*")
_I_FORMS = frozenset({"I", "I'm", "I've", "I'd", "I'll", "I\u2019m", "I\u2019ve", "I\u2019d"})
_MONTH_NAMES = frozenset(_MONTHS)


def _to_date(match: re.Match[str]) -> tuple[int | None, int, int] | None:
    groups = match.groupdict()
    month = _MONTHS[groups["mon"].lower()] if groups.get("mon") else int(groups["m"])
    day = int(groups["d"])
    year_text = groups.get("y")
    year = None
    if year_text:
        year = int(year_text) + (2000 if len(year_text) == 2 else 0)
    try:
        date(year or 2000, month, day)
    except ValueError:
        return None
    return (year, month, day)


def find_dates(text: str) -> tuple[list[tuple[int | None, int, int]], str]:
    """Dates found in `text`, and the text with them blanked out."""
    found: list[tuple[int | None, int, int]] = []
    for pattern in _DATE_PATTERNS:

        def blank(match: re.Match[str]) -> str:
            parsed = _to_date(match)
            if parsed is None:
                return match.group()
            found.append(parsed)
            return " " * len(match.group())

        text = pattern.sub(blank, text)
    return found, text


def find_code_citations(text: str) -> tuple[list[tuple[str, str | None]], str]:
    """(citation text, its section number or None) pairs, and the text with them blanked."""
    found: list[tuple[str, str | None]] = []
    for pattern in _CODE_PATTERNS:

        def blank(match: re.Match[str]) -> str:
            number = match.groupdict().get("n")
            found.append((match.group().strip(), number.rstrip(".") if number else None))
            return " " * len(match.group())

        text = pattern.sub(blank, text)
    return found, text


def find_numbers(text: str) -> list[str]:
    return [m.group().replace(",", "").rstrip(".:") for m in _NUMBER.finditer(text)]


def find_identifiers(text: str) -> list[str]:
    """Plate- or citation-like tokens: letters and digits mixed, at least 4 characters."""
    return [m.group() for m in _ALNUM.finditer(text) if not _ORDINAL_OR_TIME.fullmatch(m.group())]


def _blank_identifier(match: re.Match[str]) -> str:
    """Blank an identifier so its digits are not re-checked as numbers; keep ordinals and
    times ("1st", "15am") so their digits are checked as numbers."""
    token = match.group()
    return token if _ORDINAL_OR_TIME.fullmatch(token) else " " * len(token)


def find_names(text: str) -> list[str]:
    """Capitalised words after the first word, and all-caps words anywhere."""
    names: list[str] = []
    for i, match in enumerate(_WORD.finditer(text)):
        word = match.group().removesuffix("'s").removesuffix("\u2019s").strip("'-\u2019")
        if not word or word in _I_FORMS or word.lower() in _MONTH_NAMES:
            continue
        acronym = len(word) >= 2 and word.isupper()
        if acronym or (i > 0 and word[0].isupper()):
            names.append(word)
    return names


def _squash(token: str) -> str:
    return re.sub(r"[\s-]", "", token).lower()


def _date_supported(
    d: tuple[int | None, int, int], support: Iterable[tuple[int | None, int, int]]
) -> bool:
    year, month, day = d
    return any(
        (month, day) == (m, dd) and (year is None or y is None or year == y) for y, m, dd in support
    )


# --- Proofreading --------------------------------------------------------------------


def _fidelity_text(text: str) -> str:
    """Only whitespace, capitalization and a terminal full stop are formatting."""
    return " ".join(text.split()).casefold().rstrip(".")


def missing_customer_statements(text: str, notes: str) -> tuple[int, ...]:
    """Every distinct customer sentence must survive; exact repetition may be removed."""
    rendered = {_fidelity_text(statement) for _, _, statement in note_segments(text)}
    seen: set[str] = set()
    missing: list[int] = []
    for index, (_, _, statement) in enumerate(note_segments(notes), start=1):
        key = _fidelity_text(statement)
        if key and key not in seen and key not in rendered:
            missing.append(index)
        seen.add(key)
    return tuple(missing)


def _faithful_customer_text(sentence: DraftSentence, notes: str) -> bool:
    ranges: list[tuple[int, int]] = []
    units = [
        (start, end - len(text) + len(text.rstrip())) for start, end, text in note_segments(notes)
    ]
    starts = {start for start, _ in units}
    ends = {end for _, end in units}
    for tag in sentence.support:
        parsed = parse_tag(tag)
        if isinstance(parsed, NoteSpan) and 0 <= parsed.start < parsed.end <= len(notes):
            raw = notes[parsed.start : parsed.end]
            start = parsed.start + len(raw) - len(raw.lstrip())
            end = parsed.end - len(raw) + len(raw.rstrip())
            if start not in starts or end not in ends:
                return False
            if ranges and start < ranges[-1][1]:
                return False
            ranges.append((start, end))
    expected = " ".join(notes[start:end] for start, end in ranges)
    return bool(ranges) and _fidelity_text(sentence.text) == _fidelity_text(expected)


def _clerical_text(
    sentence: DraftSentence, selected: Sequence[Reference], facts: TicketFacts
) -> bool:
    if sentence.support == (NOTICE_SUPPORT,) and sentence.text == notice_sentence(facts):
        return True
    text = _fidelity_text(sentence.text)
    if any(
        reference.id in sentence.support
        and text
        in {
            _fidelity_text(reference.text),
            _fidelity_text(reference.quote),
            _fidelity_text(f"{reference.text} | {reference.quote}"),
        }
        for reference in selected
    ):
        return True
    if any(
        f"fact:{name}" in sentence.support and text == _fidelity_text(fact_text(facts, name))
        for name in FACT_FIELDS
    ):
        return True
    return set(sentence.support) == {"fact:citation_number", "fact:plate"} and text == (
        _fidelity_text(f"I contest citation {facts.citation_number} for plate {facts.plate}.")
    )


def _resolve(
    tags: Sequence[str],
    notes: str,
    selected: dict[str, Reference],
    facts: TicketFacts,
) -> tuple[list[str], list[str]]:
    """(support texts, unresolved tag messages)."""
    texts: list[str] = []
    bad: list[str] = []
    for tag in tags:
        if tag == NOTICE_SUPPORT:
            expected = notice_sentence(facts)
            if expected is None:
                bad.append("notice sentence requires confirmed text and opt-in")
            else:
                texts.append(expected)
            continue
        parsed = parse_tag(tag)
        match parsed:
            case NoteSpan(start, end):
                if 0 <= start < end <= len(notes):
                    texts.append(notes[start:end])
                else:
                    bad.append(f"note span {start}-{end} is outside the notes")
            case ReferenceTag(ref_id):
                ref = selected.get(ref_id)
                if ref is None:
                    bad.append(f"{ref_id!r} is not a selected reference")
                else:
                    texts.append(f"{ref.text}\n{ref.quote}")
            case FactTag(field):
                if field in FACT_FIELDS:
                    texts.append(fact_text(facts, field))
                else:
                    bad.append(f"unknown ticket fact {field!r}")
    return texts, bad


def _code_matches(citation: str, number: str | None, refs: Iterable[Reference]) -> bool:
    for ref in refs:
        haystack = f"{ref.text}\n{ref.quote}"
        if number is not None:
            if re.search(rf"(?<![\w.]){re.escape(number)}(?![\w])", haystack):
                return True
        elif citation.lower() in haystack.lower():
            return True
    return False


def proofread(
    draft: Draft,
    notes: str,
    selected: Sequence[Reference],
    facts: TicketFacts,
    *,
    require_all_notes: bool = True,
) -> tuple[Problem, ...]:
    problems: list[Problem] = []
    if not draft.sentences:
        return (Problem(None, "empty", "the draft has no sentences"),)
    by_id = {r.id: r for r in selected}
    note_order: list[int] = []
    banned = [
        (phrase, re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE))
        for phrase in BANNED_PHRASES
    ]
    for i, sentence in enumerate(draft.sentences):
        text = sentence.text
        faithful = _faithful_customer_text(sentence, notes)
        clerical = _clerical_text(sentence, selected, facts)
        if faithful:
            note_order.extend(
                tag.start
                for value in sentence.support
                if isinstance(tag := parse_tag(value), NoteSpan)
            )

        def report(kind: str, detail: str, i: int = i) -> None:
            problems.append(Problem(i, kind, detail))

        if (NOTICE_SUPPORT in sentence.support or NOTICE_PREFIX.casefold() in text.casefold()) and (
            text != notice_sentence(facts)
            or sentence.support != (NOTICE_SUPPORT,)
            or sum(NOTICE_SUPPORT in s.support for s in draft.sentences) != 1
        ):
            report("notice text", "notice sentence must be the single exact opt-in quotation")
        if not text.strip():
            report("empty", "sentence has no text")
            continue
        for phrase, pattern in banned:
            if not faithful and not clerical and pattern.search(text):
                report("banned phrase", repr(phrase))
        if not sentence.support:
            report("unsupported", "sentence has no support tags")
            continue
        support_texts, bad_tags = _resolve(sentence.support, notes, by_id, facts)
        for message in bad_tags:
            report("bad tag", message)
        support = "\n".join(support_texts)

        citations, rest = find_code_citations(text)
        for citation, number in citations:
            if not faithful and not clerical and not _code_matches(citation, number, selected):
                report("code not selected", repr(citation))

        dates, rest = find_dates(rest)
        support_dates, _ = find_dates(support)
        for d in dates:
            if not _date_supported(d, support_dates):
                y, m, dd = d
                report("unsupported date", f"{y or '????'}-{m:02d}-{dd:02d}")

        support_numbers = set(find_numbers(support))
        support_squashed = _squash(support)
        for token in find_identifiers(rest):
            if _squash(token) not in support_squashed:
                report("unsupported identifier", repr(token))
        rest_no_ids = _ALNUM.sub(_blank_identifier, rest)
        for number in find_numbers(rest_no_ids):
            if number not in support_numbers:
                report("unsupported number", repr(number))

        support_words = {w.lower() for w in _WORD.findall(support)}
        support_words |= {w.lower().removesuffix("'s") for w in _WORD.findall(support)}
        for name in find_names(rest_no_ids):
            if name.lower() not in support_words:
                report("unsupported name", repr(name))
        if not any(problem.sentence == i for problem in problems) and not faithful and not clerical:
            report("unsupported assertion", "use the customer's complete original statement")
    if note_order != sorted(note_order):
        problems.append(
            Problem(None, "customer order changed", "preserve customer statement order")
        )
    if require_all_notes:
        missing = missing_customer_statements("\n".join(s.text for s in draft.sentences), notes)
        if missing:
            problems.append(
                Problem(None, "omitted customer text", f"customer statements missing: {missing}")
            )
    return tuple(problems)
