from __future__ import annotations

import io
from dataclasses import replace

import pytest
from fpdf import FPDF
from pypdf import PdfReader

from parkingbreaker.appeal.adoption import adopt
from parkingbreaker.appeal.packet import (
    ApprovalRefused,
    approval_gate,
    build_packet,
    frozen_letter_text,
)
from parkingbreaker.research.fakes import fixed_clock
from parkingbreaker.research.fulfilment import requirement_fulfilled
from parkingbreaker.research.letter_identifiers import (
    IdentifierKind,
    classify_identifier_item,
    plan_identifier_fulfilled,
)
from parkingbreaker.research.questions import FactKey, PacketPlan
from parkingbreaker.session_window import WindowStatus
from parkingbreaker.submit_gate import GateFailure, check_submit_gate
from test_appeal_packet import AT, CITATION
from test_research_fulfilment import with_requirement

HEADER = "License Plate: 445566 (CA)\n"
PLAN = PacketPlan(customer_fields=(("plate", "445566"), ("plate_state", "CA")))


@pytest.mark.parametrize(
    ("letter", "expected"),
    [
        (HEADER + "There was a typo in license plate 445566 (OR).", (True, False)),
        ("CA license plate 445566 (OR)", (True, False)),
        (HEADER + "My license plate 445566 (ZZ) was visible.", (True, False)),
        (HEADER + "OR LICENSE PLATE 445566 was observed.", (True, False)),
        (HEADER + "TYPO IN LICENSE PLATE 445566 ON THE TICKET.", (True, True)),
        (HEADER + "MY LICENSE PLATE 445566 WAS VISIBLE.", (True, True)),
        (
            HEADER + "My license plate 445566 WAS (as shown in my photo) visible.",
            (True, True),
        ),
        (
            HEADER + "My license plate is 998877 (OR) in San Diego, CA 92112.",
            (False, False),
        ),
        (
            "In San Diego, CA 92112, my license plate 445566 (CA) was visible.",
            (True, True),
        ),
        (HEADER + "My license plate\n998877 (OR) was visible.", (False, False)),
        (
            "My license plate\nRegistration State: CA\n445566 items were reviewed.",
            (False, True),
        ),
        (
            HEADER + "The phrase says Uploaded by customer.\nMy license plate is 998877 (OR).",
            (False, False),
        ),
        (HEADER + "That was my OR license plate 445566.", (True, False)),
        (HEADER + "My plate was 998877 (OR).", (False, False)),
        ("My license plate reads 445566, registered in California.", (True, True)),
        ("License Plate: 445566, registered in California.", (True, True)),
        (HEADER + "My license plate\n\n999999 (OR).", (False, False)),
        (HEADER + "My license plate 445566 (or).", (True, False)),
        ("My license plate 445566 (ca).", (True, True)),
        ("My license plate 445566 (photo).", (True, False)),
        ("My license plate\n\n445566 items were reviewed.", (False, False)),
        (HEADER + "License Plate: 445566\n\n(or)", (True, False)),
        ("San Diego, CA 92112", (False, False)),
    ],
)
def test_visible_plate_and_state(letter: str, expected: tuple[bool, bool]) -> None:
    pdf = FPDF(unit="pt", format="letter")
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for line in letter.splitlines():
        pdf.cell(0, 15, text=line, new_x="LMARGIN", new_y="NEXT")
    pages = PdfReader(io.BytesIO(bytes(pdf.output()))).pages
    letter_texts = tuple(page.extract_text() for page in pages)

    actual = tuple(
        plan_identifier_fulfilled(kind, PLAN, letter_page_texts=letter_texts)
        for kind in (IdentifierKind.PLATE_NUMBER, IdentifierKind.PLATE_STATE)
    )
    assert actual == expected


@pytest.mark.parametrize("key", [FactKey.TICKET_SPECIFIC_REQUIREMENTS, FactKey.REQUIRED_ITEMS])
@pytest.mark.parametrize(
    ("separator", "observed", "allowed"),
    [
        ("\n", "445566 (CA)", True),
        ("\n", "445566 (OR)", False),
        ("\n\n", "999999 (ca)", False),
        ("\n\n", "445566 (or)", False),
        ("\n\n", "445566 (ca)", True),
    ],
)
def test_printed_identifier_rule_survives_both_gates(
    key: FactKey, separator: str, observed: str, allowed: bool
) -> None:
    rule = "Include your license plate number and registration state."
    research = replace(with_requirement(key, rule, rule), packet_plan=PLAN)
    assert research.resolve().ready
    assert not research.resolve(packet_plan=PacketPlan()).ready
    # The renderer breaks after the label and puts its own citation header on page two.
    text = (
        HEADER
        + "Context.\n" * 44
        + f"My license plate{separator}{observed} was recorded.\nSigned, Driver"
    )
    packet = build_packet(text, CITATION, [], AT, citation_number="ABC")
    pages = PdfReader(io.BytesIO(packet.pdf)).pages
    assert pages[0].extract_text().rstrip().endswith("My license plate")
    assert pages[1].extract_text().splitlines()[:2] == [
        "Citation ABC",
        f"{observed} was recorded.",
    ]
    plan = replace(PLAN, customer_fields=(*PLAN.customer_fields, ("citation_number", "ABC")))
    research = replace(research, packet_plan=plan)
    adoption = adopt(text, "customer", fixed_clock(AT))
    if allowed:
        approval_gate(adoption, packet, research=research, citation_number="ABC")
    else:
        with pytest.raises(ApprovalRefused, match="unresolved"):
            approval_gate(adoption, packet, research=research, citation_number="ABC")
    decision = check_submit_gate(
        auth_valid=True,
        window_status=WindowStatus.ACTIVE,
        adoption_hash=adoption.sha256,
        frozen_letter_hash=packet.letter_text_sha256,
        deadline_feasible_now=True,
        address_verified=True,
        research=research,
        frozen_pdf=packet.pdf,
    )
    assert decision.failures == (() if allowed else (GateFailure.RESEARCH_UNRESOLVED,))


def test_generated_citation_header_cannot_supply_vehicle_identifiers() -> None:
    reference = "license plate 445566 (CA)"
    body = "I contest the citation."
    packet = build_packet(body, CITATION, [], AT, citation_number=reference)
    assert frozen_letter_text(packet.pdf, citation_reference=f"Citation {reference}") == body
    plan = replace(
        PLAN,
        frozen_pdf=packet.pdf,
        customer_fields=(*PLAN.customer_fields, ("citation_number", reference)),
    )
    assert not requirement_fulfilled(
        FactKey.REQUIRED_ITEMS, "license plate number", "Include your license plate number.", plan
    )


def test_residence_and_enclosures_are_not_written_registration_state_requirements() -> None:
    assert (
        classify_identifier_item("state", "Include your state of residence and vehicle plate.")
        is None
    )
    assert (
        classify_identifier_item("state", "Provide your registration state.")
        is IdentifierKind.PLATE_STATE
    )
    assert classify_identifier_item("copy of the registration card") is None


@pytest.mark.parametrize(
    "quote",
    [
        "Attach a photograph of the license plate number.",
        "Provide the license plate number on the agency form.",
    ],
)
def test_printing_a_plate_cannot_fulfill_an_enclosure_requirement(quote: str) -> None:
    assert classify_identifier_item("license plate number", quote) is None
