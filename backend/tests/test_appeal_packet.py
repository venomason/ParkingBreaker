from __future__ import annotations

import binascii
import dataclasses
import hashlib
import io
import struct
import zlib
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fpdf import FPDF
from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    DictionaryObject,
    NameObject,
    TextStringObject,
)

from parkingbreaker.appeal.adoption import adopt
from parkingbreaker.appeal.drafting import Draft, DraftSentence
from parkingbreaker.appeal.letter import LetterInput, Sender, render_letter
from parkingbreaker.appeal.packet import (
    ATTACHMENT_NAME,
    MAX_IMAGE_PIXELS,
    ApprovalRefused,
    PacketError,
    _customer_photo_page,
    approval_gate,
    build_packet,
    exif_photo_date,
)
from parkingbreaker.case_state import CustomerApproved
from parkingbreaker.research.fakes import fixed_clock
from samples import CONTEST_ADDRESS, SAN_FRANCISCO

AT = datetime(2026, 10, 11, 18, 30, tzinfo=UTC)


def image(
    fmt: str = "PNG", taken: str | None = None, color: tuple[int, int, int] = (200, 30, 30)
) -> bytes:
    buffer = io.BytesIO()
    kwargs = {}
    if taken is not None:
        exif = Image.Exif()
        exif.get_ifd(0x8769)[36867] = taken
        kwargs["exif"] = exif
    Image.new("RGB", (240, 160), color).save(buffer, fmt, **kwargs)
    return buffer.getvalue()


def letter_text(sentences: int = 3, hardship: bool = False) -> str:
    draft = Draft(
        tuple(
            DraftSentence(f"Sentence number {i} says the sign was covered by a tree.", (), i // 4)
            for i in range(sentences)
        )
    )
    inp = LetterInput(
        sender=Sender("Jane Doe", ("1 Main St", "Oakland, CA 94601")),
        letter_date=date(2026, 10, 11),
        agency=SAN_FRANCISCO,
        mailing_address=CONTEST_ADDRESS,
        citation_number="SF-998877",
        plate="7ABC123",
        draft=draft,
        hardship_affirmed=hardship,
    )
    return render_letter(inp).text


CITATION = image("PNG", color=(10, 10, 200))
EVIDENCE = [image("JPEG", taken="2026:10:01 08:15:00"), image("PNG")]


def reader(pdf: bytes) -> PdfReader:
    return PdfReader(io.BytesIO(pdf))


def normalised(text: str) -> str:
    return " ".join(text.split())


def test_mailed_packet_carries_labelled_photo_pages_and_no_default_declaration() -> None:
    packet = build_packet(letter_text(hardship=True), CITATION, EVIDENCE, AT)
    pdf = reader(packet.pdf)
    pages = [p.extract_text() for p in pdf.pages]
    assert packet.page_count == len(pages) == 2 + len(EVIDENCE)
    assert pages[0].startswith("Jane Doe")
    assert not pdf.pages[0].images
    for text in ("Copy of citation", "Evidence photo", "DECLARATION", "penalty of perjury"):
        assert text not in pages[0]
    assert pages[1].splitlines()[:2] == ["Copy of citation", "Uploaded by customer"]
    assert pdf.pages[1].images
    for i, page in enumerate(pdf.pages[2:], start=1):
        assert page.extract_text().splitlines()[:2] == [
            f"Evidence photo {i} of {len(EVIDENCE)}",
            "Uploaded by customer",
        ]
        assert page.images


def test_pdf_embeds_the_adopted_text_exactly() -> None:
    text = letter_text(hardship=True)
    packet = build_packet(text, CITATION, EVIDENCE, AT)
    pdf = reader(packet.pdf)
    assert pdf.attachments[ATTACHMENT_NAME] == [text.encode("utf-8")]
    letter_part, _, declarations = text.partition("\f")
    assert normalised(pdf.pages[0].extract_text()) == normalised(letter_part)
    assert not declarations


def test_long_letter_spans_two_pages_and_still_matches() -> None:
    text = letter_text(sentences=60)
    packet = build_packet(text, CITATION, [], AT)
    pdf = reader(packet.pdf)
    assert packet.page_count == 3  # two letter pages, then the citation copy
    visible = " ".join(p.extract_text() for p in pdf.pages[:2])
    assert normalised(visible) == normalised(text)


def test_letter_longer_than_two_pages_is_refused() -> None:
    with pytest.raises(PacketError, match="longer than 2 pages"):
        build_packet(letter_text(sentences=150), CITATION, [], AT)


def test_output_is_deterministic() -> None:
    first = build_packet(letter_text(), CITATION, EVIDENCE, AT)
    second = build_packet(letter_text(), CITATION, EVIDENCE, AT)
    assert first == second
    other_time = build_packet(letter_text(), CITATION, EVIDENCE, datetime(2026, 1, 1, tzinfo=UTC))
    assert other_time.sha256 != first.sha256
    metadata = reader(first.pdf).metadata
    assert metadata is not None
    assert str(metadata["/CreationDate"]) == "D:20261011183000Z"


def test_packet_hashes() -> None:
    import hashlib

    text = letter_text()
    packet = build_packet(text, CITATION, EVIDENCE, AT)
    assert packet.sha256 == hashlib.sha256(packet.pdf).hexdigest()
    assert packet.letter_text_sha256 == hashlib.sha256(text.encode()).hexdigest()


def test_exif_dates() -> None:
    assert exif_photo_date(image("JPEG", taken="2026:10:01 08:15:00")) == "2026-10-01 08:15:00"
    assert exif_photo_date(image("JPEG")) is None
    assert exif_photo_date(image("JPEG", taken="yesterday")) is None
    assert exif_photo_date(b"not an image") is None


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((b"junk", EVIDENCE), "citation copy is not a readable image"),
        ((CITATION, [EVIDENCE[0], b"junk"]), "evidence photo 2"),
    ],
)
def test_unreadable_images_are_refused(args: tuple[bytes, list[bytes]], message: str) -> None:
    with pytest.raises(PacketError, match=message):
        build_packet(letter_text(), *args, AT)


def synthetic_png(width: int, height: int, *, corrupt_pixels: bool = False) -> bytes:
    def chunk(kind: bytes, content: bytes) -> bytes:
        crc = binascii.crc32(kind + content) & 0xFFFFFFFF
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + b"".join(
        (
            chunk(b"IHDR", ihdr),
            chunk(
                b"IDAT",
                b"not-zlib" if corrupt_pixels else zlib.compress(b"\x00" * (width * 3 + 1)),
            ),
            chunk(b"IEND", b""),
        )
    )


def test_image_pixel_bound_is_checked_before_decode() -> None:
    width = height = int(MAX_IMAGE_PIXELS**0.5) + 1
    with pytest.raises(PacketError, match="image dimensions exceed limit"):
        build_packet(letter_text(), synthetic_png(width, height), [], AT)


def test_image_pixels_must_fully_decode() -> None:
    with pytest.raises(PacketError, match="not a readable image"):
        build_packet(letter_text(), synthetic_png(2, 2, corrupt_pixels=True), [], AT)


@pytest.mark.parametrize("orientation", [1, 6])
def test_packet_raster_strips_metadata_without_changing_pixels_or_capture_date(
    orientation: int,
) -> None:
    exif = Image.Exif()
    exif[34853] = {1: "N", 2: (37, 46, 30), 3: "W", 4: (122, 25, 0)}
    exif[0x8769] = {36867: "2026:10:01 08:15:00"}
    exif[274] = orientation
    uploaded = io.BytesIO()
    pixels = Image.new("RGB", (40, 30), "red")
    pixels.putpixel((0, 0), (0, 0, 255))
    pixels.save(uploaded, "JPEG", exif=exif)
    photo = uploaded.getvalue()
    expected = ImageOps.exif_transpose(Image.open(io.BytesIO(photo))).convert("RGB")
    pdf = reader(build_packet(letter_text(), CITATION, [photo], AT).pdf)
    page = pdf.pages[2]
    assert "2026-10-01 08:15:00" in page.extract_text()
    resources = page["/Resources"]
    assert isinstance(resources, DictionaryObject)
    objects = resources["/XObject"].get_object()
    assert isinstance(objects, DictionaryObject)
    for reference in objects.values():
        stream = reference.get_object()
        assert stream.get("/Metadata") is None
        assert (stream["/Width"], stream["/Height"]) == expected.size
        assert stream.get_data() == expected.tobytes()


def test_text_must_be_printable_and_have_one_page_break() -> None:
    with pytest.raises(PacketError, match="cannot print"):
        build_packet("Nguyễn", CITATION, [], AT)
    with pytest.raises(PacketError, match="separate declaration"):
        build_packet("a\fb\fc", CITATION, [], AT)


# --- Approval gate ---------------------------------------------------------------------------


def test_approval_gate_needs_the_adoption_of_this_packets_text() -> None:
    from test_research_records import result_fixture

    research = result_fixture()
    text = letter_text()
    packet = build_packet(text, CITATION, EVIDENCE, AT, citation_number="SF-998877")
    assert (
        approval_gate(
            adopt(text, "cust_1", fixed_clock(AT)),
            packet,
            research=research,
            citation_number="SF-998877",
        )
        == CustomerApproved()
    )
    edited = text.replace("covered", "hidden")
    with pytest.raises(ApprovalRefused):
        approval_gate(
            adopt(edited, "cust_1", fixed_clock(AT)),
            packet,
            research=research,
            citation_number="SF-998877",
        )
    stale = dataclasses.replace(packet, letter_text_sha256="0" * 64)
    with pytest.raises(ApprovalRefused):
        approval_gate(
            adopt(text, "cust_1", fixed_clock(AT)),
            stale,
            research=research,
            citation_number="SF-998877",
        )


def test_frozen_packet_gate_ignores_a_retired_position_and_keeps_rechecking() -> None:
    """A retired defence selection cannot promote a gap or block an approved packet."""
    from dataclasses import replace

    from parkingbreaker.research.questions import (
        CustomerPosition,
        FactKey,
        PacketPlan,
        RequirementEvidence,
    )
    from samples import source
    from test_research_records import result_fixture

    research = result_fixture()
    text = letter_text()
    packet = build_packet(text, CITATION, EVIDENCE, AT, citation_number="SF-998877")
    adoption = adopt(text, "cust_1", fixed_clock(AT))
    assert (
        approval_gate(adoption, packet, research=research, citation_number="SF-998877")
        == CustomerApproved()
    )
    research = replace(
        research,
        findings=tuple(f for f in research.findings if f.key is not FactKey.CODE_EXCEPTIONS),
    )
    assert (
        approval_gate(
            adoption,
            packet,
            research=research,
            citation_number="SF-998877",
            customer_position=CustomerPosition(exception_basis="loading passengers"),
        )
        == CustomerApproved()
    )
    # The retained procedural promotion still re-checks the packet: a required code
    # provision that is no longer evidenced blocks approval.
    quote = "You must include the code section."
    page = source(quote)
    research = replace(
        research,
        findings=tuple(f for f in research.findings if f.key is not FactKey.GOVERNING_CODE_SECTION),
    )
    with pytest.raises(ApprovalRefused):
        approval_gate(
            adoption,
            packet,
            research=research,
            citation_number="SF-998877",
            packet_plan=PacketPlan(code_required=RequirementEvidence(quote, (page.sha256,))),
        )


def test_frozen_packet_gate_rechecks_required_item_inventory() -> None:
    from parkingbreaker.research.findings import Finding, FindingStatus
    from parkingbreaker.research.questions import FactKey, PacketPlan
    from samples import source
    from test_research_records import result_fixture

    research = result_fixture()
    quote = "You must include agency form A."
    page = source(quote)
    requirement = Finding(
        FactKey.MANDATORY_FORMS,
        ["agency form A"],
        FindingStatus.CONFIRMED,
        quote,
        (page.sha256,),
        jurisdiction=research.ticket.jurisdiction,
        stage=research.ticket.stage,
    )
    research = dataclasses.replace(
        research,
        sources=(*research.sources, page),
        findings=tuple(
            requirement if f.key is FactKey.MANDATORY_FORMS else f for f in research.findings
        ),
    )
    supplied = PacketPlan(fulfilled_items=frozenset({"agency form A"}))
    assert not research.resolve(packet_plan=supplied).ready
    text = letter_text()
    packet = build_packet(text, CITATION, EVIDENCE, AT)
    adoption = adopt(text, "cust_1", fixed_clock(AT))
    with pytest.raises(ApprovalRefused, match="MANDATORY_FORMS"):
        approval_gate(adoption, packet, research=research, packet_plan=supplied)
    with pytest.raises(ApprovalRefused, match="MANDATORY_FORMS"):
        approval_gate(adoption, packet, research=research, packet_plan=PacketPlan())


def enclosed_letter(photos: int) -> str:
    text = letter_text()
    from parkingbreaker.appeal.letter import enclosure_line

    line = enclosure_line(True, photos)
    assert line is not None
    return text + "\n\n" + line


def test_enclosure_list_matches_the_mailed_pages() -> None:
    packet = build_packet(enclosed_letter(len(EVIDENCE)), CITATION, EVIDENCE, AT)
    assert (
        "Enclosures: Copy of citation; 2 evidence photos"
        in reader(packet.pdf).pages[0].extract_text()
    )


@pytest.mark.parametrize("photos", [0, 1, 3])
def test_enclosure_list_that_disagrees_with_the_pages_is_refused(photos: int) -> None:
    with pytest.raises(PacketError, match="enclosure list"):
        build_packet(enclosed_letter(photos), CITATION, EVIDENCE, AT)


def test_every_photo_page_carries_the_citation_number_and_still_passes_the_gate() -> None:
    text = letter_text()
    packet = build_packet(text, CITATION, EVIDENCE, AT, citation_number="SF-998877")
    for page in reader(packet.pdf).pages[1:]:
        lines = page.extract_text().splitlines()
        assert lines[1] == "Uploaded by customer"
        assert "Citation SF-998877" in lines
    assert _customer_photo_page(reader(packet.pdf).pages[1])


def test_two_page_letter_identifiers_are_deterministic_and_preserve_exact_attachment() -> None:
    from test_research_records import result_fixture

    text = letter_text(sentences=28)
    packet = build_packet(text, CITATION, EVIDENCE, AT, citation_number="SF-998877")
    assert packet == build_packet(text, CITATION, EVIDENCE, AT, citation_number="SF-998877")
    pdf = reader(packet.pdf)
    assert packet.page_count == 5
    assert pdf.attachments[ATTACHMENT_NAME] == [text.encode("utf-8")]
    for page in pdf.pages:
        assert page.extract_text().splitlines().count("Citation SF-998877") == 1
    assert (
        approval_gate(
            adopt(text, "customer", fixed_clock(AT)),
            packet,
            research=result_fixture(),
            citation_number="SF-998877",
        )
        == CustomerApproved()
    )


@pytest.mark.parametrize("replacement", ["", "Citation OTHER-000"])
def test_changed_continuation_identifier_is_refused(replacement: str) -> None:
    from test_research_records import result_fixture

    text = letter_text(sentences=28)
    packet = build_packet(text, CITATION, [], AT, citation_number="SF-998877")
    writer = PdfWriter(clone_from=io.BytesIO(packet.pdf))
    page = writer.pages[1]
    stream = ContentStream(page["/Contents"], writer)
    changed = 0
    for operands, operator in stream.operations:
        if operator == b"Tj" and operands[0] == "Citation SF-998877":
            operands[0] = TextStringObject(replacement)
            changed += 1
    assert changed == 1
    page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    data = output.getvalue()
    forged = dataclasses.replace(packet, pdf=data, sha256=hashlib.sha256(data).hexdigest())
    assert reader(forged.pdf).attachments[ATTACHMENT_NAME] == [text.encode("utf-8")]
    with pytest.raises(ApprovalRefused, match="visible letter"):
        approval_gate(
            adopt(text, "customer", fixed_clock(AT)),
            forged,
            research=result_fixture(),
            citation_number="SF-998877",
        )


@pytest.mark.parametrize("extra", ["My unadopted additional statement.", "Citation SF-998877"])
def test_added_body_text_cannot_be_ignored_as_a_citation_header(extra: str) -> None:
    from test_research_records import result_fixture

    text = letter_text(sentences=28)
    packet = build_packet(text, CITATION, [], AT, citation_number="SF-998877")
    overlay = FPDF(orientation="portrait", unit="pt", format="letter")
    overlay.set_creation_date(AT)
    overlay.add_page()
    overlay.set_font("Helvetica", size=11)
    overlay.text(72, 150, extra)
    writer = PdfWriter(clone_from=io.BytesIO(packet.pdf))
    writer.pages[1].merge_page(reader(bytes(overlay.output())).pages[0])
    output = io.BytesIO()
    writer.write(output)
    data = output.getvalue()
    forged = dataclasses.replace(packet, pdf=data, sha256=hashlib.sha256(data).hexdigest())
    assert reader(forged.pdf).attachments[ATTACHMENT_NAME] == [text.encode("utf-8")]
    with pytest.raises(ApprovalRefused, match="visible letter"):
        approval_gate(
            adopt(text, "customer", fixed_clock(AT)),
            forged,
            research=result_fixture(),
            citation_number="SF-998877",
        )


def test_identifier_header_does_not_shift_body_pagination_or_signature() -> None:
    from item2_helpers import customer_ink

    text = letter_text(sentences=28)
    legacy = build_packet(text, CITATION, [], AT, customer_signature=customer_ink())
    identified = build_packet(
        text, CITATION, [], AT, customer_signature=customer_ink(), citation_number="SF-998877"
    )
    assert legacy.page_count == identified.page_count == 3
    legacy_pdf, identified_pdf = reader(legacy.pdf), reader(identified.pdf)
    for old, new in zip(legacy_pdf.pages[:2], identified_pdf.pages[:2], strict=True):
        # The one known first header is added outside the adopted body layout.
        assert new.extract_text().splitlines()[0] == "Citation SF-998877"
        assert new.extract_text().splitlines()[1:] == old.extract_text().splitlines()
    old_annotations = legacy_pdf.pages[1]["/Annots"]
    new_annotations = identified_pdf.pages[1]["/Annots"]
    assert isinstance(old_annotations, ArrayObject)
    assert isinstance(new_annotations, ArrayObject)
    old_signature = old_annotations[0].get_object()
    new_signature = new_annotations[0].get_object()
    assert isinstance(old_signature, DictionaryObject)
    assert isinstance(new_signature, DictionaryObject)
    assert new_signature["/Rect"] == old_signature["/Rect"]


@pytest.mark.parametrize("citation", ["bad\x00identifier", "Nguyễn", " \n\t "])
def test_citation_identifier_must_be_printable(citation: str) -> None:
    with pytest.raises(PacketError, match="citation number"):
        build_packet(letter_text(), CITATION, [], AT, citation_number=citation)


def test_identifier_whitespace_normalizes_consistently_in_build_and_approval() -> None:
    from test_research_records import result_fixture

    text = letter_text()
    packet = build_packet(text, CITATION, [], AT, citation_number=" \n SF-998877\t ")
    for page in reader(packet.pdf).pages:
        assert "Citation SF-998877" in page.extract_text().splitlines()
    assert (
        approval_gate(
            adopt(text, "customer", fixed_clock(AT)),
            packet,
            research=result_fixture(),
            citation_number="SF-998877",
        )
        == CustomerApproved()
    )


def test_identifier_too_wide_to_print_fails_cleanly_in_build_and_approval() -> None:
    from test_research_records import result_fixture

    text = letter_text()
    identifier = "W" * 100
    with pytest.raises(PacketError, match="does not fit"):
        build_packet(text, CITATION, [], AT, citation_number=identifier)
    legacy = build_packet(text, CITATION, [], AT)
    with pytest.raises(ApprovalRefused, match="does not fit"):
        approval_gate(
            adopt(text, "customer", fixed_clock(AT)),
            legacy,
            research=result_fixture(),
            citation_number=identifier,
        )


@pytest.mark.parametrize("sentences", [3, 24, 25, 60])
@pytest.mark.parametrize("enclosed", [False, True])
def test_closing_ink_and_name_stay_together_without_overlapping_enclosures(
    sentences: int, enclosed: bool
) -> None:
    from item2_helpers import customer_ink
    from parkingbreaker.appeal.letter import enclosure_line
    from test_research_records import result_fixture

    text = letter_text(sentences=sentences)
    evidence = EVIDENCE if enclosed else []
    if enclosed:
        text += "\n\n" + str(enclosure_line(True, len(evidence)))
    packet = build_packet(
        text, CITATION, evidence, AT, customer_signature=customer_ink(), citation_number="SF-998877"
    )
    assert packet == build_packet(
        text, CITATION, evidence, AT, customer_signature=customer_ink(), citation_number="SF-998877"
    )
    pdf = reader(packet.pdf)
    assert pdf.attachments[ATTACHMENT_NAME] == [text.encode()]
    letter_pages = packet.page_count - 1 - len(evidence)
    assert 1 <= letter_pages <= 2
    closings: list[tuple[int, float]] = []
    names: list[tuple[int, float]] = []
    enclosures: list[tuple[int, float]] = []
    signatures: list[tuple[int, float, float]] = []
    for index, page in enumerate(pdf.pages[:letter_pages]):

        def visit(
            value: str,
            _cm: list[float],
            tm: list[float],
            _font: Any,
            _size: float,
            page_index: int = index,
        ) -> None:
            position = (page_index, 792 - tm[5])
            if value.strip() == "Sincerely,":
                closings.append(position)
            elif value.strip() == "Jane Doe":
                names.append(position)
            elif value.strip().startswith("Enclosures:"):
                enclosures.append(position)

        page.extract_text(visitor_text=visit)
        for ref in page.get("/Annots", []):
            widget = ref.get_object()
            if widget.get("/FT") == "/Sig":
                _, bottom, _, top = map(float, widget["/Rect"])
                signatures.append((index, 792 - top, 792 - bottom))
    assert len(closings) == len(signatures) == 1
    closing_page, closing_baseline = closings[0]
    name_page, name_baseline = names[-1]
    ink_page, ink_top, ink_bottom = signatures[0]
    assert closing_page == name_page == ink_page
    assert 72 <= ink_top < ink_bottom <= 720
    assert closing_baseline < ink_top < ink_bottom < name_baseline
    if enclosed:
        assert len(enclosures) == 1
        assert enclosures[0] > (ink_page, ink_bottom)
    assert (
        approval_gate(
            adopt(text, "customer", fixed_clock(AT)),
            packet,
            research=result_fixture(),
            citation_number="SF-998877",
        )
        == CustomerApproved()
    )


@pytest.mark.parametrize(
    "text",
    ["My exact words without a signature block.", letter_text() + "\n\nSincerely,\n\n\nJane Doe"],
)
def test_signature_is_never_placed_by_guessing_an_absent_or_ambiguous_block(text: str) -> None:
    from item2_helpers import customer_ink

    with pytest.raises(PacketError, match="unambiguous letter signature block"):
        build_packet(text, CITATION, [], AT, customer_signature=customer_ink())
    assert reader(build_packet(text, CITATION, [], AT).pdf).attachments[ATTACHMENT_NAME] == [
        text.encode()
    ]
