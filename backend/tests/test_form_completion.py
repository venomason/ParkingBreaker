"""Official forms preserve exact customer inputs and fail closed on unknown mapping."""

from __future__ import annotations

import hashlib
import io
from dataclasses import replace

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, TextStringObject

from item2_helpers import FORM_TITLE, form_case
from parkingbreaker.research.fulfilment import (
    _completed_form,
    _widgets,
    complete_form,
    complete_required_forms,
)
from parkingbreaker.research.questions import FactKey, PacketPlan
from parkingbreaker.research.sources import AuthorityClass

STATEMENT = "The sign was covered.\nThe kiosk screen was blank.  I took two photos."


def statement_plan(plan: PacketPlan) -> PacketPlan:
    return replace(
        plan,
        customer_fields=(
            *(pair for pair in plan.customer_fields if pair[0] != "customer_statement"),
            ("customer_statement", STATEMENT),
        ),
    )


def test_official_blank_form_is_filled_deterministically_with_exact_original_statement() -> None:
    research, plan, _ = form_case()
    plan = replace(
        plan,
        customer_fields=(("citation_number", "SF-998877"), ("customer_statement", STATEMENT)),
    )
    completed = complete_required_forms(research.findings, research.sources, research.ticket, plan)
    assert completed is not None
    assert len(completed) == 1
    assert (
        complete_required_forms(research.findings, research.sources, research.ticket, plan)
        == completed
    )
    pdf = PdfReader(io.BytesIO(completed[0]))
    assert len(pdf.pages) == 1
    widgets = _widgets(pdf.pages[0])
    assert {str(w["/T"]): str(w["/V"]) for w in widgets} == {
        "Citation number": "SF-998877",
        "Explanation": STATEMENT,
    }
    visible = " ".join(pdf.pages[0].extract_text().split())
    assert " ".join(STATEMENT.split()) in visible
    assert "SF-998877" in visible
    assert (
        complete_form(plan.form_templates[0], FORM_TITLE, plan, research.sources, research.ticket)
        == completed[0]
    )
    # A usable preparation is not proof that a later frozen packet includes this page.
    prepared = research.reassess(packet_plan=plan).resolve()
    assert prepared.research_complete
    assert prepared.awaiting_packet
    assert not prepared.ready


@pytest.mark.parametrize(
    "wrong", ["", "The sign was covered.", "THE SIGN WAS COVERED.", "invented reason"]
)
def test_completed_form_rejects_changed_or_omitted_customer_narrative(wrong: str) -> None:
    research, plan, supplied = form_case()
    plan = statement_plan(plan)
    writer = PdfWriter(clone_from=io.BytesIO(supplied))
    widget = _widgets(writer.pages[0])[1]
    widget[NameObject("/V")] = TextStringObject(wrong)
    output = io.BytesIO()
    writer.write(output)
    assert not _completed_form(
        PdfReader(io.BytesIO(output.getvalue())),
        FORM_TITLE,
        plan,
        research.sources,
        research.ticket,
    )


@pytest.mark.parametrize(
    "failure", ["secondary", "digest", "issuer", "stage", "missing", "duplicate", "unknown"]
)
def test_form_completion_refuses_unverified_scope_or_unknown_customer_fields(failure: str) -> None:
    research, plan, _ = form_case(extra_field="Hearing date" if failure == "unknown" else None)
    plan = statement_plan(plan)
    if failure in {"secondary", "digest"}:
        original = research.sources[-1]
        source = replace(
            original,
            **(
                {"authority_class": AuthorityClass.SECONDARY}
                if failure == "secondary"
                else {"sha256": "0" * 64}
            ),
        )
        research = replace(research, sources=(*research.sources[:-1], source))
    elif failure == "issuer":
        research = replace(research, ticket=replace(research.ticket, jurisdiction="US-CA-Oakland"))
    elif failure == "stage":
        research = replace(
            research, ticket=replace(research.ticket, stage="administrative hearing")
        )
    elif failure == "missing":
        plan = replace(plan, customer_fields=(("citation_number", "SF-998877"),))
    elif failure == "duplicate":
        plan = replace(plan, customer_fields=(*plan.customer_fields, ("citation_number", "other")))
    assert (
        complete_form(plan.form_templates[0], FORM_TITLE, plan, research.sources, research.ticket)
        is None
    )


@pytest.mark.parametrize("field_type", ["/Btn", "/Sig", "/Ch"])
def test_unknown_controls_fail_closed(field_type: str) -> None:
    research, plan, _ = form_case()
    writer = PdfWriter(clone_from=io.BytesIO(plan.form_templates[0]))
    _widgets(writer.pages[0])[0][NameObject("/FT")] = NameObject(field_type)
    output = io.BytesIO()
    writer.write(output)
    template = output.getvalue()
    source = replace(research.sources[-1], sha256=hashlib.sha256(template).hexdigest())
    plan = replace(statement_plan(plan), form_templates=(template,))
    assert (
        complete_form(template, FORM_TITLE, plan, (*research.sources[:-1], source), research.ticket)
        is None
    )


def test_optional_official_form_is_never_added_to_the_packet_inventory() -> None:
    research, plan, _ = form_case()
    research = replace(
        research,
        findings=tuple(f for f in research.findings if f.key is not FactKey.MANDATORY_FORMS),
    )
    assert complete_required_forms(research.findings, research.sources, research.ticket, plan) == ()


@pytest.mark.parametrize(
    ("label", "key", "value"),
    [
        ("Plate", "plate", "SYN123"),
        ("License plate", "plate", "SYN123"),
        ("Name", "customer_name", "Synthetic Customer"),
        ("Phone", "customer_phone", "415-555-0100"),
        ("Email", "customer_email", "synthetic@example.test"),
        ("Address", "customer_address", "123 Example Street\nSan Francisco, CA 94103"),
        ("Issue date", "issue_date", "2026-10-01"),
        ("Violation code", "violation_code", "TC 7.2.22"),
        ("Reason for contest", "customer_statement", STATEMENT),
    ],
)
def test_each_closed_form_field_prints_the_saved_customer_value(
    label: str, key: str, value: str
) -> None:
    research, plan, _ = form_case(extra_field=label)
    known = dict(statement_plan(plan).customer_fields)
    known[key] = value
    plan = replace(plan, customer_fields=tuple(known.items()))
    result = complete_required_forms(research.findings, research.sources, research.ticket, plan)
    assert result is not None
    page = PdfReader(io.BytesIO(result[0])).pages[0]
    assert str(_widgets(page)[2]["/V"]) == value
    assert " ".join(value.split()) in " ".join(page.extract_text().split())


@pytest.mark.parametrize("location", ["catalog", "page", "widget", "other_annotation"])
def test_active_pdf_actions_are_not_imported_from_official_forms(location: str) -> None:
    from pypdf.generic import ArrayObject, DictionaryObject, NumberObject

    research, plan, _ = form_case()
    writer = PdfWriter(clone_from=io.BytesIO(plan.form_templates[0]))
    action = DictionaryObject(
        {
            NameObject("/S"): NameObject("/JavaScript"),
            NameObject("/JS"): TextStringObject("app.alert('synthetic')"),
        }
    )
    if location == "catalog":
        writer._root_object[NameObject("/OpenAction")] = action
    elif location == "page":
        writer.pages[0][NameObject("/AA")] = DictionaryObject({NameObject("/O"): action})
    elif location == "widget":
        _widgets(writer.pages[0])[0][NameObject("/AA")] = DictionaryObject(
            {NameObject("/E"): action}
        )
    else:
        link = DictionaryObject(
            {
                NameObject("/Subtype"): NameObject("/Link"),
                NameObject("/A"): action,
                NameObject("/Rect"): ArrayObject([NumberObject(v) for v in (0, 0, 10, 10)]),
            }
        )
        writer.pages[0][NameObject("/Annots")] = ArrayObject(
            [*_widgets(writer.pages[0]), writer._add_object(link)]
        )
    out = io.BytesIO()
    writer.write(out)
    template = out.getvalue()
    source = replace(research.sources[-1], sha256=hashlib.sha256(template).hexdigest())
    plan = replace(plan, form_templates=(template,))
    assert (
        complete_form(template, FORM_TITLE, plan, (*research.sources[:-1], source), research.ticket)
        is None
    )


def test_form_refuses_long_exact_statement_instead_of_silently_clipping() -> None:
    research, plan, _ = form_case()
    known = dict(plan.customer_fields)
    known["customer_statement"] = "The kiosk was blank.  I recorded every detail. " * 60
    plan = replace(plan, customer_fields=tuple(known.items()))
    assert (
        complete_required_forms(research.findings, research.sources, research.ticket, plan) is None
    )
