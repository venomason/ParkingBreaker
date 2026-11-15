"""Case-specific forms, agency wording, signature artefacts and notice discrepancies."""

from __future__ import annotations

import hashlib
import io
from dataclasses import replace

import pytest
from pypdf import PdfReader, PdfWriter

from item2_helpers import (
    DECLARATION,
    DECLARATION_RULE,
    FORM_RULE,
    agency_form,
    customer_ink,
    form_case,
)
from parkingbreaker.appeal.adoption import adopt
from parkingbreaker.appeal.drafting import Draft, with_notice_sentence
from parkingbreaker.appeal.fakes import FakeDrafter
from parkingbreaker.appeal.letter import LetterError, enclosure_line, render_letter
from parkingbreaker.appeal.packet import ApprovalRefused, PacketError, approval_gate, build_packet
from parkingbreaker.appeal.proofread import proofread
from parkingbreaker.appeal.run import DraftingStatus, run_drafting
from parkingbreaker.research.fakes import fixed_clock
from parkingbreaker.research.findings import Finding, FindingStatus
from parkingbreaker.research.fulfilment import SignatureRequirement, signature_requirement
from parkingbreaker.research.questions import FactKey, PacketPlan
from parkingbreaker.research.resolution import (
    NoticeFact,
    TicketNoticeFacts,
    detect_notice_conflicts,
    resolution_event,
)
from parkingbreaker.research.sources import AuthorityClass
from samples import TEST_BASIS
from test_appeal_letter import INPUT
from test_appeal_packet import AT, CITATION, EVIDENCE, letter_text
from test_drafting_run import ACTIVE, CLEAN, FACTS, NOTES
from test_research_fulfilment import with_requirement
from test_research_records import result_fixture

CITATION_NUMBER = "SF-998877"


@pytest.mark.parametrize(
    "failure",
    ["issuer", "stage", "authority", "hash", "unknown_field", "missing_value", "wrong_value"],
)
def test_form_scope_and_all_non_narrative_fields_are_required(failure: str) -> None:
    research, plan, form = form_case(
        extra_field="Hearing date" if failure == "unknown_field" else None
    )
    if failure in {"issuer", "stage"}:
        ticket = (
            replace(research.ticket, jurisdiction="US-CA-Oakland")
            if failure == "issuer"
            else replace(research.ticket, stage="administrative hearing")
        )
        research = replace(research, ticket=ticket)
    elif failure in {"authority", "hash"}:
        page = research.sources[-1]
        page = replace(
            page,
            **(
                {"authority_class": AuthorityClass.SECONDARY}
                if failure == "authority"
                else {"sha256": "0" * 64}
            ),
        )
        research = replace(research, sources=(*research.sources[:-1], page))
    elif failure == "missing_value":
        plan = replace(plan, customer_fields=())
    elif failure == "wrong_value":
        plan = replace(plan, customer_fields=(("citation_number", "SF-112233"),))
    with pytest.raises(PacketError, match="field mapping"):
        build_packet(
            letter_text(),
            CITATION,
            [],
            AT,
            research=research,
            packet_plan=plan,
            completed_forms=[form],
        )
    # Bypassing the renderer still cannot pass the existing Tier 1/S3 gate.
    writer = PdfWriter(clone_from=io.BytesIO(build_packet(letter_text(), CITATION, [], AT).pdf))
    writer.append(PdfReader(io.BytesIO(form)))
    out = io.BytesIO()
    writer.write(out)
    resolution = research.resolve(packet_plan=replace(plan, frozen_pdf=out.getvalue()))
    assert not resolution.ready
    assert resolution_event(resolution) is not None


def test_no_blank_or_optional_form_is_attached_by_default() -> None:
    research, _, form = form_case()
    # Letter, citation copy and the customer's evidence pages; no form unless required.
    assert build_packet(letter_text(), CITATION, EVIDENCE, AT).page_count == 2 + len(EVIDENCE)
    with pytest.raises(PacketError, match="field mapping"):
        build_packet(
            letter_text(),
            CITATION,
            [],
            AT,
            research=research,
            completed_forms=[agency_form(filled=False)],
        )
    with pytest.raises(PacketError, match="field mapping"):
        build_packet(
            letter_text(), CITATION, [], AT, research=result_fixture(), completed_forms=[form]
        )


def test_usable_required_form_adds_exactly_one_page_and_survives_approval() -> None:
    research, _, form = form_case()
    text = (
        letter_text()
        + "\n\n"
        + str(
            enclosure_line(
                True, len(EVIDENCE), required_forms=("Parking Citation Initial Review Form",)
            )
        )
    )
    packet = build_packet(text, CITATION, EVIDENCE, AT, research=research, completed_forms=[form])
    assert packet.page_count == 2 + len(EVIDENCE) + 1
    approval_gate(adopt(text, "customer", fixed_clock(AT)), packet, research=research)
    assert FORM_RULE in next(
        f.verbatim_quote or "" for f in research.findings if f.key is FactKey.MANDATORY_FORMS
    )


def test_required_declaration_uses_only_quoted_agency_wording_inline() -> None:
    research = with_requirement(
        FactKey.REQUIRED_DECLARATIONS, "signed declaration", DECLARATION_RULE
    )
    letter = render_letter(replace(INPUT, research=research))
    assert letter.declarations_part == DECLARATION + "\nDate: " + INPUT.letter_date.isoformat()
    assert letter.text.count(DECLARATION) == 1
    assert "\\f" not in letter.text
    packet = build_packet(
        letter.text,
        CITATION,
        EVIDENCE,
        AT,
        research=research,
        customer_signature=customer_ink(),
        citation_number=CITATION_NUMBER,
    )
    assert packet.page_count == 2 + len(EVIDENCE)
    approval_gate(
        adopt(letter.text, "customer", fixed_clock(AT)),
        packet,
        research=research,
        citation_number=CITATION_NUMBER,
    )


@pytest.mark.parametrize("failure", ["missing_wording", "secondary", "wrong_stage", "unknown"])
def test_declaration_cannot_be_invented_from_a_name_or_unverified_rule(failure: str) -> None:
    research = with_requirement(
        FactKey.REQUIRED_DECLARATIONS, "signed declaration", DECLARATION_RULE
    )
    if failure == "missing_wording":
        research = with_requirement(
            FactKey.REQUIRED_DECLARATIONS, "signed declaration", "A signed declaration is required."
        )
        with pytest.raises(LetterError, match="wording"):
            render_letter(replace(INPUT, research=research))
        return
    if failure == "secondary":
        research = replace(
            research,
            sources=(
                *research.sources[:-1],
                replace(research.sources[-1], authority_class=AuthorityClass.SECONDARY),
            ),
        )
    else:
        research = replace(
            research,
            findings=tuple(
                replace(
                    f,
                    **(
                        {"stage": "administrative hearing"}
                        if failure == "wrong_stage"
                        else {"status": FindingStatus.UNKNOWN}
                    ),
                )
                if f.key is FactKey.REQUIRED_DECLARATIONS
                else f
                for f in research.findings
            ),
        )
    assert render_letter(replace(INPUT, research=research)).declarations_part is None


def test_default_or_customer_hardship_answer_never_creates_sworn_text() -> None:
    for research in (None, result_fixture()):
        text = render_letter(replace(INPUT, hardship_affirmed=True, research=research)).text.lower()
        assert all(term not in text for term in ("declaration", "penalty of perjury", "sworn"))
    with pytest.raises(PacketError, match="evidenced wording"):
        build_packet(
            letter_text() + "\nI declare under penalty of perjury that this is true.",
            CITATION,
            [],
            AT,
        )


def test_signature_requirement_is_three_state_and_typed_name_is_insufficient() -> None:
    base = result_fixture()
    assert (
        signature_requirement(base.findings, base.sources, base.ticket)
        is SignatureRequirement.NOT_REQUIRED
    )
    missing = tuple(f for f in base.findings if f.key is not FactKey.SIGNATURE_REQUIREMENT)
    assert signature_requirement(missing, base.sources, base.ticket) is SignatureRequirement.UNKNOWN
    research = with_requirement(
        FactKey.SIGNATURE_REQUIREMENT, "You must sign the letter.", "You must sign the letter."
    )
    assert (
        signature_requirement(research.findings, research.sources, research.ticket)
        is SignatureRequirement.REQUIRED
    )
    with pytest.raises(PacketError, match="captured customer signature"):
        build_packet(letter_text(), CITATION, [], AT, research=research)
    typed = build_packet(letter_text(), CITATION, [], AT, citation_number=CITATION_NUMBER)
    typed_resolution = research.resolve(
        packet_plan=PacketPlan(
            frozen_pdf=typed.pdf,
            customer_fields=(("citation_number", CITATION_NUMBER),),
        )
    )
    assert not typed_resolution.ready
    assert {key for item in typed_resolution.pending for key in item.keys} == {
        FactKey.SIGNATURE_REQUIREMENT
    }
    signed = build_packet(
        letter_text(),
        CITATION,
        [],
        AT,
        research=research,
        customer_signature=customer_ink(),
        citation_number=CITATION_NUMBER,
    )
    assert research.resolve(
        packet_plan=PacketPlan(
            frozen_pdf=signed.pdf,
            customer_fields=(("citation_number", CITATION_NUMBER),),
        )
    ).ready
    approval_gate(
        adopt(letter_text(), "customer", fixed_clock(AT)),
        signed,
        research=research,
        citation_number=CITATION_NUMBER,
    )
    unsigned = build_packet(
        letter_text(), CITATION, [], AT, research=base, citation_number=CITATION_NUMBER
    )
    approval_gate(
        adopt(letter_text(), "customer", fixed_clock(AT)),
        unsigned,
        research=base,
        citation_number=CITATION_NUMBER,
    )


def notice() -> TicketNoticeFacts:
    return TicketNoticeFacts(
        "Request initial review within 21 days of the issue date. "
        "Mail to PO Box 100, San Francisco, CA 94103.",
        confirmed=True,
        deadline_days=NoticeFact(21, "within 21 days"),
        deadline_trigger=NoticeFact("ISSUE_DATE", "of the issue date"),
        mailing_address=NoticeFact(
            "PO Box 100, San Francisco, CA 94103", "Mail to PO Box 100, San Francisco, CA 94103."
        ),
        stage=NoticeFact("first-level review", "Request initial review"),
    )


def test_notice_conflicts_retain_both_verbatim_quotes_without_changing_findings() -> None:
    n = notice()
    findings = (
        Finding(
            FactKey.DEADLINE_DAYS,
            30,
            FindingStatus.CONFIRMED,
            "Request review within 30 days.",
            ("a" * 64,),
        ),
        Finding(
            FactKey.DEADLINE_CLOCK_START,
            "NOTICE_DATE",
            FindingStatus.CONFIRMED,
            "The deadline runs from the notice date.",
            ("a" * 64,),
        ),
        Finding(
            FactKey.MAILING_ADDRESS,
            {"line1": "PO Box 200", "city": "San Francisco", "state": "CA", "zip_code": "94103"},
            FindingStatus.CONFIRMED,
            "Mail hearing requests to PO Box 200, San Francisco, CA 94103.",
            ("a" * 64,),
            stage="administrative hearing",
        ),
    )
    before = repr(findings)
    conflicts = detect_notice_conflicts(n, findings)
    assert {c.field for c in conflicts} == {
        "deadline_days",
        "deadline_trigger",
        "mailing_address",
        "stage",
    }
    assert all(c.printed_quote in n.instructions for c in conflicts)
    assert all(c.official_quote in {f.verbatim_quote for f in findings} for c in conflicts)
    assert repr(findings) == before
    assert detect_notice_conflicts(replace(n, confirmed=False), findings) == ()
    assert (
        detect_notice_conflicts(
            n, tuple(replace(f, status=FindingStatus.UNKNOWN) for f in findings)
        )
        == ()
    )


def test_notice_equivalent_addresses_stage_and_timing_do_not_conflict() -> None:
    n = notice()
    findings = (
        Finding(
            FactKey.DEADLINE_DAYS,
            21,
            FindingStatus.CONFIRMED,
            "Request review within 21 days.",
            ("a" * 64,),
            stage="initial review",
        ),
        Finding(
            FactKey.DEADLINE_CLOCK_START,
            "ISSUE_DATE",
            FindingStatus.CONFIRMED,
            "Within 21 days of the issue date.",
            ("a" * 64,),
        ),
        Finding(
            FactKey.MAILING_ADDRESS,
            {
                "recipient": "SFMTA Citation Review",
                "line1": "P.O. Box 100",
                "city": "San Francisco",
                "state": "CA",
                "zip_code": "94103",
            },
            FindingStatus.CONFIRMED,
            "Mail to P.O. Box 100, San Francisco, CA 94103.",
            ("a" * 64,),
        ),
    )
    assert detect_notice_conflicts(n, findings) == ()


def test_notice_sentence_is_code_inserted_opt_in_verbatim_and_proofread() -> None:
    facts = replace(FACTS, confirmed_notice=notice(), notice_sentence_opt_in=True)
    drafter = FakeDrafter(lambda *_: CLEAN)
    result = run_drafting(
        NOTES,
        (),
        facts,
        drafter,
        lambda: AT,
        AT,
        lambda: ACTIVE,
        new_revision=lambda: "notice",
        get_state_basis=lambda: {"CA": TEST_BASIS},
    )
    assert result.status is DraftingStatus.READY
    expected = "The instructions printed on my citation state: '" + notice().instructions + "'."
    assert sum(s.text == expected for s in result.spans) == 1
    inserted = with_notice_sentence(CLEAN, facts)
    assert proofread(inserted, NOTES, (), facts) == ()
    edited = replace(inserted.sentences[-1], text=expected.replace("21", "90"))
    assert any(p.kind == "notice text" for p in proofread(Draft((edited,)), NOTES, (), facts))
    assert any(
        p.kind == "notice text"
        for p in proofread(Draft((inserted.sentences[-1],) * 2), NOTES, (), facts)
    )
    assert with_notice_sentence(CLEAN, replace(facts, notice_sentence_opt_in=False)) == CLEAN
    assert (
        with_notice_sentence(
            CLEAN, replace(facts, confirmed_notice=replace(notice(), confirmed=False))
        )
        == CLEAN
    )


@pytest.mark.parametrize("term", ["due process", "void", "unconstitutional", "legally bound"])
def test_confirmed_notice_is_quoted_without_judging_its_words(term: str) -> None:
    facts = replace(
        FACTS, confirmed_notice=TicketNoticeFacts(term, confirmed=True), notice_sentence_opt_in=True
    )
    draft = with_notice_sentence(CLEAN, facts)
    assert proofread(draft, NOTES, (), facts) == ()


def test_freeze_rejects_added_exhibits_even_with_recomputed_packet_hash() -> None:
    research = result_fixture()
    text = letter_text()
    packet = build_packet(text, CITATION, [], AT)
    writer = PdfWriter(clone_from=io.BytesIO(packet.pdf))
    writer.append(PdfReader(io.BytesIO(agency_form(filled=True))))
    output = io.BytesIO()
    writer.write(output)
    forged = replace(
        packet,
        pdf=output.getvalue(),
        sha256=hashlib.sha256(output.getvalue()).hexdigest(),
        page_count=2,
    )
    with pytest.raises(ApprovalRefused, match="exhibit pages"):
        approval_gate(adopt(text, "customer", fixed_clock(AT)), forged, research=research)


def test_resolution_exposes_and_persists_guarded_notice_conflicts() -> None:
    from parkingbreaker.research_records import decode_resolution, resolution_json

    research = result_fixture()
    n = replace(
        notice(),
        instructions=notice().instructions.replace("21", "30"),
        deadline_days=NoticeFact(30, "within 30 days"),
    )
    resolution = research.resolve(ticket_notice_facts=n)
    assert any(c.field == "deadline_days" for c in resolution.notice_conflicts)
    assert decode_resolution(resolution_json(resolution)) == resolution
    assert resolution.ready == research.resolve().ready


def test_notice_text_stays_local_and_verbatim_through_letter_rendering() -> None:
    from parkingbreaker.appeal.drafting import build_draft_prompt

    n = replace(
        notice(),
        instructions=notice().instructions.replace("Request initial", "Request  initial"),
        stage=NoticeFact("initial review", "Request  initial review"),
    )
    facts = replace(FACTS, confirmed_notice=n, notice_sentence_opt_in=True)
    prompt = build_draft_prompt(NOTES, (), facts)
    assert n.instructions not in prompt.user
    letter = render_letter(replace(INPUT, draft=with_notice_sentence(CLEAN, facts)))
    assert "'" + n.instructions + "'." in letter.text


def test_customer_edit_cannot_bypass_notice_quotation_proofread() -> None:
    from parkingbreaker.appeal.correction import SpanAuthor, TextSpan, check_spans

    facts = replace(FACTS, confirmed_notice=notice(), notice_sentence_opt_in=True)
    sentence = with_notice_sentence(CLEAN, facts).sentences[-1]
    edited = TextSpan(
        "notice", sentence.text.replace("21", "90"), SpanAuthor.CUSTOMER, sentence.support
    )
    assert any(p.kind == "notice text" for p in check_spans((edited,), NOTES, (), facts))


def test_completed_form_cannot_smuggle_an_extra_exhibit_page() -> None:
    research, _, form = form_case()
    writer = PdfWriter(clone_from=io.BytesIO(form))
    writer.append(PdfReader(io.BytesIO(build_packet(letter_text(), CITATION, [], AT).pdf)))
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(PacketError, match="layout"):
        build_packet(
            letter_text(), CITATION, [], AT, research=research, completed_forms=[output.getvalue()]
        )
    with pytest.raises(PacketError, match="readable PDF"):
        build_packet(
            letter_text(), CITATION, [], AT, research=research, completed_forms=[b"invalid PDF"]
        )


def test_sfmta_true_and_correct_is_agency_wording_adopted_signed_and_dated() -> None:
    wording = "I declare that the foregoing is true and correct"
    research = with_requirement(
        FactKey.REQUIRED_DECLARATIONS, wording, wording + "\nSignature\nDate"
    )
    letter = render_letter(replace(INPUT, research=research))
    assert wording in letter.text
    assert "Date: " + INPUT.letter_date.isoformat() in letter.text
    packet = build_packet(
        letter.text,
        CITATION,
        [],
        AT,
        research=research,
        customer_signature=customer_ink(),
        citation_number=CITATION_NUMBER,
    )
    approval_gate(
        adopt(letter.text, "customer", fixed_clock(AT)),
        packet,
        research=research,
        citation_number=CITATION_NUMBER,
    )
    assert wording not in render_letter(INPUT).text


def test_sfmta_declaration_on_linked_form_is_never_treated_as_silence() -> None:
    from parkingbreaker.research.procedure import requirement_sentences

    wording = "I declare that the foregoing is true and correct"
    assert requirement_sentences(
        FactKey.REQUIRED_DECLARATIONS,
        "Use this form, or your own letter, to request administrative review of a citation(s).\n"
        + wording
        + "\nSignature\nDate",
    ) == (wording,)


def test_direct_declaration_cannot_be_truncated_into_a_different_statement() -> None:
    from parkingbreaker.research.fulfilment import declaration_wording

    assert (
        declaration_wording(
            "I declare that the foregoing is true and correct under penalty of perjury."
        )
        is None
    )
