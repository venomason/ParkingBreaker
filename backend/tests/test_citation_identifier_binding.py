"""The frozen citation identifier must bind the exact customer value, not any prose."""

from __future__ import annotations

import dataclasses
import hashlib
import io

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ContentStream, NameObject, TextStringObject

from parkingbreaker.appeal.adoption import adopt
from parkingbreaker.appeal.packet import ApprovalRefused, Packet, approval_gate, build_packet
from parkingbreaker.case_state import CustomerApproved
from parkingbreaker.research.fakes import fixed_clock
from parkingbreaker.research.fulfilment import requirement_fulfilled
from parkingbreaker.research.questions import FactKey, PacketPlan
from test_appeal_packet import AT, CITATION, letter_text
from test_research_records import result_fixture

ITEM = "citation number"
QUOTE = "Include your citation number."


def frozen_packet(identifier: str | None, text: str | None = None) -> Packet:
    return build_packet(
        letter_text() if text is None else text,
        CITATION,
        [],
        AT,
        citation_number=identifier,
    )


def plan_with(identifier: str | None) -> PacketPlan:
    fields = () if identifier is None else (("citation_number", identifier),)
    return PacketPlan(customer_fields=fields)


def satisfied(packet: Packet, identifier: str | None) -> bool:
    plan = PacketPlan(frozen_pdf=packet.pdf, customer_fields=plan_with(identifier).customer_fields)
    return requirement_fulfilled(FactKey.REQUIRED_ITEMS, ITEM, QUOTE, plan)


@pytest.mark.parametrize(
    "identifier",
    ["ABCXYZ", "AbC-12/34", "sf-98/76-ZZ", "SF-998877"],
)
def test_exact_identifier_binds_including_letter_only_and_mixed_punctuation(
    identifier: str,
) -> None:
    packet = frozen_packet(identifier)

    assert satisfied(packet, identifier)


def test_letter_only_identifier_is_not_refused_for_lacking_a_digit() -> None:
    packet = frozen_packet("ABCXYZ")

    assert satisfied(packet, "ABCXYZ")


def test_numeric_identifier_still_binds() -> None:
    packet = frozen_packet("SF-998877")

    assert satisfied(packet, "SF-998877")


def test_a_different_identifier_never_satisfies_the_requirement() -> None:
    packet = frozen_packet("SF-998877")

    assert not satisfied(packet, "ABCXYZ")
    assert not satisfied(packet, "SF-99887")
    assert not satisfied(packet, "SF-9988777")


def test_a_packet_header_without_an_expected_identifier_is_not_proof() -> None:
    packet = frozen_packet("ABCXYZ")

    assert not satisfied(packet, None)


def test_extra_numeric_prose_cannot_stand_in_for_the_expected_identifier() -> None:
    text = "I dispute this matter.\n\nReference: Citation 12345."
    packet = frozen_packet(None, text)

    assert not satisfied(packet, "ABCXYZ")
    assert not satisfied(packet, None)


def test_forged_legacy_shaped_body_line_is_never_an_authoritative_identifier() -> None:
    forged = "I dispute this charge.\nRe: Citation ABCXYZ, Plate 7ABC123\n"
    packet = frozen_packet(None, forged)
    assert any(
        "Re: Citation ABCXYZ, Plate 7ABC123" in (page.extract_text() or "")
        for page in PdfReader(io.BytesIO(packet.pdf)).pages
    )

    # The line has no structural provenance: an adopted body can contain it verbatim.
    for fields in ((), (("plate", "7ABC123"),)):
        plan = PacketPlan(frozen_pdf=packet.pdf, customer_fields=fields)
        assert not requirement_fulfilled(FactKey.REQUIRED_ITEMS, ITEM, QUOTE, plan)


def test_an_altered_actual_header_is_refused_by_the_approval_gate() -> None:
    text = letter_text()
    packet = frozen_packet("ABCXYZ", text)
    writer = PdfWriter(clone_from=io.BytesIO(packet.pdf))
    page = writer.pages[0]
    stream = ContentStream(page["/Contents"], writer)
    changed = 0
    for operands, operator in stream.operations:
        if operator == b"Tj" and operands[0] == "Citation ABCXYZ":
            operands[0] = TextStringObject("Citation OTHER1")
            changed += 1
    assert changed == 1
    page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    data = output.getvalue()
    forged = dataclasses.replace(packet, pdf=data, sha256=hashlib.sha256(data).hexdigest())

    with pytest.raises(ApprovalRefused, match="visible letter"):
        approval_gate(
            adopt(text, "customer", fixed_clock(AT)),
            forged,
            research=result_fixture(),
            citation_number="ABCXYZ",
        )


@pytest.mark.parametrize("identifier", ["ABCXYZ", "AbC-12/34", "SF-998877"])
def test_build_and_gate_bind_the_same_explicit_identifier(identifier: str) -> None:
    text = letter_text()

    packet = frozen_packet(identifier, text)

    assert (
        approval_gate(
            adopt(text, "customer", fixed_clock(AT)),
            packet,
            research=result_fixture(),
            citation_number=identifier,
        )
        == CustomerApproved()
    )


def test_gate_binds_identifier_into_a_partial_effective_plan() -> None:
    text = letter_text()
    packet = frozen_packet("ABCXYZ", text)
    plan = PacketPlan(customer_fields=(("plate", "7ABC123"),))

    assert (
        approval_gate(
            adopt(text, "customer", fixed_clock(AT)),
            packet,
            research=result_fixture(),
            packet_plan=plan,
            citation_number="ABCXYZ",
        )
        == CustomerApproved()
    )


def test_gate_refuses_a_contradictory_saved_identifier() -> None:
    text = letter_text()
    packet = frozen_packet("ABCXYZ", text)
    plan = PacketPlan(customer_fields=(("citation_number", "OTHER1"),))

    with pytest.raises(ApprovalRefused, match="contradict"):
        approval_gate(
            adopt(text, "customer", fixed_clock(AT)),
            packet,
            research=result_fixture(),
            packet_plan=plan,
            citation_number="ABCXYZ",
        )


def test_gate_refuses_duplicate_saved_customer_fields() -> None:
    text = letter_text()
    packet = frozen_packet("ABCXYZ", text)
    plan = PacketPlan(
        customer_fields=(("citation_number", "ABCXYZ"), ("citation_number", "ABCXYZ"))
    )

    with pytest.raises(ApprovalRefused, match="contradictory"):
        approval_gate(
            adopt(text, "customer", fixed_clock(AT)),
            packet,
            research=result_fixture(),
            packet_plan=plan,
            citation_number="ABCXYZ",
        )
