"""The frozen PDF packet that is mailed (docs/architecture-v6.md 9.1), and the approval gate.

Mailed, in order: the letter (minimum pages), a copy of the citation, each evidence photo
the customer chose, and any completed required agency form (owner decision 2026-10-16,
as 2.x does; reverses v8 §16 item 3). Each photo page is labelled as uploaded by the
customer.
The exact adopted UTF-8 text is embedded as "adopted-letter.txt".

Output is deterministic: the caller passes `created_at` (use the adoption time), the PDF
/ID is fpdf2's hash of the document content, and nothing reads the clock. The same inputs
always produce the same bytes.

Photo labels state only what we know: "Uploaded by customer", plus the date from the
photo's own EXIF metadata (DateTimeOriginal, else DateTimeDigitized), marked as such.
No other date is ever stamped.
"""

from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from fpdf import FPDF
from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    ContentStream,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from parkingbreaker.appeal.adoption import Adoption, text_sha256
from parkingbreaker.appeal.letter import ENCLOSURES_PREFIX, PAGE_BREAK, enclosure_line
from parkingbreaker.appeal.required_copies import (
    RequiredCopyPages,
    required_copy_items,
    unsupported_copy_requirements,
    validated_copy_pages,
)
from parkingbreaker.case_state import CustomerApproved
from parkingbreaker.mail_pages import MAX_OWN_PAGES, prints_as_letter
from parkingbreaker.research.fulfilment import (
    CITATION_BACK_PAGE_TITLE,
    CITATION_PAGE_TITLE,
    EVIDENCE_PAGE_TITLE,
    UPLOADED_LABEL,
    SignatureRequirement,
    _completed_form,
    customer_packet_image_page,
    required_declarations,
    required_form_items,
    required_form_titles,
    signature_requirement,
)
from parkingbreaker.research.questions import CustomerPosition, PacketPlan
from parkingbreaker.research.resolution import ResearchAnswers
from parkingbreaker.research.run import ResearchResult

LETTER_MAX_PAGES = 2
MAX_IMAGE_PIXELS = 20_000_000
ATTACHMENT_NAME = "adopted-letter.txt"
PHOTO_DATE_LABEL = "Photo date (from file metadata): "


def _customer_photo_page(page: PageObject) -> bool:
    """Compatibility name used by existing packet tests and callers."""
    return customer_packet_image_page(page)


_MARGIN = 72.0  # points; one inch
_BODY_SIZE = 11
_LINE_HEIGHT = 14.0
_LABEL_SIZE = 10
_EXIF_IFD = 0x8769
_DATETIME_ORIGINAL = 36867
_DATETIME_DIGITIZED = 36868
_EXIF_DATE = re.compile(r"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})")


class PacketError(ValueError):
    pass


@dataclass(frozen=True)
class Packet:
    pdf: bytes
    sha256: str
    letter_text_sha256: str
    page_count: int


def exif_photo_date(image_bytes: bytes) -> str | None:
    """The capture date from EXIF, as "YYYY-MM-DD HH:MM:SS", or None if absent/invalid."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            exif = img.getexif()
            ifd = exif.get_ifd(_EXIF_IFD)
            raw = ifd.get(_DATETIME_ORIGINAL) or ifd.get(_DATETIME_DIGITIZED)
    except (UnidentifiedImageError, OSError):
        return None
    if not isinstance(raw, str):
        return None
    match = _EXIF_DATE.fullmatch(raw.strip("\x00 "))
    if not match:
        return None
    y, mo, d, h, mi, s = match.groups()
    return f"{y}-{mo}-{d} {h}:{mi}:{s}"


def photo_labels(image_bytes: bytes) -> tuple[str, ...]:
    date = exif_photo_date(image_bytes)
    return (UPLOADED_LABEL,) if date is None else (UPLOADED_LABEL, PHOTO_DATE_LABEL + date)


def check_image(image_bytes: bytes, what: str) -> None:
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            if img.width * img.height > MAX_IMAGE_PIXELS:
                raise PacketError("image dimensions exceed limit")
            img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise PacketError(f"{what} is not a readable image") from exc


def _packet_image(image_bytes: bytes) -> Image.Image:
    """A lossless, oriented raster; raw customer metadata never reaches the PDF stream."""
    with Image.open(io.BytesIO(image_bytes)) as source:
        image = ImageOps.exif_transpose(source)
    image.info.clear()
    image.getexif().clear()
    # Prevent the renderer from reusing the original encoded JPEG/EXIF stream.
    image.format = None
    return image


def _citation_reference(citation_number: str | None) -> str | None:
    """Normalize an explicit identifier without inferring one from research or prose."""
    if citation_number is None:
        return None
    number = " ".join(citation_number.split())
    try:
        number.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise PacketError("the citation number contains characters the PDF cannot print") from exc
    if not number or not number.isprintable():
        raise PacketError("the citation number must be printable text")
    return f"Citation {number}"


class _PacketPdf(FPDF):
    def __init__(self, citation_reference: str | None = None) -> None:
        super().__init__(orientation="portrait", unit="pt", format="letter")
        self._citation_reference = citation_reference
        self._letter_pages = True
        self.set_margins(_MARGIN, _MARGIN, _MARGIN)
        self.set_auto_page_break(auto=True, margin=_MARGIN)

    def header(self) -> None:
        if self._letter_pages and self._citation_reference is not None:
            # Absolute text leaves the body cursor and pagination untouched. The local
            # graphics state restores the body font even on automatic continuation pages.
            with self.local_context(font_family="Helvetica", font_size=_LABEL_SIZE):
                if self.get_string_width(self._citation_reference) > self.epw:
                    raise PacketError("the citation number does not fit the page header")
                self.text(_MARGIN, _MARGIN / 2, self._citation_reference)

    def text_block(
        self, text: str, *, signature_image: bytes | None = None
    ) -> tuple[int, float] | None:
        self.set_font("Helvetica", size=_BODY_SIZE)
        closings = tuple(re.finditer(r"(?m)^Sincerely,\n\n\n[^\n]+", text))
        if len(closings) != 1:
            if signature_image is not None:
                raise PacketError(
                    "a captured signature needs one unambiguous letter signature block"
                )
            self.multi_cell(0, _LINE_HEIGHT, text, align="L")
            return None
        closing = closings[0]
        before, block, after = text[: closing.start()], closing[0], text[closing.end() :]
        if before:
            # A separate multi_cell consumes its terminal newline as an extra row.
            # The next segment already starts on that row; retain the original spacing.
            prefix = before[:-1] if before.endswith("\n") else before
            self.multi_cell(0, _LINE_HEIGHT, prefix, align="L", new_x="LMARGIN")
        height = self.multi_cell(0, _LINE_HEIGHT, block, dry_run=True, output="HEIGHT")
        assert isinstance(height, (int, float))
        if height > self.eph:
            raise PacketError("the letter signature block does not fit a page")
        if self.will_page_break(height):
            self.add_page()
        signature_position = (self.page - 1, self.get_y() + _LINE_HEIGHT)
        self.multi_cell(0, _LINE_HEIGHT, block, align="L", new_x="LMARGIN")
        if signature_image is not None:
            self.image(
                _packet_image(signature_image),
                x=_MARGIN,
                y=signature_position[1],
                w=120,
                h=28,
            )
        if after:
            suffix = after[1:] if after.startswith("\n") else after
            self.multi_cell(0, _LINE_HEIGHT, suffix, align="L", new_x="LMARGIN")
        return signature_position

    def image_page(self, image_bytes: bytes, labels: Sequence[str]) -> None:
        self._letter_pages = False
        self.add_page()
        self.set_font("Helvetica", size=_LABEL_SIZE)
        for label in labels:
            self.cell(0, _LINE_HEIGHT, label, new_x="LMARGIN", new_y="NEXT")
        top = self.get_y() + _LINE_HEIGHT / 2
        self.image(
            _packet_image(image_bytes),
            x=_MARGIN,
            y=top,
            w=self.epw,
            h=self.h - _MARGIN - top,
            keep_aspect_ratio=True,
        )


def _check_declaration_text(
    text: str, research: ResearchResult | None, *, require_all: bool = True
) -> None:
    try:
        declarations = (
            ()
            if research is None
            else required_declarations(research.findings, research.sources, research.ticket)
        )
    except ValueError as exc:
        raise PacketError(str(exc)) from exc
    if require_all and any(wording not in text for wording in declarations):
        raise PacketError("required agency declaration is missing from adopted text")
    remainder = text
    for wording in declarations:
        remainder = remainder.replace(wording, "")
    if re.search(r"penalty of perjury|\bsworn\b|\bI (?:declare|certify|affirm)\b", remainder, re.I):
        raise PacketError("declaration requires the agency's evidenced wording")


def _check_mailable_pages(pages: Sequence[PageObject]) -> None:
    """Lob prints only US Letter pages, and only so many of them (mail_pages)."""
    if len(pages) > MAX_OWN_PAGES:
        raise PacketError(f"the packet is longer than {MAX_OWN_PAGES} pages")
    if not all(prints_as_letter(page) for page in pages):
        raise PacketError("every packet page must be US Letter (8.5 x 11 inches)")


def _required_form_items(research: ResearchResult) -> tuple[str, ...]:
    return required_form_items(research.findings, research.sources, research.ticket)


def _forms_match_required_items(
    forms: Sequence[PdfReader],
    required_items: Sequence[str],
    plan: PacketPlan,
    research: ResearchResult,
) -> bool:
    """Cover the verified inventory with distinct completed forms, with reassignment.

    A form can satisfy several item descriptions. Augmenting paths avoid rejecting a
    valid assignment just because the first form initially takes another form's only
    match. Matching still uses the full existing provenance and field-mapping check.
    The separate research resolution gate still checks all other unmet requirements.
    """
    items = tuple(required_items)
    if len(forms) != len(items):
        return False
    options = [
        tuple(
            index
            for index, item in enumerate(items)
            if _completed_form(form, item, plan, research.sources, research.ticket)
        )
        for form in forms
    ]
    assigned: dict[int, int] = {}

    def assign(form_index: int, visited: set[int]) -> bool:
        for item_index in options[form_index]:
            if item_index in visited:
                continue
            visited.add(item_index)
            previous = assigned.get(item_index)
            if previous is None or assign(previous, visited):
                assigned[item_index] = form_index
                return True
        return False

    return all(assign(index, set()) for index in range(len(forms)))


def _check_enclosure_manifest(
    text: str,
    *,
    citation_back: bool,
    evidence_count: int,
    form_titles: Sequence[str],
    copy_titles: Sequence[str] = (),
) -> None:
    listed = [line for line in text.splitlines() if line.startswith(ENCLOSURES_PREFIX)]
    if (listed or citation_back or form_titles or copy_titles) and listed != [
        enclosure_line(
            True,
            evidence_count,
            encloses_citation_back=citation_back,
            required_forms=form_titles,
            required_copies=copy_titles,
        )
    ]:
        raise PacketError("the letter's enclosure list does not match the mailed pages")


def build_packet(
    adopted_text: str,
    citation_image: bytes,
    evidence_images: Sequence[bytes],
    created_at: datetime,
    *,
    completed_forms: Sequence[bytes] = (),
    supporting_documents: Sequence[bytes] = (),
    required_copies: Sequence[RequiredCopyPages] = (),
    research: ResearchResult | None = None,
    packet_plan: PacketPlan | None = None,
    customer_signature: bytes | None = None,
    citation_number: str | None = None,
    citation_back_image: bytes | None = None,
) -> Packet:
    """Render the adopted text and enclosures into the frozen PDF."""
    if PAGE_BREAK in adopted_text:
        raise PacketError("separate declaration or exhibit pages are not permitted")
    if supporting_documents:
        raise PacketError("supporting documents are not mailed as exhibit pages")
    if research is not None and unsupported_copy_requirements(
        research.findings, research.sources, research.ticket
    ):
        raise PacketError("required copies need unsupported original or certified verification")
    try:
        copies = validated_copy_pages(
            ()
            if research is None
            else required_copy_items(research.findings, research.sources, research.ticket),
            required_copies,
        )
    except ValueError as exc:
        raise PacketError(str(exc)) from exc
    plan = packet_plan or (research.packet_plan if research else PacketPlan())
    required_forms = () if research is None else _required_form_items(research)
    parsed_forms: list[PdfReader] = []
    for form in completed_forms:
        try:
            form_reader = PdfReader(io.BytesIO(form))
            if form_reader.is_encrypted or len(form_reader.pages) != 1:
                raise PacketError("agency form layout is not supported")
            _check_declaration_text(
                form_reader.pages[0].extract_text() or "", research, require_all=False
            )
            _check_mailable_pages(form_reader.pages)
            parsed_forms.append(form_reader)
        except PacketError:
            raise
        except Exception as exc:
            raise PacketError("completed form is not a readable PDF") from exc
    if (research is None and parsed_forms) or (
        research is not None
        and not _forms_match_required_items(parsed_forms, required_forms, plan, research)
    ):
        raise PacketError("agency form lacks applicable evidence or complete field mapping")
    _check_declaration_text(adopted_text, research)
    signature = (
        SignatureRequirement.UNKNOWN
        if research is None
        else signature_requirement(research.findings, research.sources, research.ticket)
    )
    if signature is SignatureRequirement.REQUIRED and customer_signature is None:
        # A captured signature on a completed agency form may fulfil its own rule.
        from parkingbreaker.research.fulfilment import _signature_present

        if not any(
            _signature_present(page)
            for form in completed_forms
            for page in PdfReader(io.BytesIO(form)).pages
        ):
            raise PacketError("required signature needs a captured customer signature artifact")
    try:
        adopted_text.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise PacketError("the adopted text contains characters the PDF cannot print") from exc
    _check_enclosure_manifest(
        adopted_text,
        citation_back=citation_back_image is not None,
        evidence_count=len(evidence_images),
        copy_titles=tuple(item.title for item, _ in copies),
        form_titles=()
        if research is None
        else required_form_titles(research.findings, research.sources, research.ticket),
    )
    check_image(citation_image, "the citation copy")
    if citation_back_image is not None:
        check_image(citation_back_image, "the citation back")
    for i, image in enumerate(evidence_images, start=1):
        check_image(image, f"evidence photo {i}")
    if customer_signature is not None:
        check_image(customer_signature, "customer signature")
        with Image.open(io.BytesIO(customer_signature)) as ink:
            rgba = ink.convert("RGBA")
            background = Image.new("RGBA", rgba.size, "white")
            background.alpha_composite(rgba)
            low, high = background.convert("L").getextrema()
            if low == high:
                raise PacketError("customer signature artifact contains no ink")

    citation_reference = _citation_reference(citation_number)
    pdf = _PacketPdf(citation_reference)
    pdf.set_creation_date(created_at)
    pdf.set_title("Parking citation appeal")
    pdf.add_page()
    signature_position = pdf.text_block(adopted_text, signature_image=customer_signature)
    if pdf.page > LETTER_MAX_PAGES:
        raise PacketError(f"the letter is longer than {LETTER_MAX_PAGES} pages")

    # The citation number on every photo page lets a separated page find its file.
    reference = () if citation_reference is None else (citation_reference,)
    pdf.image_page(citation_image, (CITATION_PAGE_TITLE, UPLOADED_LABEL, *reference))
    if citation_back_image is not None:
        pdf.image_page(citation_back_image, (CITATION_BACK_PAGE_TITLE, UPLOADED_LABEL, *reference))
    total = len(evidence_images)
    for i, image in enumerate(evidence_images, start=1):
        pdf.image_page(
            image, (f"{EVIDENCE_PAGE_TITLE} {i} of {total}", *photo_labels(image), *reference)
        )

    for item, pages in copies:
        for index, page in enumerate(pages, 1):
            check_image(page, "required document copy")
            pdf.image_page(
                page, (item.title, UPLOADED_LABEL, f"Page {index} of {len(pages)}", *reference)
            )

    pdf.embed_file(
        bytes=adopted_text.encode("utf-8"),
        basename=ATTACHMENT_NAME,
        modification_date=created_at,
        mime_type="text/plain",
    )
    data = bytes(pdf.output())
    if completed_forms or customer_signature is not None:
        writer = PdfWriter(clone_from=io.BytesIO(data))
        if customer_signature is not None:
            assert signature_position is not None
            signature_page, signature_y = signature_position
            signature_bottom = pdf.h - signature_y - 28
            _signature_widget(writer, signature_page, signature_bottom)
        for form in completed_forms:
            try:
                form_pdf = PdfReader(io.BytesIO(form))
                if form_pdf.is_encrypted or not form_pdf.pages:
                    raise PacketError("completed form is not a readable PDF")
                writer.append(form_pdf)
            except Exception as exc:
                raise PacketError("completed form is not a readable PDF") from exc
        output = io.BytesIO()
        writer.write(output)
        data = output.getvalue()
    _check_mailable_pages(PdfReader(io.BytesIO(data)).pages)
    return Packet(
        pdf=data,
        sha256=hashlib.sha256(data).hexdigest(),
        letter_text_sha256=text_sha256(adopted_text),
        page_count=len(PdfReader(io.BytesIO(data)).pages),
    )


def _signature_widget(writer: PdfWriter, page_index: int, bottom: float) -> None:
    """Bind the captured ink image to a printed signature widget for the freeze check."""
    page = writer.pages[page_index]
    resources = page["/Resources"]
    assert isinstance(resources, DictionaryObject)
    objects = resources["/XObject"].get_object()
    assert isinstance(objects, DictionaryObject)
    image_name = next(reversed(objects))
    appearance = DecodedStreamObject()
    appearance.set_data(f"q 120 0 0 28 0 0 cm {image_name} Do Q".encode())
    appearance.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject([NumberObject(v) for v in (0, 0, 120, 28)]),
            NameObject("/Resources"): DictionaryObject({NameObject("/XObject"): objects}),
        }
    )
    # The image is already printed in the letter's signature block. A matching appearance
    # makes the captured artifact inspectable by the same fulfilment check as form ink.
    widget = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/FT"): NameObject("/Sig"),
            NameObject("/T"): TextStringObject("Signature"),
            NameObject("/F"): NumberObject(4),
            NameObject("/Rect"): ArrayObject(
                [NumberObject(v) for v in (72, int(bottom), 192, int(bottom + 28))]
            ),
            NameObject("/AP"): DictionaryObject({NameObject("/N"): writer._add_object(appearance)}),
        }
    )
    reference = writer._add_object(widget)
    page[NameObject("/Annots")] = ArrayObject([*page.get("/Annots", []), reference])
    writer._root_object[NameObject("/AcroForm")] = writer._add_object(
        DictionaryObject({NameObject("/Fields"): ArrayObject([reference])})
    )


class ApprovalRefused(ValueError):
    pass


def _bind_citation_plan(plan: PacketPlan, citation_number: str | None) -> PacketPlan:
    """Carry the explicit identifier in the effective plan; never accept a contradiction."""
    if citation_number is None:
        return plan
    fields = dict(plan.customer_fields)
    if len(fields) != len(plan.customer_fields):
        raise ApprovalRefused("the saved packet plan has contradictory customer fields")
    existing = fields.get("citation_number")
    if existing is not None:
        if " ".join(existing.split()) != " ".join(citation_number.split()):
            raise ApprovalRefused("the citation number contradicts the saved packet plan")
        return plan
    return replace(
        plan, customer_fields=(*plan.customer_fields, ("citation_number", citation_number))
    )


def _confirm_letter_boundary(
    text: str, expected: Sequence[PageObject], frozen: Sequence[PageObject], boundary: int
) -> None:
    """Fail closed unless this PDF's own pages confirm the recomputed letter boundary.

    The renderer's pagination is only an estimate of where the frozen letter ends, so
    it must agree with the frozen pages themselves. approval_gate's whitespace-
    normalized page text is the reliable comparison; each recomputed letter page must
    appear inside its frozen page (the frozen pages may carry a citation header the
    re-render cannot reproduce). The adopted text's final non-empty line must land on
    the last of those pages and must not continue onto the page after it.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise PacketError("frozen letter page boundary is not recoverable")
    for index in range(boundary):
        wanted = " ".join((expected[index].extract_text() or "").split())
        found = " ".join((frozen[index].extract_text() or "").split())
        if not wanted or wanted not in found:
            raise PacketError("frozen letter page boundary is not recoverable")
    last = " ".join(lines[-1].split())
    on_last = " ".join((frozen[boundary - 1].extract_text() or "").split())
    after = " ".join((frozen[boundary].extract_text() or "").split())
    if last not in on_last or last in after:
        raise PacketError("frozen letter page boundary is not recoverable")


def frozen_signature_artwork(pdf: bytes) -> bytes | None:
    """Recover the frozen signature as an image that re-prints unchanged; None means unsigned.

    Only the signature widget our packet builder places on the letter page counts:
    enclosed agency forms and their fields are not our captured ink. Decode through
    pypdf's image API, including palettes, bilevel images, soft masks and JPEG colour
    spaces, and keep CMYK ink in its own colour space (PIL's conversion is not
    colour-exact). Any unreadable signed appearance fails closed, even if this agency
    did not require a signature. Never substitute a photo for the ink.
    """
    try:
        reader = PdfReader(io.BytesIO(pdf))
        # The same renderer places all letter pages before the citation, photos and
        # forms. Recompute that boundary from the frozen adopted text, as approval_gate
        # does; agency /Sig widgets on later pages are never our captured artwork.
        embedded = reader.attachments[ATTACHMENT_NAME]
        if len(embedded) != 1:
            raise PacketError("frozen letter text is not recoverable")
        text = embedded[0].decode("utf-8")
        letter = _PacketPdf()
        letter.add_page()
        letter.text_block(text)
        if letter.page > LETTER_MAX_PAGES or len(reader.pages) <= letter.page:
            raise PacketError("frozen letter page boundary is not recoverable")
        expected = PdfReader(io.BytesIO(bytes(letter.output()))).pages
        _confirm_letter_boundary(text, expected, reader.pages, letter.page)
        for page in reader.pages[: letter.page]:
            for reference in page.get("/Annots", []):
                widget = reference.get_object()
                if (
                    widget.get("/Subtype") != "/Widget"
                    or widget.get("/FT") != "/Sig"
                    or str(widget.get("/T", "")).casefold() != "signature"
                ):
                    continue
                appearance = widget["/AP"]["/N"].get_object()
                names = [
                    operands[0]
                    for operands, operator in ContentStream(appearance, reader).operations
                    if operator == b"Do"
                ]
                if len(names) != 1:
                    raise PacketError("frozen signature appearance is not recoverable")
                ink = appearance["/Resources"]["/XObject"][names[0]].get_object()
                width, height = int(ink["/Width"]), int(ink["/Height"])
                if not (width > 0 and height > 0 and width * height <= MAX_IMAGE_PIXELS):
                    raise PacketError("frozen signature image is not recoverable")
                if ink.get("/ImageMask") == BooleanObject(True):
                    # A stencil paints black where its bit is zero (or one with an
                    # inverted Decode). Preserve the unpainted pixels as transparent.
                    if ink.get("/BitsPerComponent", 1) != 1:
                        raise PacketError("invalid signature image mask")
                    decode = ink.get("/Decode", [0, 1])
                    if decode not in ([0, 1], [1, 0]):
                        raise PacketError("invalid signature image mask decode")
                    filters = ink.get("/Filter", [])
                    if isinstance(filters, str):
                        filters = [filters]
                    if any(f in filters for f in ("/CCITTFaxDecode", "/JBIG2Decode")):
                        # pypdf wraps these compressed bitmaps in a TIFF/PNG container.
                        alpha = Image.open(io.BytesIO(ink.get_data())).convert("L")
                    else:
                        alpha = Image.frombytes("1", (width, height), ink.get_data()).convert("L")
                    if alpha.size != (width, height):
                        raise PacketError("invalid signature image mask size")
                    if decode == [0, 1]:
                        alpha = ImageOps.invert(alpha)
                    artwork = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                    artwork.putalpha(alpha)
                else:
                    # The appearance's own resources identify the captured ink,
                    # independently of the page's photos or other forms.
                    signature_page = PageObject()
                    signature_page[NameObject("/Resources")] = appearance["/Resources"]
                    signature_page[NameObject("/Contents")] = appearance
                    image = signature_page.images[names[0]].image
                    if image is None:
                        raise PacketError("frozen signature image is not recoverable")
                    if image.mode == "CMYK":
                        # PIL's CMYK conversion is not colour-exact: re-embed the frozen
                        # ink samples in their own colour space (lossless TIFF) so the
                        # re-mail prints the same pixels as the original.
                        out = io.BytesIO()
                        image.save(out, "TIFF")
                        return out.getvalue()
                    artwork = image.convert("RGBA")
                out = io.BytesIO()
                artwork.save(out, "PNG")
                return out.getvalue()
    except Exception as exc:
        raise PacketError("frozen signature is not recoverable") from exc
    return None


def _copy_page_identity(page: PageObject) -> object:
    """Compare printable operations and their resources, never just extracted labels.

    Resource names and object numbers are serialization details. Resolved resources,
    decoded stream bytes, clipping/transformation operations and page boxes are exact.
    Reject any page features our image renderer does not produce.
    """
    if set(page) - {"/Type", "/Parent", "/Resources", "/Contents", "/MediaBox", "/Group"}:
        raise ValueError("unexpected copy page features")
    resources = page["/Resources"].get_object()
    if not isinstance(resources, DictionaryObject):
        raise ValueError("copy page resources are invalid")
    if set(resources) - {"/Font", "/XObject", "/ProcSet"}:
        raise ValueError("unexpected copy page resources")

    def resolved(value: object, depth: int = 0) -> object:
        if depth > 20:
            raise ValueError("cyclic copy resource")
        if hasattr(value, "get_object"):
            value = value.get_object()
        if isinstance(value, DictionaryObject):
            entries = tuple(
                sorted(
                    (str(key), resolved(item, depth + 1))
                    for key, item in value.items()
                    if str(key) != "/Length"
                )
            )
            if hasattr(value, "get_data"):
                return entries, value.get_data()
            return entries
        if isinstance(value, (list, tuple)):
            return tuple(resolved(item, depth + 1) for item in value)
        return value

    content = page.get_contents()
    if content is None:
        raise ValueError("copy page has no printable content")
    operations = []
    for arguments, operator in content.operations:
        args = list(arguments)
        if operator in {b"Do", b"Tf"}:
            category = "/XObject" if operator == b"Do" else "/Font"
            entries = resources[category]
            if not isinstance(entries, DictionaryObject):
                raise ValueError("copy resource category is invalid")
            args[0] = resolved(entries[args[0]])
        operations.append((operator, tuple(args)))
    pixels = []
    for image in page.images:
        artwork = image.image
        if artwork is None:
            raise ValueError("copy image pixels are unavailable")
        pixels.append((artwork.mode, artwork.size, artwork.tobytes()))
    return tuple(page.mediabox), resolved(page.get("/Group")), tuple(operations), tuple(pixels)


def approval_gate(
    adoption: Adoption,
    packet: Packet,
    *,
    research: ResearchResult,
    answers: ResearchAnswers | None = None,
    customer_position: CustomerPosition | None = None,
    packet_plan: PacketPlan | None = None,
    citation_number: str | None = None,
    required_copies: Sequence[RequiredCopyPages] = (),
) -> CustomerApproved:
    """Validate the current saved research against the actual frozen packet bytes."""
    if not isinstance(research, ResearchResult):
        raise ApprovalRefused("current saved research is required")
    if unsupported_copy_requirements(research.findings, research.sources, research.ticket):
        raise ApprovalRefused("required copies need unsupported original or certified verification")
    if hashlib.sha256(packet.pdf).hexdigest() != packet.sha256:
        raise ApprovalRefused("frozen packet hash does not match its contents")
    try:
        pdf = PdfReader(io.BytesIO(packet.pdf))
        embedded = pdf.attachments[ATTACHMENT_NAME]
    except Exception as exc:
        raise ApprovalRefused("frozen packet has no adopted text") from exc
    try:
        _check_mailable_pages(pdf.pages)
    except PacketError as exc:
        raise ApprovalRefused(str(exc)) from exc
    if (
        adoption.sha256 != packet.letter_text_sha256
        or len(embedded) != 1
        or hashlib.sha256(embedded[0]).hexdigest() != adoption.sha256
    ):
        raise ApprovalRefused("the adopted text is not the text in this packet")
    plan = replace(
        packet_plan or research.packet_plan, frozen_pdf=packet.pdf, fulfilled_items=frozenset()
    )
    plan = _bind_citation_plan(plan, citation_number)
    # Recompute the minimum letter pages from the adopted bytes. Extra pages must each
    # be the customer's labelled photo pages or independently fulfil an applicable required
    # form; any other added page fails here.
    text = embedded[0].decode("utf-8")
    try:
        _check_declaration_text(text, research)
        _check_declaration_text(
            "\n".join(page.extract_text() or "" for page in pdf.pages), research
        )
        citation_reference = _citation_reference(citation_number)
    except PacketError as exc:
        raise ApprovalRefused(str(exc)) from exc
    letter_pdf = _PacketPdf(citation_reference)
    letter_pdf.set_creation_date(adoption.adopted_at)
    try:
        letter_pdf.add_page()
        letter_pdf.text_block(text)
    except PacketError as exc:
        raise ApprovalRefused(str(exc)) from exc
    letter_pages = letter_pdf.page
    if PAGE_BREAK in text or letter_pages > LETTER_MAX_PAGES:
        raise ApprovalRefused("packet exceeds the minimum letter page budget")
    allowed = _required_form_items(research)
    expected_letter = PdfReader(io.BytesIO(bytes(letter_pdf.output())))
    actual_letter = pdf.pages[:letter_pages]
    if len(actual_letter) != letter_pages or any(
        " ".join((actual.extract_text() or "").split())
        != " ".join((expected.extract_text() or "").split())
        for actual, expected in zip(actual_letter, expected_letter.pages, strict=True)
    ):
        raise ApprovalRefused("visible letter differs from adopted text")
    extras = pdf.pages[letter_pages:]
    # The customer's own photo pages follow the letter: a title, the uploaded label, an image.
    photo_titles: list[str] = []
    while extras and _customer_photo_page(extras[0]):
        photo_titles.append((extras[0].extract_text() or "").splitlines()[0])
        extras = extras[1:]
    try:
        copies = validated_copy_pages(
            required_copy_items(research.findings, research.sources, research.ticket),
            required_copies,
        )
        expected_copies = _PacketPdf(citation_reference)
        expected_copies.set_creation_date(adoption.adopted_at)
        expected_copies._set_min_pdf_version("1.4")
        for item, pages in copies:
            for index, copy_page in enumerate(pages, 1):
                check_image(copy_page, "required document copy")
                expected_copies.image_page(
                    copy_page,
                    (
                        item.title,
                        UPLOADED_LABEL,
                        f"Page {index} of {len(pages)}",
                        *((citation_reference,) if citation_reference else ()),
                    ),
                )
        count = sum(len(pages) for _, pages in copies)
        if count:
            expected_pages = PdfReader(io.BytesIO(bytes(expected_copies.output()))).pages
            if len(extras) < count or any(
                _copy_page_identity(actual) != _copy_page_identity(expected)
                for actual, expected in zip(extras[:count], expected_pages, strict=True)
            ):
                raise ValueError("required copy pages differ from selected originals")
            extras = extras[count:]
    except Exception as exc:
        raise ApprovalRefused("required copy pages are missing or altered") from exc
    if len(extras) > len(allowed):
        raise ApprovalRefused("exhibit pages are not permitted")
    forms: list[PdfReader] = []
    for page in extras:
        writer = PdfWriter()
        writer.add_page(page)
        output = io.BytesIO()
        writer.write(output)
        forms.append(PdfReader(io.BytesIO(output.getvalue())))
    resolution = research.resolve(answers, customer_position=customer_position, packet_plan=plan)
    if not resolution.ready:
        keys = ", ".join(str(key) for gap in resolution.pending for key in gap.keys)
        raise ApprovalRefused(f"frozen packet has unresolved Tier 1 requirements: {keys}")
    if not _forms_match_required_items(forms, allowed, plan, research):
        raise ApprovalRefused("extra page is not a usable required agency form")
    has_back = CITATION_BACK_PAGE_TITLE in photo_titles
    evidence_count = sum(title.startswith(EVIDENCE_PAGE_TITLE + " ") for title in photo_titles)
    expected_titles = [CITATION_PAGE_TITLE]
    if has_back:
        expected_titles.append(CITATION_BACK_PAGE_TITLE)
    expected_titles.extend(
        f"{EVIDENCE_PAGE_TITLE} {i} of {evidence_count}" for i in range(1, evidence_count + 1)
    )
    if photo_titles != expected_titles:
        raise ApprovalRefused("customer photo pages are missing, mislabeled or out of order")
    try:
        _check_enclosure_manifest(
            text,
            citation_back=has_back,
            evidence_count=evidence_count,
            copy_titles=tuple(item.title for item, _ in copies),
            form_titles=required_form_titles(research.findings, research.sources, research.ticket),
        )
    except PacketError as exc:
        raise ApprovalRefused(str(exc)) from exc
    return CustomerApproved()
