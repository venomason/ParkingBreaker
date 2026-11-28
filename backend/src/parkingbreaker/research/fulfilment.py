"""Requirement fulfilment from packet bytes and closed relationship/process answers.

Unsupported proof types stay unfulfilled. A noun in a letter, or a caller's list of
fulfilled items, cannot stand in for an enclosed completed form or a signature.
"""

from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Sequence
from enum import StrEnum
from typing import TYPE_CHECKING

from fpdf import FPDF
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf._page import PageObject
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from parkingbreaker.mail_pages import prints_as_letter
from parkingbreaker.research.letter_identifiers import (
    IdentifierKind,
    classify_identifier_item,
    plan_identifier_fulfilled,
)
from parkingbreaker.research.questions import FactKey, PacketPlan, Ticket
from parkingbreaker.research.route_requirements import compulsory_route_stage, quoted_route_stage
from parkingbreaker.research.sources import AuthorityClass, SourceRecord
from parkingbreaker.research.support import direct_declaration, signed_declaration_block

if TYPE_CHECKING:
    from parkingbreaker.research.findings import Finding


# Closed mappings: an unfamiliar non-narrative field is an S3 gap, never guessed.
FORM_FIELDS = {
    "citation number": "citation_number",
    "citationno": "citation_number",
    "parking ticket number": "citation_number",
    "plate": "plate",
    "license plate": "plate",
    "license plate number": "plate",
    "name": "customer_name",
    "phone": "customer_phone",
    "email": "customer_email",
    "address": "customer_address",
    "issue date": "issue_date",
    "citation issue date": "issue_date",
    "violation code": "violation_code",
}
NARRATIVE_FIELDS = frozenset({"explanation", "reason for contest"})
CITATION_PAGE_TITLE = "Copy of citation"
CITATION_BACK_PAGE_TITLE = "Copy of citation (back)"
EVIDENCE_PAGE_TITLE = "Evidence photo"
UPLOADED_LABEL = "Uploaded by customer"


class SignatureRequirement(StrEnum):
    REQUIRED = "required"
    NOT_REQUIRED = "not required"
    UNKNOWN = "unknown"


def customer_evidence_photo_page(page: PageObject) -> bool:
    lines = (page.extract_text() or "").splitlines()
    return (
        len(lines) >= 2
        and lines[0].startswith(EVIDENCE_PAGE_TITLE + " ")
        and lines[1] == UPLOADED_LABEL
        and bool(page.images)
    )


def customer_packet_image_page(page: PageObject) -> bool:
    lines = (page.extract_text() or "").splitlines()
    return (
        len(lines) >= 2
        and (
            lines[0] in {CITATION_PAGE_TITLE, CITATION_BACK_PAGE_TITLE}
            or lines[0].startswith(EVIDENCE_PAGE_TITLE + " ")
        )
        and lines[1] == UPLOADED_LABEL
        and bool(page.images)
    )


def verified_findings(
    findings: Sequence[Finding], sources: Sequence[SourceRecord], ticket: Ticket
) -> tuple[Finding, ...]:
    """Reapply the existing guards before using saved findings in a packet."""
    from parkingbreaker.research.findings import (
        FindingStatus,
        apply_quote_guard,
        apply_value_bounds,
        consolidate,
        finding_in_scope,
    )

    official = {
        s.sha256
        for s in sources
        if s.authority_class is not AuthorityClass.SECONDARY
        and s.http_status == 200
        and re.fullmatch(r"[0-9a-f]{64}", s.sha256)
    }
    return tuple(
        f
        for f in consolidate(apply_value_bounds(apply_quote_guard(findings, sources)))
        if f.status is FindingStatus.CONFIRMED
        and official.intersection(f.source_sha256s)
        and finding_in_scope(f, ticket)
    )


def signature_requirement(
    findings: Sequence[Finding], sources: Sequence[SourceRecord], ticket: Ticket
) -> SignatureRequirement:
    guarded = verified_findings(findings, sources, ticket)
    for f in guarded:
        if (
            f.key
            in {
                FactKey.SIGNATURE_REQUIREMENT,
                FactKey.REQUIRED_DECLARATIONS,
                FactKey.MANDATORY_FORMS,
                FactKey.REQUIRED_ITEMS,
            }
            and isinstance(f.value, list)
            and f.value
            and (
                f.key is FactKey.SIGNATURE_REQUIREMENT
                or any(
                    isinstance(v, str)
                    and re.search(
                        r"\b(?:signed|sign|signature)\b", v + " " + (f.verbatim_quote or ""), re.I
                    )
                    for v in f.value
                )
            )
        ):
            return SignatureRequirement.REQUIRED
    if any(f.key is FactKey.SIGNATURE_REQUIREMENT and f.value == [] for f in guarded):
        return SignatureRequirement.NOT_REQUIRED
    return SignatureRequirement.UNKNOWN


def declaration_wording(quote: str) -> str | None:
    """Only quoted agency wording following an explicit declaration requirement."""
    # A form may print the customer's declaration directly, without quotation marks.
    direct = direct_declaration(quote)
    if direct:
        return direct
    if not re.search(r"\b(?:must|required|shall)\b", quote, re.I) or not re.search(
        r"\b(?:declaration|statement)\b", quote, re.I
    ):
        return None
    quoted = re.findall(r'["“](.+?)["”]', quote, re.S)
    return quoted[0] if len(quoted) == 1 else None


def required_declarations(
    findings: Sequence[Finding], sources: Sequence[SourceRecord], ticket: Ticket
) -> tuple[str, ...]:
    result: list[str] = []
    for f in verified_findings(findings, sources, ticket):
        if f.key is FactKey.REQUIRED_DECLARATIONS and f.value:
            wording = declaration_wording(f.verbatim_quote or "")
            if wording is None or not isinstance(f.value, list) or len(f.value) != 1:
                raise ValueError("required declaration wording is not established")
            result.append(wording)
    return tuple(dict.fromkeys(result))


def required_vehicle_identifiers(
    findings: Sequence[Finding], sources: Sequence[SourceRecord], ticket: Ticket
) -> frozenset[IdentifierKind]:
    """Written identifiers required by guarded, applicable agency instructions."""
    required: set[IdentifierKind] = set()
    for finding in verified_findings(findings, sources, ticket):
        if finding.key not in {
            FactKey.REQUIRED_ITEMS,
            FactKey.TICKET_SPECIFIC_REQUIREMENTS,
        } or not isinstance(finding.value, list):
            continue
        quote = finding.verbatim_quote or ""
        for item in finding.value:
            if not isinstance(item, str) or not requirement_interpretable(finding.key, item, quote):
                continue
            kind = _identifier_requirement_kind(finding.key, item, quote)
            if kind is IdentifierKind.PLATE_AND_STATE:
                required.update((IdentifierKind.PLATE_NUMBER, IdentifierKind.PLATE_STATE))
            elif kind is not None:
                required.add(kind)
    return frozenset(required)


def _widgets(page: PageObject) -> list[DictionaryObject]:
    return [
        obj
        for ref in page.get("/Annots", [])
        if isinstance(obj := ref.get_object(), DictionaryObject)
        and obj.get("/Subtype") == "/Widget"
    ]


def required_form_items(
    findings: Sequence[Finding], sources: Sequence[SourceRecord], ticket: Ticket
) -> tuple[str, ...]:
    """One verified requirement per form identity, shared by letter and packet."""
    items: dict[str, str] = {}
    for finding in verified_findings(findings, sources, ticket):
        if finding.key not in {FactKey.MANDATORY_FORMS, FactKey.REQUIRED_ITEMS} or not isinstance(
            finding.value, list
        ):
            continue
        for item in finding.value:
            if isinstance(item, str) and "form" in item.casefold():
                items.setdefault(" ".join(_document_name(item).split()).casefold(), item)
    return tuple(items.values())


def required_form_titles(
    findings: Sequence[Finding], sources: Sequence[SourceRecord], ticket: Ticket
) -> tuple[str, ...]:
    return tuple(_document_name(item) for item in required_form_items(findings, sources, ticket))


def _printable_citation_reference(number: str | None) -> str | None:
    """The exact printable reference the letter renderer prints, or None if unusable."""
    if number is None:
        return None
    normalized = " ".join(number.split())
    try:
        normalized.encode("latin-1")
    except UnicodeEncodeError:
        return None
    if not normalized or not normalized.isprintable():
        return None
    return f"Citation {normalized}"


def _expected_citation_reference(plan: PacketPlan) -> str | None:
    """The exact reference for the plan's authoritative citation number, or None.

    Only a customer-field inventory carrying exactly one `citation_number` binds an
    expected identifier. A plan without that authoritative value stays unresolved: the
    frozen packet's own body lines prove nothing, because an adopted letter can contain
    any customer-written line that merely resembles a citation reference.
    """
    fields = dict(plan.customer_fields)
    if len(fields) != len(plan.customer_fields) or "citation_number" not in fields:
        return None
    return _printable_citation_reference(fields["citation_number"])


def _citation_reference_present(text: str, reference: str) -> bool:
    """Match the whole printed reference, rejecting longer or differently coded values."""
    body = r"\s+".join(re.escape(part) for part in reference.split())
    return re.search(rf"(?<![\w/]){body}(?![\w/-])", text) is not None


def _known_form_values(plan: PacketPlan, ticket: Ticket) -> dict[str, str] | None:
    known = dict(plan.customer_fields)
    if len(known) != len(plan.customer_fields):
        return None
    known["issue_date"] = ticket.issue_date.isoformat()
    known["violation_code"] = ticket.violation_code
    return known


def _form_appearance_fits(widget: DictionaryObject, reader: PdfReader) -> bool:
    """Check the generated standard-font ink against its actual clipping rectangle."""
    appearances = widget["/AP"]
    if not isinstance(appearances, DictionaryObject):
        return False
    appearance = appearances["/N"]
    stream = ContentStream(appearance, reader)
    clip: tuple[float, float, float, float] | None = None
    rectangle: tuple[float, float, float, float] | None = None
    x = y = size = 0.0
    metrics = FPDF(unit="pt")
    for args, operator in stream.operations:
        if operator == b"re":
            rectangle = (float(args[0]), float(args[1]), float(args[2]), float(args[3]))
        elif operator in {b"W", b"W*"}:
            clip = rectangle
        elif operator == b"Tf":
            size = float(args[1])
            if str(args[0]) != "/PBForm" or size < 8:
                return False
            metrics.set_font("Helvetica", size=size)
        elif operator == b"Td":
            x += float(args[0])
            y += float(args[1])
        elif operator == b"Tj":
            if clip is None or size == 0:
                return False
            left, bottom, width, height = clip
            text = str(args[0])
            advance = metrics.get_string_width(text)
            # Conservative Helvetica glyph bounds, in points. Extractable text alone
            # does not prove that the PDF's clipping region will actually print it.
            if (
                x < left - 0.5
                or x + advance > left + width + 0.5
                or y - 0.225 * size < bottom - 0.5
                or y + 0.931 * size > bottom + height + 0.5
            ):
                return False
    return clip is not None and size >= 8


def _wrapped_form_value(value: str, width: float) -> str | None:
    """Insert cosmetic line breaks without dropping words or shrinking the type."""
    metrics = FPDF(unit="pt")
    metrics.set_font("Helvetica", size=10)
    lines: list[str] = []
    for paragraph in value.splitlines():
        current = ""
        for word in paragraph.split(" "):
            if metrics.get_string_width(word) > width:
                return None
            candidate = current + " " + word if current else word
            if metrics.get_string_width(candidate) > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return "\n".join(lines)


def complete_form(
    template: bytes,
    item: str,
    plan: PacketPlan,
    sources: Sequence[SourceRecord],
    ticket: Ticket,
) -> bytes | None:
    """Populate a saved official text form only from exact known customer inputs."""
    from dataclasses import replace

    known = _known_form_values(plan, ticket)
    if known is None or template not in plan.form_templates:
        return None
    try:
        original = PdfReader(io.BytesIO(template))
        if original.is_encrypted or len(original.pages) != 1:
            return None
        root = original.trailer["/Root"]
        if not isinstance(root, DictionaryObject):
            return None
        acroform = root.get("/AcroForm")
        if acroform is None or not isinstance(acroform.get_object(), DictionaryObject):
            return None
        acroform = acroform.get_object()
        assert isinstance(acroform, DictionaryObject)
        if (
            any(key in root for key in ("/AA", "/Names"))
            or ("/OpenAction" in root and not isinstance(root["/OpenAction"], ArrayObject))
            or any(key in acroform for key in ("/XFA", "/AA"))
        ):
            return None
        page = original.pages[0]
        if not prints_as_letter(page):
            return None  # Lob mails only US Letter pages; such a form can never be sent
        if "/AA" in page or any(
            ref.get_object().get("/Subtype") != "/Widget" for ref in page.get("/Annots", [])
        ):
            return None
        fields = _widgets(page)
        root_fields = acroform.get("/Fields")
        if (
            not isinstance(root_fields, ArrayObject)
            or len(root_fields) != len(fields)
            or any(ref.get_object() not in fields for ref in root_fields)
        ):
            return None
        values: dict[str, str] = {}
        for widget in fields:
            name = str(widget.get("/T", ""))
            label = name.casefold()
            value = known.get(
                "customer_statement" if label in NARRATIVE_FIELDS else FORM_FIELDS.get(label, "")
            )
            if (
                widget.get("/FT") != "/Tx"
                or value is None
                or not value.strip()
                or name in values
                or any(key in widget for key in ("/A", "/AA", "/Parent"))
                or int(widget.get("/Ff", 0)) & (1 | 8192 | 16777216 | 33554432)
                or int(widget.get("/F", 0)) & (1 | 2 | 32)
                or ("/MaxLen" in widget and len(value) > int(str(widget["/MaxLen"])))
            ):
                return None
            value.encode("latin-1")
            values[name] = value
        if not values:
            return None
        writer = PdfWriter(clone_from=io.BytesIO(template))
        form = writer._root_object["/AcroForm"].get_object()
        assert isinstance(form, DictionaryObject)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
        resources = form.setdefault(NameObject("/DR"), DictionaryObject()).get_object()
        assert isinstance(resources, DictionaryObject)
        fonts = resources.setdefault(NameObject("/Font"), DictionaryObject()).get_object()
        assert isinstance(fonts, DictionaryObject)
        fonts[NameObject("/PBForm")] = writer._add_object(font)
        printable_values: dict[str, tuple[str, str, float]] = {}
        for widget in _widgets(writer.pages[0]):
            widget[NameObject("/DA")] = TextStringObject("/PBForm 0 Tf 0 g")
            rect = widget["/Rect"]
            if not isinstance(rect, ArrayObject) or len(rect) != 4:
                return None
            value = _wrapped_form_value(
                values[str(widget["/T"])], float(rect[2]) - float(rect[0]) - 8
            )
            if value is None:
                return None
            printable_values[str(widget["/T"])] = (value, "/PBForm", 10)
            # Only appearance layout uses multiline. Restore the source's field flags
            # and the exact original value after rendering; no form fact is rewritten.
            widget[NameObject("/Ff")] = NumberObject(int(widget.get("/Ff", 0)) | 4096)
        # Flatten the generated appearance into the printable page, retaining widgets
        # so the existing approval gate can verify their exact values and layout.
        writer.update_page_form_field_values(
            writer.pages[0], printable_values, auto_regenerate=False, flatten=True
        )
        for original_widget, widget in zip(fields, _widgets(writer.pages[0]), strict=True):
            widget[NameObject("/Ff")] = NumberObject(int(original_widget.get("/Ff", 0)))
            widget[NameObject("/V")] = TextStringObject(values[str(widget["/T"])])
        output = io.BytesIO()
        writer.write(output)
        data = output.getvalue()
        completed = PdfReader(io.BytesIO(data))
        if not all(
            _form_appearance_fits(widget, completed) for widget in _widgets(completed.pages[0])
        ):
            return None
        return (
            data
            if _completed_form(
                completed,
                item,
                replace(plan, form_templates=(template,)),
                sources,
                ticket,
            )
            else None
        )
    except Exception:
        return None


def complete_required_forms(
    findings: Sequence[Finding],
    sources: Sequence[SourceRecord],
    ticket: Ticket,
    plan: PacketPlan,
) -> tuple[bytes, ...] | None:
    """Complete exactly the verified inventory in order, without optional attachments."""
    result: list[bytes] = []
    used: set[int] = set()
    for item in required_form_items(findings, sources, ticket):
        for index, template in enumerate(plan.form_templates):
            if (
                index not in used
                and (completed := complete_form(template, item, plan, sources, ticket)) is not None
            ):
                used.add(index)
                result.append(completed)
                break
        else:
            return None
    return tuple(result)


def _completed_form(
    reader: PdfReader,
    item: str,
    plan: PacketPlan,
    sources: Sequence[SourceRecord],
    ticket: Ticket | None,
) -> bool:
    from parkingbreaker.research.procedure import jurisdiction_in_text, same_stage

    if ticket is None:
        return False
    known = _known_form_values(plan, ticket)
    if known is None:
        return False
    name = _document_name(item).casefold()
    for template in plan.form_templates:
        digest = hashlib.sha256(template).hexdigest()
        source = next(
            (
                s
                for s in sources
                if s.sha256 == digest
                and s.http_status == 200
                and s.authority_class is AuthorityClass.OFFICIAL_FORM
            ),
            None,
        )
        if source is None or not ticket.jurisdiction or not ticket.stage:
            continue
        if not jurisdiction_in_text(ticket.jurisdiction, source.text) or not same_stage(
            source.text, ticket.stage
        ):
            continue
        try:
            original = PdfReader(io.BytesIO(template))
            # Multi-page/flat forms need a future explicit mapping; do not guess.
            if original.is_encrypted or len(original.pages) != 1:
                continue
            template_page = original.pages[0]
            template_text = template_page.extract_text() or ""
            if " ".join(template_text.split()) != " ".join(source.text.split()):
                continue
            fields = _widgets(template_page)
            if (
                not fields
                or not template_text.splitlines()
                or (template_text.splitlines()[0].strip().casefold() != name)
            ):
                continue
            for page in reader.pages:
                visible = page.extract_text() or ""
                if not visible.startswith(template_text):
                    continue
                widgets = _widgets(page)
                if len(widgets) != len(fields):
                    continue
                valid = True
                for field, widget in zip(fields, widgets, strict=True):
                    label = str(field.get("/T", "")).casefold()
                    raw_value = str(widget.get("/V", ""))
                    value = raw_value.strip()
                    if (
                        widget.get("/T") != field.get("/T")
                        or field.get("/FT") != "/Tx"
                        or widget.get("/FT") != field.get("/FT")
                        or widget.get("/Rect") != field.get("/Rect")
                        or widget.get("/Ff", 0) != field.get("/Ff", 0)
                        or (
                            label in NARRATIVE_FIELDS
                            and (
                                not known.get("customer_statement")
                                or raw_value != known["customer_statement"]
                            )
                        )
                        or (
                            label not in NARRATIVE_FIELDS
                            and (
                                label not in FORM_FIELDS
                                or not known.get(FORM_FIELDS[label])
                                or value != known[FORM_FIELDS[label]]
                            )
                        )
                        or (int(field.get("/Ff", 0)) & 2 and not value)
                        or (value and " ".join(value.split()) not in " ".join(visible.split()))
                    ):
                        valid = False
                if valid:
                    return True
        except Exception:
            continue
    return False


def _document_name(item: str) -> str:
    name = re.sub(
        r"^(?:(?:you must |must )?(?:include|enclose|attach|provide|return) |"
        r"(?:a |the )|(?:completed |signed |copy of (?:the )?))+",
        "",
        item,
        flags=re.I,
    )
    return re.sub(r" (?:is required|must be signed)[.]?$", "", name, flags=re.I).strip(" .")


def _physical_item(item: str) -> bool:
    return bool(
        re.search(
            r"\b(?:SASE|self[ -]addressed(?:[ -]stamped)? envelope|"
            r"(?:return|send|enclose|include|provide) (?:the |an? |your )?original|"
            r"original (?:(?:parking )?(?:ticket|citation)|document))\b",
            item,
            re.I,
        )
    ) and not bool(re.search(r"\bcopy of (?:the |an? |your )?original\b", item, re.I))


_NON_OWNER_PERMISSION_RULE = re.compile(
    r"\bif no,\s*then you must provide documentation\s*(?:\(letter\)\s*)?"
    r"from (?:the )?vehicle owner giving you permission to "
    r"(?:appeal|contest|dispute)\b",
    re.I,
)
_CITATION_COPY_ITEMS = frozenset(
    {
        "citation copy",
        "copy of citation",
        "copy of the citation",
        "copy of your citation",
        "ticket copy",
        "copy of ticket",
        "copy of the ticket",
        "copy of your ticket",
    }
)
_CONDITIONAL_PLACARD_ITEM = re.compile(
    r"copy of (?:your |the |an? )?(?:hp|hvp|handicap\w*|disabled|accessible) placard"
    r"\s*\((?:if|when) applicable\)[.]?",
    re.I,
)
_PLACARD_VIOLATION = re.compile(
    r"\b(?:hp|hvp|handicap\w*|disabled|accessible|placard)\b",
    re.I,
)


def _ticket_is_placard_violation(ticket: Ticket | None) -> bool:
    if ticket is None or not ticket.violation_code.strip():
        return True
    violation_text = f"{ticket.violation_code} {ticket.violation_description or ''}"
    return bool(_PLACARD_VIOLATION.search(violation_text))


def standing_question_applies(quote: str) -> bool:
    return bool(
        re.search(
            r"\bonly (?:the )?registered owner (?:may|can) (?:contest|appeal)\b",
            quote,
            re.I,
        )
        or _NON_OWNER_PERMISSION_RULE.search(" ".join(quote.split()))
    )


def _inventory_item(item: str) -> str:
    # Preserve 'copy of': stripping it could turn an acceptable copy into an original.
    return re.sub(
        r"^(?:(?:you must |must )?(?:include|enclose|attach|provide|return) |a |the )+",
        "",
        item,
        flags=re.I,
    ).strip(" .")


def _requirement_scope(item: str, quote: str) -> str:
    """Keep unrelated requirement or optional wording from changing this item's meaning."""
    subjects = {
        token[:-1] if token.endswith("s") and len(token) > 4 else token
        for token in re.findall(r"[a-z]+", item.casefold())
        if token not in _REQUIREMENT_SUBJECT_FILLER
    }
    if not subjects:
        return quote
    clauses = re.split(r"(?<=[.!?])\s+|\n+|;\s*", quote)
    relevant = [
        clause
        for clause in clauses
        if subjects
        & {
            token[:-1] if token.endswith("s") and len(token) > 4 else token
            for token in re.findall(r"[a-z]+", clause.casefold())
        }
    ]
    return " ".join(relevant) if relevant else quote


def _identifier_requirement_kind(key: FactKey, item: str, quote: str) -> IdentifierKind | None:
    if key not in {FactKey.REQUIRED_ITEMS, FactKey.TICKET_SPECIFIC_REQUIREMENTS}:
        return None
    return classify_identifier_item(_inventory_item(item), _requirement_scope(item, quote))


def requirement_interpretable(key: FactKey, item: str, quote: str) -> bool:
    """Whether the evidenced rule has a supported, deterministic fulfilment check.

    Unknown wording is a research gap, not proof of workflow incompatibility.
    """
    if compulsory_route_stage(item) is not None:
        return quoted_route_stage(item, quote) is not None
    if key is FactKey.REQUIRED_DECLARATIONS and declaration_wording(quote):
        return True
    if key is FactKey.SIGNATURE_REQUIREMENT and signed_declaration_block(quote) == quote:
        return True
    scoped_quote = (
        _requirement_scope(item, quote)
        if key in {FactKey.REQUIRED_ITEMS, FactKey.TICKET_SPECIFIC_REQUIREMENTS}
        else quote
    )
    if not re.search(
        r"\b(?:must|required|shall|only|eligible)\b"
        r"|^(?:include|complete|enclose|attach|submit|provide|sign|return)\b"
        r"|\bmail (?:your |an? |the )?(?:appeal|contest|dispute)(?: letter| request)? with\b",
        scoped_quote,
        re.I,
    ) or re.search(
        r"^no\b|\bnot (?:required|needed|mandatory)\b"
        r"|\b(?:optional|do not|must not|may not|cannot)\b",
        scoped_quote,
        re.I,
    ):
        return False
    if _physical_item(item):
        # An inventory that omits an explicit copy alternative cannot prove that
        # the driver must surrender the original. Leave that requirement unresolved.
        return not (
            re.search(r"\boriginal (?:parking )?(?:ticket|citation)\b", item, re.I)
            and re.search(
                r"\boriginal (?:parking )?(?:ticket|citation)\s+or\s+"
                r"(?:(?:provide|include|send) )?(?:a )?(?:legible |clear )?copy\b",
                scoped_quote,
                re.I,
            )
        )
    if _identifier_requirement_kind(key, item, scoped_quote) is not None:
        return True
    if key is FactKey.CONTEST_ELIGIBILITY:
        if _NON_OWNER_PERMISSION_RULE.search(" ".join(quote.split())):
            return True
        return standing_question_applies(quote) and not re.search(
            r"\b(?:proof|document|registration|copy)\b",
            quote,
            re.I,
        )
    if key is FactKey.PRIOR_STEP_REQUIREMENT:
        return bool(
            re.fullmatch(
                r"(?:you must first request an? |an? )initial review(?: must be completed)?[.]?",
                quote,
                re.I,
            )
        )
    if key is FactKey.SIGNATURE_REQUIREMENT:
        if declaration_wording(quote) and re.search(r"\b(?:signed|sign|signature)\b", quote, re.I):
            return True
        return bool(
            re.fullmatch(
                r"(?:you must sign (?:the |a )?|(?:the |a )?)(?:[\w -]+ )?"
                r"(?:declaration|letter|appeal|form)(?: must be signed| is required)?[.]?",
                item,
                re.I,
            )
        )
    if key is FactKey.REQUIRED_DECLARATIONS:
        if declaration_wording(quote):
            return True
        return bool(
            re.fullmatch(
                r"(?:a |the )?(?:signed )?(?:[\w -]+ )?declaration(?: is required)?[.]?",
                item,
                re.I,
            )
        )
    if key is FactKey.TICKET_SPECIFIC_REQUIREMENTS:
        return requirement_interpretable(FactKey.REQUIRED_ITEMS, _inventory_item(item), quote)
    if key is FactKey.REQUIRED_ITEMS and re.search(r"\b(?:photos?|photographs?)\b", item, re.I):
        return True
    if key is FactKey.MANDATORY_FORMS or "form" in item.casefold():
        return bool(_document_name(item))
    if key is FactKey.REQUIRED_ITEMS and _CONDITIONAL_PLACARD_ITEM.fullmatch(_inventory_item(item)):
        return True
    return key is FactKey.REQUIRED_ITEMS and (
        item.casefold() in {"citation number", *_CITATION_COPY_ITEMS}
        or bool(re.fullmatch(r"copy of (?:the )?[\w -]+", item, re.I))
    )


def _signature_present(page: PageObject) -> bool:
    # A typed name or /V assertion is insufficient: the printed signature field needs
    # an actual ink image in its appearance. Digital signature validation is unsupported.
    for reference in page.get("/Annots", []):
        widget = reference.get_object()
        if (
            widget.get("/Subtype") != "/Widget"
            or str(widget.get("/T", "")).casefold() != "signature"
            or not int(widget.get("/F", 0)) & 4
            or int(widget.get("/F", 0)) & (1 | 2 | 32)
        ):
            continue
        appearance = widget.get("/AP", {}).get("/N")
        if appearance is None:
            continue
        appearance = appearance.get_object()
        rectangle, bounds = widget.get("/Rect"), appearance.get("/BBox")
        if (
            rectangle is None
            or bounds is None
            or len(rectangle) != 4
            or len(bounds) != 4
            or float(rectangle[2]) <= float(rectangle[0])
            or float(rectangle[3]) <= float(rectangle[1])
            or float(bounds[2]) <= float(bounds[0])
            or float(bounds[3]) <= float(bounds[1])
        ):
            continue
        objects = appearance.get("/Resources", {}).get("/XObject", {})
        for name, reference in objects.items():
            ink = reference.get_object()
            if (
                ink.get("/Subtype") != "/Image"
                or f"{name} Do".encode() not in appearance.get_data()
            ):
                continue
            width, height = int(ink.get("/Width", 0)), int(ink.get("/Height", 0))
            if width <= 0 or height <= 0 or ink.get("/BitsPerComponent") != 8:
                continue
            mode = {"/DeviceRGB": "RGB", "/DeviceGray": "L"}.get(ink.get("/ColorSpace"))
            if mode is None:
                continue
            image = Image.frombytes(mode, (width, height), ink.get_data()).convert("L")
            low, high = image.getextrema()
            if isinstance(low, int) and isinstance(high, int) and low < high:
                return True
    return False


def _declaration_present(reader: PdfReader, quote: str, *, signed: bool) -> bool:
    wording = declaration_wording(quote)
    return wording is not None and any(
        " ".join(wording.split()) in " ".join((page.extract_text() or "").split())
        and (not signed or _signature_present(page))
        and (
            not re.search(r"\bdate\b", quote, re.I)
            or re.search(r"\bDate: \d{4}-\d{2}-\d{2}\b", page.extract_text() or "")
        )
        for page in reader.pages
    )


# "Include copies of any documents that support your statement" (SFMTA) or "a copy of any
# evidence you want reviewed": the agency invites supporting papers the customer may choose
# to send. Choosing none is compliant, so such an item never requires packet bytes.
_CUSTOMER_CHOICE_OBJECTS = (
    re.compile(
        r"\bany\s+(?P<object>[^.!?;\n]*?)\s+"
        r"(?:you (?:want|wish|would like|choose)|that supports?|supporting)\b",
        re.I,
    ),
    re.compile(r"\bif you have\s+(?P<object>[^.!?;\n]+)", re.I),
    re.compile(r"\byou may (?:include|attach|enclose)\s+(?P<object>[^.!?;\n]+)", re.I),
    re.compile(r"\b(?P<object>[^.!?;\n]+?)\s+may be included\b", re.I),
)
# These words describe choice wording or generic grammar, not the attachment itself.
_OPTIONAL_ITEM_FILLER = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "are",
        "be",
        "by",
        "choose",
        "copies",
        "copy",
        "fact",
        "for",
        "have",
        "if",
        "include",
        "included",
        "may",
        "of",
        "or",
        "statement",
        "support",
        "supporting",
        "that",
        "the",
        "to",
        "want",
        "which",
        "wish",
        "would",
        "you",
        "your",
    }
)
_REQUIREMENT_SUBJECT_FILLER = _OPTIONAL_ITEM_FILLER | frozenset(
    {
        "be",
        "complete",
        "eligible",
        "enclose",
        "include",
        "included",
        "must",
        "optional",
        "provide",
        "required",
        "return",
        "shall",
        "sign",
        "signed",
        "submit",
    }
)
# Conditions of acceptance stay required however softly they are phrased.
_ALWAYS_REQUIRED = re.compile(
    r"\b(?:form|original|ticket|notice|citation|signature|signed|declaration|identification|"
    r"id|license|proof|registration|ownership|fee|payment|deposit|check|money order|"
    r"envelope|sase|stamp(?:ed)?|notar\w*|affidavit)\b",
    re.I,
)


def customer_optional(item: str, quote: str) -> bool:
    """An invited, customer-chosen enclosure ("any ... you want", "any ... that support")."""
    if not re.search(r"\b(?:documents?|evidence|photos?|copies|copy|records?)\b", item, re.I):
        return False
    if _ALWAYS_REQUIRED.search(item):
        return False
    item_terms = {
        token[:-1] if token.endswith("s") and len(token) > 4 else token
        for token in re.findall(r"[a-z]+", item.casefold())
        if token not in _OPTIONAL_ITEM_FILLER
    }
    if not item_terms:
        return False
    for pattern in _CUSTOMER_CHOICE_OBJECTS:
        for match in pattern.finditer(quote):
            object_terms = {
                token[:-1] if token.endswith("s") and len(token) > 4 else token
                for token in re.findall(r"[a-z]+", match.group("object").casefold())
                if token not in _OPTIONAL_ITEM_FILLER
            }
            if item_terms <= object_terms:
                return True
    return False


def packet_fulfillment_pending(
    key: FactKey,
    item: str,
    quote: str,
    *,
    plan: PacketPlan | None = None,
    sources: Sequence[SourceRecord] = (),
    ticket: Ticket | None = None,
) -> bool:
    """Whether a known requirement can wait for the packet-building step.

    Customer answers and inherently physical items are procedural limits, not packet work.
    Other requirements defer only when the existing fulfilment rules understand them;
    unsupported wording remains an explicit verification gap.
    """
    if compulsory_route_stage(item) is not None:
        return False
    if key in {FactKey.CONTEST_ELIGIBILITY, FactKey.PRIOR_STEP_REQUIREMENT}:
        return False
    if _physical_item(item):
        return False
    if (kind := _identifier_requirement_kind(key, item, quote)) is not None:
        return (
            requirement_interpretable(key, item, quote)
            and plan is not None
            and plan_identifier_fulfilled(kind, plan, letter_page_texts=None)
        )
    if key is FactKey.MANDATORY_FORMS or (
        key in {FactKey.REQUIRED_ITEMS, FactKey.TICKET_SPECIFIC_REQUIREMENTS}
        and re.search(r"\bforms?\b", item, re.I)
    ):
        # An unfillable official form is a known procedural stop, not work to defer
        # until after drafting. Actual freeze still validates the enclosed bytes.
        return plan is not None and requirement_fulfilled(
            key, item, quote, plan, sources=sources, ticket=ticket
        )
    if key in {
        FactKey.REQUIRED_ITEMS,
        FactKey.TICKET_SPECIFIC_REQUIREMENTS,
    } and _CONDITIONAL_PLACARD_ITEM.fullmatch(_inventory_item(item)):
        return not _ticket_is_placard_violation(ticket)
    return requirement_interpretable(key, item, quote)


def requirement_fulfilled(
    key: FactKey,
    item: str,
    quote: str,
    plan: PacketPlan,
    *,
    vehicle_relationship: str | None = None,
    initial_review_completed: bool | None = None,
    sources: Sequence[SourceRecord] = (),
    ticket: Ticket | None = None,
) -> bool:
    if compulsory_route_stage(item) is not None:
        return False
    if key is FactKey.REQUIRED_ITEMS and customer_optional(item, quote):
        return True
    if not requirement_interpretable(key, item, quote) or _physical_item(item):
        return False
    if (kind := _identifier_requirement_kind(key, item, quote)) is not None:
        if plan.frozen_pdf is None:
            return plan_identifier_fulfilled(kind, plan, letter_page_texts=None)
        # The packet renderer owns the letter boundary. Its adopted-text comparison
        # prevents attached forms, copy pages and customer photos from proving that an
        # identifier appears in the written request.
        from parkingbreaker.appeal.packet import PacketError, frozen_letter_text

        try:
            text = frozen_letter_text(
                plan.frozen_pdf, citation_reference=_expected_citation_reference(plan)
            )
        except PacketError:
            return False
        return plan_identifier_fulfilled(kind, plan, letter_page_texts=(text,))
    if key is FactKey.TICKET_SPECIFIC_REQUIREMENTS:
        return requirement_fulfilled(
            FactKey.REQUIRED_ITEMS,
            _inventory_item(item),
            quote,
            plan,
            sources=sources,
            ticket=ticket,
        )
    if key is FactKey.CONTEST_ELIGIBILITY:
        if _NON_OWNER_PERMISSION_RULE.search(" ".join(quote.split())):
            return vehicle_relationship == "REGISTERED_OWNER"
        # A relationship answer proves standing only; a separate proof requirement is
        # not satisfied by the answer and is never treated as an enclosure.
        return (
            vehicle_relationship == "REGISTERED_OWNER"
            and standing_question_applies(quote)
            and not re.search(r"\b(?:proof|document|registration|copy)\b", quote, re.I)
        )
    if key is FactKey.PRIOR_STEP_REQUIREMENT:
        return initial_review_completed is True and bool(
            re.fullmatch(
                r"(?:you must first request an? |an? )initial review(?: must be completed)?[.]?",
                quote,
                re.I,
            )
        )
    if key is FactKey.REQUIRED_ITEMS and _CONDITIONAL_PLACARD_ITEM.fullmatch(_inventory_item(item)):
        if not _ticket_is_placard_violation(ticket):
            return True
        if plan.frozen_pdf is None:
            return False
    if plan.frozen_pdf is None:
        if ticket is not None and (
            key is FactKey.MANDATORY_FORMS
            or (key is FactKey.REQUIRED_ITEMS and "form" in item.casefold())
        ):
            # Before drafting, prove that the saved official form can actually be
            # completed from supplied inputs. Freeze always takes the packet branch
            # below and rechecks the enclosed page, never this preparation result.
            return any(
                complete_form(template, item, plan, sources, ticket) is not None
                for template in plan.form_templates
            )
        # The letter renderer supplies the citation identifier; freeze verifies it.
        # Documents, copies, forms and signatures always need actual packet bytes.
        return key is FactKey.REQUIRED_ITEMS and item.casefold() == "citation number"
    try:
        reader = PdfReader(io.BytesIO(plan.frozen_pdf))
        if reader.is_encrypted:
            return False
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if key is FactKey.MANDATORY_FORMS:
            return _completed_form(reader, item, plan, sources, ticket)
        if key is FactKey.REQUIRED_ITEMS:
            if re.search(r"\b(?:photos?|photographs?)\b", item, re.I):
                return any(customer_evidence_photo_page(page) for page in reader.pages)
            if item.casefold() == "citation number":
                expected = _expected_citation_reference(plan)
                return expected is not None and _citation_reference_present(text, expected)
            if item.casefold() in _CITATION_COPY_ITEMS:
                return any(
                    (page.extract_text() or "").startswith("Copy of citation") and page.images
                    for page in reader.pages
                )
            if "form" in item.lower():
                return _completed_form(reader, item, plan, sources, ticket)
            if re.fullmatch(r"copy of (?:the )?[\w -]+", item, re.I):
                return any(
                    (page.extract_text() or "").splitlines()[0].strip().casefold()
                    == item.casefold()
                    and bool(page.images)
                    for page in reader.pages
                    if (page.extract_text() or "").splitlines()
                )
        if (
            key is FactKey.SIGNATURE_REQUIREMENT
            and not re.search(r"declaration", quote, re.I)
            and declaration_wording(quote) is None
        ):
            if re.search(r"letter|appeal", item, re.I):
                return any(
                    "Sincerely," in (page.extract_text() or "") and _signature_present(page)
                    for page in reader.pages
                )
            return False
        if key in {FactKey.REQUIRED_DECLARATIONS, FactKey.SIGNATURE_REQUIREMENT}:
            return _declaration_present(
                reader,
                quote,
                signed=key is FactKey.SIGNATURE_REQUIREMENT
                or bool(re.search(r"\b(?:signed|sign|signature)\b", quote + " " + item, re.I)),
            )
    except Exception:
        return False
    return False
