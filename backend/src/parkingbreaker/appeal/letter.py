"""The letter frame around the customer's drafted body. Pure: builds text only.

The frame's shape follows 2.x's appeal_letter_template (reference only, not copied):
sender block, date, agency address, "Re:" line, salutation, body, one closing request
sentence, signature block. Required agency declaration wording stays in the letter flow
and is part of the exact text the customer adopts.

The closing sentence is one explicit input with a default. (2.x chose it with
`a or b if c else d`, which Python reads as `(a or b) if c else d`, so a customer's own
sentence was dropped whenever no profile existed.)

All text must print in the PDF core fonts (Latin-1). Common typographic characters are
replaced with ASCII equivalents before the customer sees the letter, so the text they
adopt is exactly the text that prints; anything else outside Latin-1 is refused.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from parkingbreaker.agencies import MailingAddress, ResolvedAgency
from parkingbreaker.appeal.drafting import NOTICE_SUPPORT, VERBATIM_SUPPORT, Draft
from parkingbreaker.research.fulfilment import required_declarations, required_form_titles
from parkingbreaker.research.run import ResearchResult

DEFAULT_SALUTATION = "To Whom It May Concern:"
# No stock advocacy sentence: the customer's own text is the letter. A caller may still
# supply the customer's own closing sentence explicitly.
DEFAULT_CLOSING: str | None = None
PAGE_BREAK = "\f"


class LetterError(ValueError):
    pass


def unsupported_characters(text: str) -> tuple[str, ...]:
    """Characters the PDF core fonts cannot print, in code-point order."""
    return tuple(
        sorted({c for c in text if ord(c) > 0xFF or (ord(c) < 0x20 and c not in "\n\f\t")})
    )


def is_printable(text: str) -> bool:
    return not unsupported_characters(text)


def printable(text: str) -> str:
    """Validate text for the PDF fonts. Never substitutes: unsupported glyphs are refused.

    Text is returned unchanged so what the customer adopts is what prints. Characters
    outside Latin-1 (such as curly quotes or an em dash) are named in the error instead of
    being silently converted.
    """
    bad = unsupported_characters(text)
    if bad:
        raise LetterError(f"characters the letter cannot print: {[hex(ord(c)) for c in bad]}")
    return text


@dataclass(frozen=True)
class Sender:
    name: str
    address_lines: tuple[str, ...]
    phone: str | None = None
    email: str | None = None


@dataclass(frozen=True)
class LetterInput:
    sender: Sender
    letter_date: date
    agency: ResolvedAgency
    mailing_address: MailingAddress
    citation_number: str
    plate: str
    draft: Draft
    closing_sentence: str | None = DEFAULT_CLOSING
    salutation: str = DEFAULT_SALUTATION
    hardship_affirmed: bool = False  # Legacy answer cannot establish an agency requirement.
    research: ResearchResult | None = None
    # What the envelope holds besides the letter, so the clerk can see nothing is missing.
    encloses_citation: bool = False
    evidence_photos: int = 0
    encloses_citation_back: bool = False


@dataclass(frozen=True)
class Letter:
    text: str  # Exact adopted letter, including any evidenced required wording inline.
    letter_part: str
    declarations_part: str | None


def body_paragraphs(draft: Draft) -> list[str]:
    """Paragraphs in customer order. Customer text is kept exactly; no whitespace collapse.

    Paragraph breaks are layout: each stored paragraph keeps its own characters, and the
    single blank line between paragraphs is supplied here. Sentences carrying support tags
    belong to legacy drafted text and keep their previous whitespace-normalized joining.
    """
    grouped: list[list[str]] = []
    exact: list[bool] = []
    previous: int | None = None
    for sentence in draft.sentences:
        if NOTICE_SUPPORT in sentence.support and not is_printable(sentence.text):
            raise LetterError("confirmed notice quotation cannot be printed verbatim")
        if previous != sentence.paragraph:
            grouped.append([])
            exact.append(True)
        grouped[-1].append(sentence.text)
        exact[-1] = exact[-1] and (
            VERBATIM_SUPPORT in sentence.support or NOTICE_SUPPORT in sentence.support
        )
        previous = sentence.paragraph
    paragraphs: list[str] = []
    for text_parts, verbatim in zip(grouped, exact, strict=True):
        if verbatim:
            paragraphs.append(re.sub(r"\n[ \t]*\n$", "", "".join(text_parts)))
        else:
            paragraphs.append(" ".join(" ".join(part.split()) for part in text_parts))
    return paragraphs


ENCLOSURES_PREFIX = "Enclosures: "


def enclosure_line(
    encloses_citation: bool,
    evidence_photos: int,
    *,
    encloses_citation_back: bool = False,
    required_forms: Sequence[str] = (),
    required_copies: Sequence[str] = (),
) -> str | None:
    """The letter's list of mailed pages; the packet builder checks it against the pages."""
    if encloses_citation_back and not encloses_citation:
        raise LetterError("a citation back requires its front copy")
    citation = "Copy of citation (front and back)" if encloses_citation_back else "Copy of citation"
    items = [citation] if encloses_citation else []
    if evidence_photos:
        items.append(f"{evidence_photos} evidence photo{'s' if evidence_photos != 1 else ''}")
    if any(not is_printable(title) for title in required_forms):
        raise LetterError("required form title cannot be printed verbatim")
    if any(not is_printable(title) for title in required_copies):
        raise LetterError("required copy title cannot be printed verbatim")
    items.extend(required_copies)
    items.extend(required_forms)
    return ENCLOSURES_PREFIX + "; ".join(items) if items else None


def _required(value: str, name: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned:
        raise LetterError(f"{name} is required")
    return cleaned


def _lines(lines: Sequence[str | None]) -> str:
    return "\n".join(" ".join(line.split()) for line in lines if line and line.strip())


def address_block(address: MailingAddress) -> str:
    """The destination block exactly as the letter prints it (one shared renderer)."""
    # Agencies often print the attention line with its own "ATTN:"; never repeat it.
    attention = (
        None
        if not address.attention
        else address.attention
        if re.match(r"\s*(?:attn|attention)\b", address.attention, re.I)
        else f"ATTN: {address.attention}"
    )
    return _lines(
        [
            address.recipient,
            attention,
            address.line1,
            address.line2,
            f"{address.city}, {address.state} {address.zip_code}",
        ]
    )


def redirect_letter_address(text: str, mailed: MailingAddress, corrected: MailingAddress) -> str:
    """The adopted letter re-rendered for a corrected destination.

    render_letter emits sender, date, destination, then the rest, separated by blank
    lines. Inspect only that third section; the customer's prose may quote the same
    address any number of times. Refuse a changed frame rather than searching prose.
    """
    sections = text.split("\n\n", 3)
    old_block, new_block = address_block(mailed), address_block(corrected)
    if len(sections) != 4 or sections[2] != old_block or old_block == new_block:
        raise LetterError("the destination section does not match the mailed destination")
    return "\n\n".join([*sections[:2], new_block, sections[3]])


def render_letter(inp: LetterInput) -> Letter:
    name = _required(inp.sender.name, "sender name")
    if not any(line.strip() for line in inp.sender.address_lines):
        raise LetterError("sender address is required")
    paragraphs = body_paragraphs(inp.draft)
    if not paragraphs:
        raise LetterError("the draft has no sentences")
    sections = [
        _lines([name, *inp.sender.address_lines, inp.sender.phone, inp.sender.email]),
        f"{inp.letter_date.strftime('%B')} {inp.letter_date.day}, {inp.letter_date.year}",
        address_block(inp.mailing_address),
        "Re: Citation "
        f"{_required(inp.citation_number, 'citation number')}, "
        f"Plate {_required(inp.plate, 'plate')}",
        _required(inp.salutation, "salutation"),
        *paragraphs,
    ]
    if inp.closing_sentence is not None:
        sections.append(_required(inp.closing_sentence, "closing sentence"))
    sections.append(f"Sincerely,\n\n\n{name}")
    from parkingbreaker.appeal.required_copies import required_copy_items

    enclosures = enclosure_line(
        inp.encloses_citation,
        inp.evidence_photos,
        encloses_citation_back=inp.encloses_citation_back,
        required_copies=()
        if inp.research is None
        else tuple(
            item.title
            for item in required_copy_items(
                inp.research.findings, inp.research.sources, inp.research.ticket
            )
        ),
        required_forms=()
        if inp.research is None
        else required_form_titles(inp.research.findings, inp.research.sources, inp.research.ticket),
    )
    if enclosures:
        sections.append(enclosures)
    letter_part = printable("\n\n".join(sections))
    declarations = None
    if inp.research is not None:
        try:
            wording = required_declarations(
                inp.research.findings, inp.research.sources, inp.research.ticket
            )
        except ValueError as exc:
            raise LetterError(str(exc)) from exc
        if any(not is_printable(text) for text in wording):
            raise LetterError("agency declaration cannot be printed verbatim")
        if wording:
            declarations = "\n\n".join(wording)
            declarations += "\nDate: " + inp.letter_date.isoformat()
            # Required wording stays in the letter flow, never an extra exhibit page.
            before, closing = letter_part.rsplit("Sincerely,", 1)
            letter_part = before + declarations + "\n\nSincerely," + closing
    return Letter(text=letter_part, letter_part=letter_part, declarations_part=declarations)
