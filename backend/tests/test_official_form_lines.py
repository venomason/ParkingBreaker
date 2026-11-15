"""Captured official PDF regression; claims below are test inputs, not model qualification."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from parkingbreaker.research.fakes import FakeFetcher
from parkingbreaker.research.findings import Finding, FindingStatus, apply_quote_guard
from parkingbreaker.research.fulfilment import (
    SignatureRequirement,
    declaration_wording,
    required_declarations,
    requirement_fulfilled,
    signature_requirement,
)
from parkingbreaker.research.procedure import requirement_sentences
from parkingbreaker.research.questions import FactKey, PacketPlan
from parkingbreaker.research.sources import AuthorityClass, FetchResponse, SourceRecord, pdf_to_text
from parkingbreaker.research.support import value_supported_by_quote
from samples import source
from test_procedure_silence import PROCEDURE, complete_page_claim
from test_research_records import result_fixture
from test_research_sources import fetch

ROOT = next(
    p
    for p in Path(__file__).resolve().parents
    if (p / "build-kit/state/guard-freeze.json").is_file()
)
SHA256 = "08fbc461e49a55375e6d6304c168e3bfbacec91f04045b611cd0ebd89f50a258"
PDF = ROOT / "build-kit/evidence/bench-pages" / SHA256
URL = "https://www.sfmta.com/media/40967/download?inline"
DECLARATION = "I declare that the foregoing is true and correct:"
BLOCK = DECLARATION + "\nSIGNATURE DATE"


def official_form() -> SourceRecord:
    body = PDF.read_bytes()
    assert hashlib.sha256(body).hexdigest() == SHA256
    result = fetch(
        FakeFetcher({URL: FetchResponse(URL, 200, {"content-type": "application/pdf"}, body)}), URL
    )
    assert isinstance(result, SourceRecord)
    assert result.sha256 == SHA256
    assert result.text == pdf_to_text(body)
    return replace(result, authority_class=AuthorityClass.OFFICIAL_FORM)


def test_actual_pdf_keeps_complete_declaration_and_signature_fields() -> None:
    form = official_form()
    assert DECLARATION in requirement_sentences(FactKey.REQUIRED_DECLARATIONS, form.text)
    assert BLOCK in requirement_sentences(FactKey.SIGNATURE_REQUIREMENT, form.text)
    assert not requirement_sentences(FactKey.MANDATORY_FORMS, form.text)
    assert declaration_wording(BLOCK) == DECLARATION
    assert value_supported_by_quote(FactKey.SIGNATURE_REQUIREMENT, [BLOCK], BLOCK)


@pytest.mark.parametrize("key", [FactKey.REQUIRED_DECLARATIONS, FactKey.SIGNATURE_REQUIREMENT])
def test_actual_linked_form_vetoes_false_silence(key: FactKey) -> None:
    form = official_form()
    guarded = apply_quote_guard([complete_page_claim(key)], [source(PROCEDURE), form])
    # A model's page-only silence cannot erase a condition in the fetched official form.
    assert any(f.status is not FindingStatus.CONFIRMED or f.value for f in guarded)
    result = result_fixture()
    if key is FactKey.SIGNATURE_REQUIREMENT:
        assert (
            signature_requirement(guarded, [source(PROCEDURE), form], result.ticket)
            is not SignatureRequirement.NOT_REQUIRED
        )


def test_exact_official_wording_and_signature_survive_quote_rechecking() -> None:
    form = official_form()
    result = result_fixture()
    findings = tuple(
        Finding(
            key,
            [quote],
            FindingStatus.CONFIRMED,
            quote,
            (SHA256,),
            jurisdiction=result.ticket.jurisdiction,
            stage=result.ticket.stage,
        )
        for key, quote in (
            (FactKey.REQUIRED_DECLARATIONS, DECLARATION),
            (FactKey.SIGNATURE_REQUIREMENT, BLOCK),
        )
    )
    assert required_declarations(findings, [form], result.ticket) == (DECLARATION,)
    assert signature_requirement(findings, [form], result.ticket) is SignatureRequirement.REQUIRED


@pytest.mark.parametrize(
    "text",
    [
        "I declare that the foregoing is true and correct under penalty of perjury.",
        "Optional signature and date fields.",
        "I declare that the foregoing is true and correct: under penalty of perjury.",
        "An example: I declare that the foregoing is true and correct:",
    ],
)
def test_unrecognized_or_longer_wording_never_becomes_the_short_declaration(text: str) -> None:
    assert declaration_wording(text) is None
    assert not requirement_sentences(FactKey.REQUIRED_DECLARATIONS, text)
    assert not value_supported_by_quote(FactKey.SIGNATURE_REQUIREMENT, ["signature"], text)


def test_packet_enforces_exact_captured_declaration_and_real_signature() -> None:
    from item2_helpers import customer_ink
    from parkingbreaker.appeal.adoption import adopt
    from parkingbreaker.appeal.letter import render_letter
    from parkingbreaker.appeal.packet import (
        ApprovalRefused,
        PacketError,
        approval_gate,
        build_packet,
    )
    from parkingbreaker.research.fakes import fixed_clock
    from test_appeal_letter import INPUT
    from test_appeal_packet import AT, CITATION

    form = official_form()
    result = result_fixture()
    replacements = {
        key: Finding(
            key,
            [quote],
            FindingStatus.CONFIRMED,
            quote,
            (SHA256,),
            jurisdiction=result.ticket.jurisdiction,
            stage=result.ticket.stage,
        )
        for key, quote in (
            (FactKey.REQUIRED_DECLARATIONS, DECLARATION),
            (FactKey.SIGNATURE_REQUIREMENT, BLOCK),
        )
    }
    result = replace(
        result,
        sources=(*result.sources, form),
        findings=tuple(replacements.get(f.key, f) for f in result.findings),
    )
    letter = render_letter(replace(INPUT, research=result))
    assert DECLARATION in letter.text
    with pytest.raises(PacketError, match="required signature"):
        build_packet(letter.text, CITATION, [], AT, research=result, citation_number="SF-998877")
    signed = build_packet(
        letter.text,
        CITATION,
        [],
        AT,
        research=result,
        customer_signature=customer_ink(),
        citation_number="SF-998877",
    )
    assert requirement_fulfilled(
        FactKey.SIGNATURE_REQUIREMENT,
        BLOCK,
        BLOCK,
        PacketPlan(frozen_pdf=signed.pdf),
        sources=[form],
        ticket=result.ticket,
    )
    # This actual form also has conditional police-report instructions. Recognizing
    # its signature cannot silently waive those independent unresolved conditions.
    with pytest.raises(ApprovalRefused, match="TICKET_SPECIFIC_REQUIREMENTS"):
        approval_gate(
            adopt(letter.text, "customer", fixed_clock(AT)),
            signed,
            research=result,
            citation_number="SF-998877",
        )
