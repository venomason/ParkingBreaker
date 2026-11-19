"""Procedure assessment and frozen-packet regressions using ordinary agency-style text."""

from __future__ import annotations

import io
import json
from dataclasses import replace

import pytest
from fpdf import FPDF
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject

from parkingbreaker.appeal.adoption import adopt
from parkingbreaker.appeal.packet import ApprovalRefused, approval_gate, build_packet
from parkingbreaker.case_state import CustomerApproved
from parkingbreaker.research.extractor import parse_findings
from parkingbreaker.research.fakes import fixed_clock
from parkingbreaker.research.findings import (
    EvidenceBasis,
    Finding,
    FindingStatus,
    apply_quote_guard,
    complete_findings,
    validate_tier1,
)
from parkingbreaker.research.procedure import SILENCE_KEYS, assessment_data
from parkingbreaker.research.questions import FactKey, PacketPlan
from parkingbreaker.research.resolution import ResearchAnswers, VehicleRelationship
from parkingbreaker.research.sources import AuthorityClass, html_to_text
from parkingbreaker.research_records import (
    answers_json,
    decode_answers,
    decode_research,
    research_json,
)
from samples import fixture, source
from test_appeal_packet import AT, CITATION, letter_text
from test_research_findings import SOURCES, full_findings, silence_finding
from test_research_records import result_fixture

K = FactKey
ST = FindingStatus
BASIS = EvidenceBasis.NONE_STATED_IN_OFFICIAL_PROCEDURE
CITATION_NUMBER = "SF-998877"

# A synthetic official-style page with actionable instructions, not negative assertions.
PROCEDURE = """San Francisco Parking Citation Initial Review
You may contest your parking citation by mail.
Submit your written explanation within 21 calendar days of the citation issue date.
Include your citation number and copies of supporting evidence for review.
Mail your initial review to SFMTA Customer Service Center, ATTN: Citation Review,
11 South Van Ness Avenue, San Francisco, CA 94103.
Contests sent by mail must be received by the SFMTA by the deadline."""


def complete_page_claim(key: FactKey = K.MANDATORY_FORMS) -> Finding:
    page = source(PROCEDURE)
    f = silence_finding(key, page)
    assert f.procedure_assessment is not None
    return replace(
        f,
        procedure_assessment=replace(
            f.procedure_assessment,
            how_quote="You may contest your parking citation by mail.",
            where_quote=(
                "Mail your initial review to SFMTA Customer Service Center, "
                "ATTN: Citation Review,\n11 South Van Ness Avenue, San Francisco, CA 94103."
            ),
        ),
    )


@pytest.mark.parametrize("key", sorted(SILENCE_KEYS))
def test_complete_official_procedure_without_negative_sentences(key: FactKey) -> None:
    claim = complete_page_claim(key)
    (guarded,) = apply_quote_guard([claim], [source(PROCEDURE)])
    assert guarded.status is ST.CONFIRMED
    assert guarded.value == []
    assert guarded.evidence_basis is BASIS
    assert guarded.procedure_assessment == claim.procedure_assessment
    assert not any(word in PROCEDURE.lower().split() for word in ("no", "not", "none"))
    findings = [claim if f.key is key else f for f in full_findings()]
    assert validate_tier1(findings, [*SOURCES, source(PROCEDURE)]).proceed


@pytest.mark.parametrize(
    "damage",
    [
        "payment",
        "faq",
        "fragment",
        "truncated",
        "bounded_fragment",
        "another_stage",
        "secondary",
        "failed_judgment",
        "missing_assessment",
        "forged_quote",
        "wrong_hash",
        "wrong_jurisdiction",
        "wrong_type",
        "no_deadline",
        "no_destination",
    ],
)
def test_procedure_guard_vetoes_incomplete_or_wrong_scope(damage: str) -> None:
    claim = complete_page_claim()
    page = source(PROCEDURE)
    a = claim.procedure_assessment
    assert a is not None
    if damage in {"payment", "faq"}:
        page = replace(page, final_url="https://www.sfmta.com/" + damage)
    elif damage in {"fragment", "truncated", "another_stage", "no_deadline", "no_destination"}:
        text = {
            "fragment": PROCEDURE.split("Mail your initial review")[0],
            "truncated": PROCEDURE + "\nContinued on the next page.",
            "another_stage": PROCEDURE.replace("Initial Review", "Administrative Hearing"),
            "no_deadline": PROCEDURE.replace(
                "Submit your written explanation within 21 calendar days "
                "of the citation issue date.",
                "Submit your written explanation.",
            ),
            "no_destination": PROCEDURE.split("Mail your initial review")[0],
        }[damage]
        page = source(text)
        a = replace(a, source_sha256=page.sha256, procedure_text=text, scope_quote=text)
        claim = replace(claim, source_sha256s=(page.sha256,), verbatim_quote=text)
    elif damage == "bounded_fragment":
        a = replace(a, procedure_text=PROCEDURE.split("Mail your initial review")[0])
    elif damage == "secondary":
        page = replace(page, authority_class=AuthorityClass.SECONDARY)
    elif damage == "failed_judgment":
        a = replace(a, complete=False)
    elif damage == "forged_quote":
        a = replace(a, how_quote="Send an appeal using your postal service.")
    elif damage == "wrong_hash":
        a = replace(a, source_sha256="0" * 64)
    elif damage == "wrong_jurisdiction":
        a = replace(a, jurisdiction="us-ca-oakland")
    elif damage == "wrong_type":
        a = replace(a, ticket_type="moving citation")
    claim = replace(claim, procedure_assessment=None if damage == "missing_assessment" else a)
    guarded = complete_findings(apply_quote_guard([claim], [page]))
    assert next(f for f in guarded if f.key is K.MANDATORY_FORMS).status is ST.UNKNOWN
    assert not validate_tier1([claim], [page]).proceed


@pytest.mark.parametrize("key", [K.REQUIRED_ITEMS, K.CODE_EXCEPTIONS, K.DEADLINE_ABSOLUTE])
def test_procedural_silence_is_not_an_inventory_or_deadline_waiver(key: FactKey) -> None:
    assert (
        apply_quote_guard([complete_page_claim(key)], [source(PROCEDURE)])[0].status is ST.UNKNOWN
    )


def completed_form_pdf(*, filled: bool) -> bytes:
    """A small fillable agency-style form with a citation and explanation field."""
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(
        0,
        8,
        "agency form A\nParking Citation Initial Review\nCitation number\nExplanation"
        + ("\nSF-998877\nThe sign was covered by a tree." if filled else ""),
    )
    writer = PdfWriter(clone_from=io.BytesIO(bytes(pdf.output())))
    widgets = []
    for index, (name, value) in enumerate(
        (
            ("Citation number", "SF-998877"),
            ("Explanation", "The sign was covered by a tree."),
        )
    ):
        widget = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Widget"),
                NameObject("/FT"): NameObject("/Tx"),
                NameObject("/T"): TextStringObject(name),
                NameObject("/V"): TextStringObject(value if filled else ""),
                NameObject("/Ff"): NumberObject(2),
                NameObject("/Rect"): ArrayObject(
                    [NumberObject(v) for v in (30, 650 - index * 40, 300, 675 - index * 40)]
                ),
            }
        )
        widgets.append(writer._add_object(widget))
    writer.pages[0][NameObject("/Annots")] = ArrayObject(widgets)
    writer._root_object[NameObject("/AcroForm")] = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Fields"): ArrayObject(widgets),
            }
        )
    )
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_explicit_form_overrides_silence_until_completed_form_is_in_frozen_packet() -> None:
    from item2_helpers import FORM_RULE, form_case

    research, plan, form_pdf = form_case()
    # Discover the rule without extracting a replacement for the original silence answer.
    base = result_fixture()
    research = replace(base, sources=research.sources, packet_plan=plan).reassess()
    form = next(f for f in research.findings if f.key is K.MANDATORY_FORMS)
    assert form.status is ST.CONFIRMED
    assert form.value == [FORM_RULE]
    assert form.evidence_basis is None
    assert any(f.evidence_basis is BASIS for f in form.evidence)
    assert not research.resolve().ready
    text = letter_text()
    adoption = adopt(text, "cust_1", fixed_clock(AT))
    packet = build_packet(text, CITATION, [], AT)
    with pytest.raises(ApprovalRefused, match="MANDATORY_FORMS"):
        approval_gate(adoption, packet, research=research)
    from parkingbreaker.appeal.letter import enclosure_line

    form_text = (
        text
        + "\n\n"
        + str(enclosure_line(True, 0, required_forms=("Parking Citation Initial Review Form",)))
    )
    adoption = adopt(form_text, "cust_1", fixed_clock(AT))
    filled = build_packet(
        form_text, CITATION, [], AT, completed_forms=[form_pdf], research=research
    )
    assert approval_gate(adoption, filled, research=research) == CustomerApproved()
    saved = decode_research(research_json(research))
    assert not saved.resolve().ready
    assert approval_gate(adoption, filled, research=saved) == CustomerApproved()


def test_new_evidence_rechecks_saved_assessment_after_record_round_trip() -> None:
    research = result_fixture()
    encoded = research_json(research)
    assert json.loads(encoded)["version"] == 5
    saved = decode_research(encoded)
    assert saved == research
    assert research_json(saved) == encoded
    page = source("Parking Citation Initial Review\nYou must include agency form A.")
    updated = replace(saved, sources=(*saved.sources, page)).reassess()
    assert not updated.decision.ok
    assert not updated.resolve().ready
    assert any(f.procedure_assessment is not None for f in updated.findings)


def test_missing_research_has_no_approval_path() -> None:
    text = letter_text()
    packet = build_packet(text, CITATION, [], AT)
    adoption = adopt(text, "cust_1", fixed_clock(AT))
    with pytest.raises(TypeError, match="research"):
        approval_gate(adoption, packet)  # type: ignore[call-arg]
    with pytest.raises(ApprovalRefused, match="research is required"):
        approval_gate(adoption, packet, research=None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("key", "quote", "good", "bad"),
    [
        (
            K.CONTEST_ELIGIBILITY,
            "Only the registered owner may contest.",
            ResearchAnswers(vehicle_relationship=VehicleRelationship.REGISTERED_OWNER),
            ResearchAnswers(vehicle_relationship=VehicleRelationship.DRIVER_NOT_OWNER),
        ),
        (
            K.PRIOR_STEP_REQUIREMENT,
            "You must first request an initial review.",
            ResearchAnswers(initial_review_completed=True),
            ResearchAnswers(initial_review_completed=False),
        ),
    ],
)
def test_standing_and_prior_steps_use_closed_customer_evidence(
    key: FactKey,
    quote: str,
    good: ResearchAnswers,
    bad: ResearchAnswers,
) -> None:
    research = result_fixture()
    page = source("Parking Citation Initial Review\n" + quote)
    requirement = Finding(
        key,
        [quote],
        ST.CONFIRMED,
        quote,
        (page.sha256,),
        jurisdiction=research.ticket.jurisdiction,
        stage=research.ticket.stage,
    )
    research = replace(
        research,
        sources=(*research.sources, page),
        findings=tuple(requirement if f.key is key else f for f in research.findings),
    )
    assert not research.resolve(bad).ready
    assert research.resolve(good).ready
    assert decode_answers(answers_json(good)) == good
    assert not research.resolve(packet_plan=PacketPlan(fulfilled_items=frozenset({quote}))).ready


def test_model_json_preserves_the_complete_procedure_judgment() -> None:
    claim = complete_page_claim()
    assert claim.procedure_assessment is not None
    raw = json.loads(
        json.dumps(
            {
                "findings": [
                    {
                        "key": claim.key,
                        "value": [],
                        "status": "CONFIRMED",
                        "verbatim_quote": claim.verbatim_quote,
                        "source_sha256s": list(claim.source_sha256s),
                        "evidence_basis": BASIS,
                        "procedure_assessment": assessment_data(claim.procedure_assessment),
                    }
                ]
            }
        )
    )
    assert parse_findings(raw) == (claim,)


def test_sfmta_inventory_keeps_citation_number_and_has_no_invented_six_lines() -> None:
    text = html_to_text(fixture("sfmta_contest.html"))
    assert "Include your citation number" in text
    assert "No " not in text
    f = next(f for f in full_findings() if f.key is K.REQUIRED_ITEMS)
    assert f.value == ["citation number"]


def test_explicit_disagreement_stays_conflicted_after_silence_is_overridden() -> None:
    research = result_fixture()
    positive = source("Parking Citation Initial Review\nYou must include agency form A.")
    absence = next(f for f in full_findings() if f.key is K.REQUIRED_DECLARATIONS)
    # Use the original page's actual explicit absence quote, opposed by a saved
    # declaration requirement. No invented negative assertion is added to any page.
    declaration = source("Parking Citation Initial Review\nA signed declaration is required.")
    explicit = Finding(
        K.REQUIRED_DECLARATIONS,
        ["signed declaration"],
        ST.CONFIRMED,
        "A signed declaration is required.",
        (declaration.sha256,),
    )
    from parkingbreaker.research.findings import consolidate

    merged = consolidate(
        apply_quote_guard(
            [complete_page_claim(K.REQUIRED_DECLARATIONS), absence, explicit],
            [*research.sources, source(PROCEDURE), positive, declaration],
        )
    )
    assert merged[0].status is ST.CONFLICTED
    assert all(f.verbatim_quote for f in merged[0].evidence)


def test_frozen_packet_plan_round_trips_actual_bytes() -> None:
    packet = build_packet(letter_text(), CITATION, [], AT, citation_number=CITATION_NUMBER)
    current = result_fixture().reassess(
        packet_plan=PacketPlan(
            frozen_pdf=packet.pdf,
            customer_fields=(("citation_number", CITATION_NUMBER),),
        )
    )
    assert current.decision.ok
    decoded = decode_research(research_json(current))
    assert decoded.packet_plan.frozen_pdf == packet.pdf
    assert decoded.resolve().ready


@pytest.mark.parametrize("source_stage", ["Administrative Hearing", ""])
@pytest.mark.parametrize("claimed_stage", [None, "first-level review"])
def test_extraction_never_fills_missing_stage_from_current_ticket(
    source_stage: str,
    claimed_stage: str | None,
) -> None:
    from parkingbreaker.research.fakes import FakeExtractor, FakeFetcher, html_response
    from parkingbreaker.research.run import run_research
    from test_research_run import CONTEST_SEED, CONTEST_URL, TEST_AGENCY, TICKET, TickingClock

    page = PROCEDURE.replace("Initial Review", source_stage).replace(
        "Mail your initial review", "Mail your written contest"
    )
    # The source supplies either a later-stage heading or no stage at all.
    transport = FakeFetcher({CONTEST_URL: html_response(CONTEST_URL, f"<main>{page}</main>")})
    extractor = FakeExtractor(
        lambda pages: [
            Finding(
                K.DEADLINE_DAYS,
                21,
                ST.CONFIRMED,
                "within 21 calendar days of the citation issue date",
                (pages[0].sha256,),
                jurisdiction=TEST_AGENCY.id if claimed_stage else None,
                stage=claimed_stage,
            )
        ]
    )
    result = run_research(
        TICKET,
        TEST_AGENCY,
        [CONTEST_SEED],
        transport,
        extractor,
        TickingClock(),
        wait=lambda _: None,
    )
    deadline = next(f for f in result.findings if f.key is K.DEADLINE_DAYS)
    assert not result.decision.ok
    assert any("DEADLINE_DAYS" in reason for reason in result.decision.reasons)
    if source_stage and claimed_stage:
        assert deadline.status is ST.UNKNOWN
    elif not claimed_stage:
        assert deadline.stage != "first-level review"


@pytest.mark.parametrize("status", [ST.NOT_APPLICABLE, ST.UNKNOWN, ST.CONFLICTED])
def test_new_basis_does_not_create_another_result_status(status: FindingStatus) -> None:
    claim = replace(complete_page_claim(), status=status)
    assert apply_quote_guard([claim], [source(PROCEDURE)])[0].status is ST.UNKNOWN


def test_old_record_gains_no_inferred_silence_from_complete_source() -> None:
    research = result_fixture()

    def old_finding(f: Finding) -> Finding:
        return (
            replace(
                f,
                value=None,
                status=ST.UNKNOWN,
                evidence_basis=None,
                procedure_assessment=None,
                evidence=(),
                scope_quote=None,
            )
            if f.evidence_basis is BASIS
            else replace(f, scope_quote=None)
        )

    old = replace(
        research,
        record_version=2,
        findings=tuple(old_finding(f) for f in research.findings),
        candidates=(),
    )
    saved = decode_research(research_json(old))
    current = saved.reassess()
    assert not current.resolve().ready
    assert all(f.evidence_basis is None for f in current.findings)
    assert all(
        f.status is ST.UNKNOWN
        for f in current.findings
        if f.key in SILENCE_KEYS - {K.REQUIRED_DECLARATIONS}
    )


def test_unclassified_compulsory_condition_vetoes_procedural_silence() -> None:
    research = result_fixture()
    page = source("Parking Citation Initial Review\nYou must supply a daytime telephone number.")
    updated = replace(research, sources=(*research.sources, page)).reassess()
    f = next(f for f in updated.findings if f.key is K.TICKET_SPECIFIC_REQUIREMENTS)
    assert f.status is ST.CONFIRMED
    assert f.value == ["You must supply a daytime telephone number."]
    assert not updated.resolve().ready


def test_repeated_reassessment_preserves_evidence_without_multiplying_it() -> None:
    research = result_fixture()
    page = source("Parking Citation Initial Review\nYou must include agency form A.")
    current = replace(research, sources=(*research.sources, page)).reassess()
    once = current.findings
    for _ in range(5):
        current = current.reassess()
        assert current.findings == once


@pytest.mark.parametrize("failed", ["judgment", "hash"])
def test_failed_assessment_round_trips_as_unknown(failed: str) -> None:
    research = result_fixture()
    claim = next(f for f in research.findings if f.key is K.MANDATORY_FORMS)
    a = claim.procedure_assessment
    assert a is not None
    a = replace(a, complete=False) if failed == "judgment" else replace(a, source_sha256="0" * 64)
    current = replace(
        research,
        findings=tuple(
            replace(f, procedure_assessment=a) if f.key is K.MANDATORY_FORMS else f
            for f in research.findings
        ),
    ).reassess()
    saved = decode_research(research_json(current))
    f = next(f for f in saved.findings if f.key is K.MANDATORY_FORMS)
    assert f.status is ST.UNKNOWN
    assert f.procedure_assessment == a
    assert not saved.resolve().ready
