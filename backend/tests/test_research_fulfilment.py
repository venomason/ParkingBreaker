"""Synthetic unit policies and customer artifacts; no official-page fixture edits."""

from __future__ import annotations

import hashlib
import io
from dataclasses import replace

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    NameObject,
)

from parkingbreaker import case_state
from parkingbreaker.appeal.adoption import adopt
from parkingbreaker.appeal.packet import ApprovalRefused, approval_gate, build_packet
from parkingbreaker.research.fakes import fixed_clock
from parkingbreaker.research.findings import Finding, FindingStatus
from parkingbreaker.research.fulfilment import requirement_fulfilled
from parkingbreaker.research.ladder import Ask, CustomerQuestion
from parkingbreaker.research.questions import FactKey, PacketPlan
from parkingbreaker.research.resolution import (
    RecoveryProgress,
    ResearchAnswers,
    VehicleRelationship,
    answer_question,
    resolution_event,
)
from parkingbreaker.research.run import ResearchResult
from parkingbreaker.research_records import (
    decode_research,
    decode_resolution,
    research_json,
    resolution_json,
)
from samples import source
from test_appeal_packet import AT, CITATION, EVIDENCE, letter_text
from test_research_records import result_fixture


def with_requirement(key: FactKey, item: str, quote: str) -> ResearchResult:
    """The injected source is a synthetic test policy, never a sampled agency page."""
    result = result_fixture()
    page = source("Parking Citation Initial Review\n" + quote, url="https://policy.test/procedure")
    finding = Finding(
        key,
        [item],
        FindingStatus.CONFIRMED,
        quote,
        (page.sha256,),
        jurisdiction=result.ticket.jurisdiction,
        stage=result.ticket.stage,
    )
    return replace(
        result,
        progress=RecoveryProgress(True, True),
        sources=(*result.sources, page),
        findings=tuple(finding if f.key is key else f for f in result.findings),
    )


def test_verified_standing_asks_once_then_accepts_registered_owner() -> None:
    quote = "Only the registered owner may contest."
    research = with_requirement(FactKey.CONTEST_ELIGIBILITY, quote, quote)
    pending = research.resolve()
    assert pending.questions == (Ask(CustomerQuestion.VEHICLE_RELATIONSHIP),)
    assert resolution_event(pending) == case_state.CustomerNeeded("VEHICLE_RELATIONSHIP")
    answers = answer_question(
        ResearchAnswers(),
        CustomerQuestion.VEHICLE_RELATIONSHIP,
        VehicleRelationship.REGISTERED_OWNER,
    )
    fulfilled = research.resolve(answers)
    assert fulfilled.ready
    assert any(f.key is FactKey.CONTEST_ELIGIBILITY for f in fulfilled.fulfilled_requirements)
    assert decode_resolution(resolution_json(fulfilled)) == fulfilled
    assert isinstance(resolution_event(fulfilled), case_state.ResearchComplete)
    declined = answer_question(ResearchAnswers(), CustomerQuestion.VEHICLE_RELATIONSHIP, None)
    event = resolution_event(research.resolve(declined))
    assert isinstance(event, case_state.Stopped)
    assert event.reason is case_state.StopReason.NOT_YET_VERIFIED
    assert event.hard_stop is case_state.HardStop.S3
    assert not research.resolve(declined).questions


def test_unsupported_standing_rule_never_asks_relationship_or_claims_incompatibility() -> None:
    quote = "Only the registered owner may contest."
    research = with_requirement(FactKey.CONTEST_ELIGIBILITY, quote, quote)
    research = replace(research, sources=research.sources[:-1])
    resolution = research.resolve()
    assert not resolution.questions
    event = resolution_event(resolution)
    assert isinstance(event, case_state.Stopped)
    assert event.reason is case_state.StopReason.NOT_YET_VERIFIED
    assert event.hard_stop is case_state.HardStop.S3


@pytest.mark.parametrize("key", [FactKey.REQUIRED_ITEMS, FactKey.TICKET_SPECIFIC_REQUIREMENTS])
@pytest.mark.parametrize(
    "quote",
    [
        "Include a SASE.",
        "Include a self-addressed stamped envelope.",
        "Return the original ticket.",
    ],
)
def test_sase_or_original_is_process_incompatible_s3_with_saved_quote(
    key: FactKey,
    quote: str,
) -> None:
    research = with_requirement(key, quote, quote)
    # Typed assertions and a PDF image of a copy cannot supply a physical original or SASE.
    plan = PacketPlan(
        fulfilled_items=frozenset({quote}),
        frozen_pdf=build_packet(
            letter_text(),
            CITATION,
            [],
            AT,
            citation_number="SF-998877",
        ).pdf,
        customer_fields=(("citation_number", "SF-998877"),),
    )
    resolution = research.resolve(packet_plan=plan)
    assert not resolution.ready
    event = resolution_event(resolution)
    assert event == case_state.Stopped(
        case_state.StopReason.PROCESS_INCOMPATIBLE,
        quote,
        case_state.HardStop.S3,
    )
    assert decode_resolution(resolution_json(resolution)) == resolution
    transition = case_state.transition(case_state.CaseState.RESEARCHING, event)
    assert transition.target is case_state.CaseState.STOPPED_NO_CHARGE
    assert transition.effects == (case_state.Effect.RELEASE_HOLD,)


@pytest.mark.parametrize("failure", ["quote", "hash", "unknown", "conflict", "ambiguous"])
def test_unverified_or_ambiguous_requirement_never_becomes_process_incompatible(
    failure: str,
) -> None:
    quote = "You must include agency form A."
    key = FactKey.MANDATORY_FORMS
    research = with_requirement(key, "agency form A", quote)
    if failure == "hash":
        research = replace(research, sources=research.sources[:-1])
    elif failure == "ambiguous":
        research = with_requirement(
            FactKey.REQUIRED_ITEMS, "suitable material", "Include suitable material."
        )
    else:

        def corrupt(f: Finding) -> Finding:
            if f.key is not key:
                return f
            if failure == "quote":
                return replace(f, verbatim_quote="unsupported claim")
            return replace(
                f,
                status=FindingStatus.UNKNOWN if failure == "unknown" else FindingStatus.CONFLICTED,
            )

        research = replace(research, findings=tuple(corrupt(f) for f in research.findings))
    event = resolution_event(research.resolve())
    assert isinstance(event, case_state.Stopped)
    assert event.reason is case_state.StopReason.NOT_YET_VERIFIED
    assert event.hard_stop is (
        case_state.HardStop.S7 if failure == "conflict" else case_state.HardStop.S3
    )


def test_required_form_absent_blank_present_and_freeze_rechecks_inventory() -> None:
    from item2_helpers import FORM_RULE, agency_form, form_case
    from parkingbreaker.appeal.packet import PacketError

    research, plan, form = form_case()
    text = letter_text() + "\n\nEnclosures: Copy of citation; Parking Citation Initial Review Form"
    adoption = adopt(text, "customer", fixed_clock(AT))
    complete = build_packet(text, CITATION, [], AT, completed_forms=[form], research=research)
    writer = PdfWriter(clone_from=io.BytesIO(complete.pdf))
    writer.remove_page(len(writer.pages) - 1)
    output = io.BytesIO()
    writer.write(output)
    absent = replace(
        complete,
        pdf=output.getvalue(),
        sha256=hashlib.sha256(output.getvalue()).hexdigest(),
        page_count=complete.page_count - 1,
    )
    event = resolution_event(research.resolve(packet_plan=replace(plan, frozen_pdf=absent.pdf)))
    assert event == case_state.Stopped(
        case_state.StopReason.PROCESS_INCOMPATIBLE, FORM_RULE, case_state.HardStop.S3
    )
    with pytest.raises(PacketError, match="field mapping"):
        build_packet(
            text, CITATION, [], AT, completed_forms=[agency_form(filled=False)], research=research
        )
    research = research.reassess(packet_plan=replace(plan, frozen_pdf=complete.pdf))
    assert research.resolve().ready
    saved = decode_research(research_json(research))
    assert saved.packet_plan == research.packet_plan
    assert saved.resolve().ready
    assert approval_gate(adoption, complete, research=research) == case_state.CustomerApproved()
    with pytest.raises(ApprovalRefused, match="MANDATORY_FORMS"):
        approval_gate(adoption, absent, research=research)


@pytest.mark.parametrize("key", [FactKey.REQUIRED_DECLARATIONS, FactKey.SIGNATURE_REQUIREMENT])
def test_required_declaration_unsigned_or_typed_name_blocks_signed_artifact_passes(
    key: FactKey,
) -> None:
    from item2_helpers import DECLARATION, DECLARATION_RULE, customer_ink
    from parkingbreaker.appeal.packet import PacketError

    research = with_requirement(key, "signed declaration", DECLARATION_RULE)
    text = letter_text().replace("Sincerely,", DECLARATION + "\n\nSincerely,")
    # The declaration rule is inventoried separately even when its signature has a key.
    if key is FactKey.SIGNATURE_REQUIREMENT:
        declaration_research = with_requirement(
            FactKey.REQUIRED_DECLARATIONS, "signed declaration", DECLARATION_RULE
        )
        research = replace(
            research,
            findings=tuple(
                next(
                    f
                    for f in declaration_research.findings
                    if f.key is FactKey.REQUIRED_DECLARATIONS
                )
                if f.key is FactKey.REQUIRED_DECLARATIONS
                else f
                for f in research.findings
            ),
        )
    before_packet = research.resolve()
    assert resolution_event(before_packet) == case_state.ResearchComplete()
    assert not any(f.key is key for f in before_packet.fulfilled_requirements)
    with pytest.raises(PacketError, match="captured customer signature"):
        build_packet(text, CITATION, [], AT, research=research)
    unsigned = build_packet(
        text,
        CITATION,
        [],
        AT,
        research=research,
        customer_signature=customer_ink(),
        citation_number="SF-998877",
    )
    # Remove the captured artifact to model a typed-only or unsigned frozen letter.
    writer = PdfWriter(clone_from=io.BytesIO(unsigned.pdf))
    writer.pages[0][NameObject("/Annots")] = ArrayObject([])
    output = io.BytesIO()
    writer.write(output)
    unsigned = replace(unsigned, pdf=output.getvalue())
    event = resolution_event(
        research.resolve(
            packet_plan=PacketPlan(
                frozen_pdf=unsigned.pdf,
                customer_fields=(("citation_number", "SF-998877"),),
            )
        )
    )
    assert isinstance(event, case_state.Stopped)
    assert event.hard_stop is case_state.HardStop.S3
    signed = build_packet(
        text,
        CITATION,
        [],
        AT,
        research=research,
        customer_signature=customer_ink(),
        citation_number="SF-998877",
    )
    resolution = research.resolve(
        packet_plan=PacketPlan(
            frozen_pdf=signed.pdf,
            customer_fields=(("citation_number", "SF-998877"),),
        )
    )
    assert resolution.ready
    assert any(f.key is key for f in resolution.fulfilled_requirements)
    assert (
        approval_gate(
            adopt(text, "customer", fixed_clock(AT)),
            signed,
            research=research,
            citation_number="SF-998877",
        )
        == case_state.CustomerApproved()
    )


def test_required_citation_copy_is_fulfilled_by_the_mailed_copy() -> None:
    research = with_requirement(FactKey.REQUIRED_ITEMS, "citation copy", "Include a citation copy.")
    before_packet = research.resolve()
    assert resolution_event(before_packet) == case_state.ResearchComplete()
    assert before_packet.research_complete
    assert not before_packet.ready
    assert before_packet.awaiting_packet
    assert decode_resolution(resolution_json(before_packet)) == before_packet
    assert not any(
        finding.key is FactKey.REQUIRED_ITEMS and "citation copy" in finding.value
        for finding in before_packet.fulfilled_requirements
        if isinstance(finding.value, list)
    )
    packet = build_packet(letter_text(), CITATION, [], AT)
    assert packet.page_count == 2
    assert research.resolve(packet_plan=PacketPlan(frozen_pdf=packet.pdf)).ready


def test_verified_prior_step_uses_closed_completion_answer_and_retains_official_quote() -> None:
    quote = "You must first request an initial review."
    research = with_requirement(FactKey.PRIOR_STEP_REQUIREMENT, quote, quote)
    unknown = resolution_event(research.resolve())
    assert isinstance(unknown, case_state.Stopped)
    assert unknown.reason is case_state.StopReason.NOT_YET_VERIFIED
    incomplete = resolution_event(research.resolve(ResearchAnswers(initial_review_completed=False)))
    assert incomplete == case_state.Stopped(
        case_state.StopReason.PROCESS_INCOMPATIBLE, quote, case_state.HardStop.S3
    )
    assert research.resolve(ResearchAnswers(initial_review_completed=True)).ready


def test_verified_standing_restriction_with_wrong_relationship_is_quoted_s3_stop() -> None:
    quote = "Only the registered owner may contest."
    research = with_requirement(FactKey.CONTEST_ELIGIBILITY, quote, quote)
    event = resolution_event(
        research.resolve(
            ResearchAnswers(
                vehicle_relationship=VehicleRelationship.DRIVER_NOT_OWNER,
            )
        )
    )
    assert event == case_state.Stopped(
        case_state.StopReason.PROCESS_INCOMPATIBLE,
        quote,
        case_state.HardStop.S3,
    )


@pytest.mark.parametrize(
    "quote", ["Do not return the original ticket.", "An original ticket is optional."]
)
def test_negated_or_optional_item_is_not_proof_of_an_unfulfillable_requirement(quote: str) -> None:
    research = with_requirement(FactKey.REQUIRED_ITEMS, "original ticket", quote)
    event = resolution_event(research.resolve())
    assert isinstance(event, case_state.Stopped)
    assert event.reason is case_state.StopReason.NOT_YET_VERIFIED


def test_ticket_specific_copy_requirement_is_met_only_by_the_mailed_copy() -> None:
    quote = "Include a copy of citation."
    research = with_requirement(FactKey.TICKET_SPECIFIC_REQUIREMENTS, quote, quote)
    before_packet = research.resolve()
    assert resolution_event(before_packet) == case_state.ResearchComplete()
    assert not any(
        finding.key is FactKey.TICKET_SPECIFIC_REQUIREMENTS
        for finding in before_packet.fulfilled_requirements
    )
    packet = build_packet(letter_text(), CITATION, [], AT, citation_number="SF-998877")
    assert research.resolve(
        packet_plan=PacketPlan(
            frozen_pdf=packet.pdf,
            customer_fields=(("citation_number", "SF-998877"),),
        )
    ).ready


@pytest.mark.parametrize(
    ("item", "quote"),
    [
        (
            "copies of any documents that support your statement of fact",
            "Include copies of any documents that support your statement of fact.",
        ),
        (
            "copy of any evidence",
            "Include your citation number and a copy of any evidence you want reviewed.",
        ),
        ("photos", "You may include photos of the signs."),
    ],
)
def test_invited_supporting_documents_are_customer_optional(item: str, quote: str) -> None:
    assert requirement_fulfilled(FactKey.REQUIRED_ITEMS, item, quote, PacketPlan())


def test_unrelated_optional_sentence_does_not_waive_mandatory_photos() -> None:
    quote = (
        "Include copies of photos of the vehicle. "
        "Include any documents that support your statement."
    )
    item = "copies of photos of the vehicle"

    assert not requirement_fulfilled(FactKey.REQUIRED_ITEMS, item, quote, PacketPlan())


def test_mandatory_photos_wait_for_packet_and_require_uploaded_photo_pages() -> None:
    quote = (
        "Photos of the vehicle are required. "
        "Any documents that support your statement are optional."
    )
    item = "Photos of the vehicle"
    research = with_requirement(FactKey.REQUIRED_ITEMS, item, quote)

    before_packet = research.resolve()
    assert resolution_event(before_packet) == case_state.ResearchComplete()
    assert not any(
        finding.key is FactKey.REQUIRED_ITEMS and item in finding.value
        for finding in before_packet.fulfilled_requirements
        if isinstance(finding.value, list)
    )
    no_photo_packet = build_packet(letter_text(), CITATION, [], AT)
    assert not research.resolve(packet_plan=PacketPlan(frozen_pdf=no_photo_packet.pdf)).ready
    photo_packet = build_packet(letter_text(), CITATION, EVIDENCE, AT)
    assert research.resolve(packet_plan=PacketPlan(frozen_pdf=photo_packet.pdf)).ready


@pytest.mark.parametrize(
    ("item", "quote"),
    [
        ("copy of registration", "You must include a copy of your vehicle registration."),
        ("copy of the police report", "Include a complete police report with your protest."),
        ("any documents", "Include any documents listed on the notice."),
        ("copy of your ID", "Attach a copy of your ID and any documents you want reviewed."),
        (
            "stamped self-addressed envelope",
            "Enclose a stamped self-addressed envelope if you have supporting documents.",
        ),
        ("proof of ownership", "If you have any proof of ownership, you may include it."),
    ],
)
def test_required_documents_stay_required(item: str, quote: str) -> None:
    assert not requirement_fulfilled(FactKey.REQUIRED_ITEMS, item, quote, PacketPlan())


@pytest.mark.parametrize(
    ("quote", "reason"),
    [
        (
            "Return the contest form or letter, the original parking ticket "
            "and additional documents.",
            case_state.StopReason.PROCESS_INCOMPATIBLE,
        ),
        (
            "You must mail back the original ticket or provide a legible copy (front & back).",
            case_state.StopReason.NOT_YET_VERIFIED,
        ),
    ],
)
def test_original_parking_ticket_depends_on_the_official_copy_alternative(
    quote: str, reason: case_state.StopReason
) -> None:
    research = with_requirement(FactKey.REQUIRED_ITEMS, "original parking ticket", quote)
    event = resolution_event(research.resolve())
    assert isinstance(event, case_state.Stopped)
    assert event.reason is reason
