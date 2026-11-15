"""Request-specific copy inventory and exact frozen page fidelity."""

from __future__ import annotations

import hashlib
import io
from dataclasses import replace

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from parkingbreaker.appeal.adoption import adopt
from parkingbreaker.appeal.letter import enclosure_line
from parkingbreaker.appeal.packet import ApprovalRefused, PacketError, approval_gate, build_packet
from parkingbreaker.appeal.required_copies import (
    RequiredCopy,
    RequiredCopyPages,
    required_copy_items,
    validated_copy_pages,
)
from parkingbreaker.research.fakes import fixed_clock
from parkingbreaker.research.findings import Finding, FindingStatus
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.run import ResearchResult
from parkingbreaker.research.sources import AuthorityClass
from samples import source
from test_appeal_packet import AT, CITATION, image, letter_text
from test_research_fulfilment import with_requirement


@pytest.mark.parametrize(
    ("key", "quote"),
    [
        (
            FactKey.TICKET_SPECIFIC_REQUIREMENTS,
            "You must include a notarized copy of registration.",
        ),
        (FactKey.TICKET_SPECIFIC_REQUIREMENTS, "The registration must be notarized."),
        (
            FactKey.TICKET_SPECIFIC_REQUIREMENTS,
            "You must include a certified copy of registration.",
        ),
        (
            FactKey.TICKET_SPECIFIC_REQUIREMENTS,
            "You must include a copy of registration with an original signature.",
        ),
        (
            FactKey.TICKET_SPECIFIC_REQUIREMENTS,
            "The registration copy must be accompanied by originals.",
        ),
        (FactKey.SIGNATURE_REQUIREMENT, "You must sign the copy of registration in original ink."),
        (
            FactKey.SIGNATURE_REQUIREMENT,
            "You must include a copy of registration with an original signature.",
        ),
    ],
)
def test_plain_copy_cannot_erase_stricter_rule_in_another_fact(key: FactKey, quote: str) -> None:
    research, _, selected, text = setup_copy()
    original = build_packet(text, CITATION, [], AT, research=research, required_copies=selected)
    page = source(
        "Parking Citation Initial Review\n" + quote, url="https://policy.test/strict-copy"
    )
    strict = Finding(
        key,
        [quote],
        FindingStatus.CONFIRMED,
        quote,
        (page.sha256,),
        jurisdiction=research.ticket.jurisdiction,
        stage=research.ticket.stage,
    )
    tightened = replace(
        research,
        sources=(*research.sources, page),
        findings=tuple(strict if finding.key is key else finding for finding in research.findings),
    )
    assert required_copy_items(tightened.findings, tightened.sources, tightened.ticket) == ()
    with pytest.raises(PacketError, match="unsupported"):
        build_packet(text, CITATION, [], AT, research=tightened, required_copies=selected)
    # A previously rendered plain-copy packet cannot satisfy the newly stricter rule,
    # with or without a caller-supplied selection or a new outer packet digest.
    for copies in (selected, ()):
        with pytest.raises(ApprovalRefused, match="unsupported"):
            approval_gate(
                adopt(text, "customer", fixed_clock(AT)),
                original,
                research=tightened,
                required_copies=copies,
            )
    unverified = replace(
        tightened,
        sources=(*tightened.sources[:-1], replace(page, authority_class=AuthorityClass.SECONDARY)),
    )
    assert required_copy_items(unverified.findings, unverified.sources, unverified.ticket)


def setup_copy() -> tuple[
    ResearchResult, tuple[RequiredCopy, ...], tuple[RequiredCopyPages, ...], str
]:
    research = with_requirement(
        FactKey.REQUIRED_ITEMS, "copy of registration", "You must include a copy of registration."
    )
    items = required_copy_items(research.findings, research.sources, research.ticket)
    assert len(items) == 1
    pages = (RequiredCopyPages(items[0].requirement_id, (image(), image(color=(1, 100, 60)))),)
    enclosures = enclosure_line(True, 0, required_copies=(items[0].title,))
    assert enclosures is not None
    text = letter_text() + "\n\n" + enclosures
    return research, items, pages, text


def test_multiple_pages_are_exactly_approved_and_missing_inventory_fails() -> None:
    research, items, pages, text = setup_copy()
    packet = build_packet(text, CITATION, [], AT, research=research, required_copies=pages)
    actual = PdfReader(io.BytesIO(packet.pdf)).pages
    assert len(actual) == 4
    assert "Page 1 of 2" in actual[2].extract_text()
    assert "Page 2 of 2" in actual[3].extract_text()
    approval_gate(
        adopt(text, "customer", fixed_clock(AT)), packet, research=research, required_copies=pages
    )
    with pytest.raises(PacketError):
        build_packet(text, CITATION, [], AT, research=research)
    for invalid in [
        (),
        (*pages, *pages),
        (RequiredCopyPages("unknown", pages[0].pages),),
        (RequiredCopyPages(items[0].requirement_id, ()),),
    ]:
        with pytest.raises(ValueError, match="required cop"):
            validated_copy_pages(items, invalid)


@pytest.mark.parametrize(
    ("item", "quote"),
    [
        ("copy of original registration", "You must include a copy of original registration."),
        ("copy of authorization form", "You must include a copy of authorization form."),
        ("copy of registration", "If you are the owner, you must include a copy of registration."),
        ("copy of registration", "You must include a certified copy of registration."),
        ("copy of citation", "You must include a copy of citation."),
    ],
)
def test_physical_conditional_and_other_packet_roles_are_not_collectible(
    item: str, quote: str
) -> None:
    research = with_requirement(FactKey.REQUIRED_ITEMS, item, quote)
    assert required_copy_items(research.findings, research.sources, research.ticket) == ()


@pytest.mark.parametrize("tamper", ["stream", "swap", "geometry", "wrong_selection"])
def test_changed_copy_cannot_pass_with_recomputed_packet_hash(tamper: str) -> None:
    research, _, pages, text = setup_copy()
    packet = build_packet(text, CITATION, [], AT, research=research, required_copies=pages)
    reader = PdfReader(io.BytesIO(packet.pdf))
    writer = PdfWriter(clone_from=reader)
    if tamper == "stream":
        stream = DecodedStreamObject()
        contents = writer.pages[2].get_contents()
        assert contents is not None
        stream.set_data(contents.get_data() + b"\n0 0 10 10 re f\n")
        writer.pages[2][NameObject("/Contents")] = stream
    elif tamper == "swap":
        first, second = writer.pages[2], writer.pages[3]
        writer.remove_page(3)
        writer.remove_page(2)
        writer.add_page(second)
        writer.add_page(first)
    elif tamper == "geometry":
        writer.pages[2].mediabox.upper_right = (200, 200)
    else:
        pages = (replace(pages[0], pages=(image(color=(0, 0, 0)), pages[0].pages[1])),)
    output = io.BytesIO()
    writer.write(output)
    data = output.getvalue()
    changed = replace(packet, pdf=data, sha256=hashlib.sha256(data).hexdigest())
    with pytest.raises(ApprovalRefused):
        approval_gate(
            adopt(text, "customer", fixed_clock(AT)),
            changed,
            research=research,
            required_copies=pages,
        )


def test_unverified_and_wrong_scope_requirements_do_not_create_inventory() -> None:
    research, _, _, _ = setup_copy()
    assert required_copy_items(research.findings, (), research.ticket) == ()
    assert (
        required_copy_items(
            research.findings, research.sources, replace(research.ticket, jurisdiction="Other")
        )
        == ()
    )


def test_distinct_copies_follow_inventory_order_and_enclosure_order() -> None:
    research = with_requirement(
        FactKey.REQUIRED_ITEMS,
        "copy of authorization",
        "A copy of authorization and a copy of registration are required.",
    )
    research = replace(
        research,
        findings=tuple(
            replace(finding, value=["copy of registration", "copy of authorization"])
            if finding.key is FactKey.REQUIRED_ITEMS
            else finding
            for finding in research.findings
        ),
    )
    items = required_copy_items(research.findings, research.sources, research.ticket)
    assert [item.title for item in items] == ["copy of authorization", "copy of registration"]
    selected = tuple(
        RequiredCopyPages(item.requirement_id, (image(color=(index, 20, 30)),))
        for index, item in enumerate(reversed(items))
    )
    enclosures = enclosure_line(True, 0, required_copies=tuple(item.title for item in items))
    assert enclosures is not None
    text = letter_text() + "\n\n" + enclosures
    packet = build_packet(text, CITATION, [], AT, research=research, required_copies=selected)
    pages = PdfReader(io.BytesIO(packet.pdf)).pages
    assert [page.extract_text().splitlines()[0] for page in pages[2:]] == [
        item.title for item in items
    ]
    # The existing research classifier retains a separate unclassified-condition gap.
    # Collection must not erase that research stop merely because both copies exist.
    with pytest.raises(ApprovalRefused, match="unresolved Tier 1"):
        approval_gate(
            adopt(text, "customer", fixed_clock(AT)),
            packet,
            research=research,
            required_copies=selected,
        )


def test_page_limits_apply_per_document_and_to_the_whole_inventory() -> None:
    _, items, pages, _ = setup_copy()
    with pytest.raises(ValueError, match="page limit"):
        validated_copy_pages(items, (replace(pages[0], pages=(CITATION,) * 11),))
    inventory = tuple(replace(items[0], requirement_id=str(index)) for index in range(3))
    with pytest.raises(ValueError, match="page limit"):
        validated_copy_pages(
            inventory,
            tuple(RequiredCopyPages(item.requirement_id, (CITATION,) * 7) for item in inventory),
        )
