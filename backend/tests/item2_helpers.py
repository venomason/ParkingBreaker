"""Synthetic agency-style forms and customer ink; no sampled official page changes."""

from __future__ import annotations

import hashlib
import io
from dataclasses import replace

from fpdf import FPDF
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from parkingbreaker.research.questions import FactKey, PacketPlan
from parkingbreaker.research.run import ResearchResult
from parkingbreaker.research.sources import AuthorityClass
from samples import source
from test_appeal_packet import AT
from test_research_fulfilment import with_requirement

FORM_TITLE = "Parking Citation Initial Review Form"
FORM_RULE = "You must include the Parking Citation Initial Review Form."
DECLARATION = "I affirm that the information provided is correct."
DECLARATION_RULE = f'You must include the following signed declaration: "{DECLARATION}"'


def agency_form(*, filled: bool, extra_field: str | None = None) -> bytes:
    """One-page fillable initial-review form with its issuer and required fields."""
    fields = [("Citation number", "SF-998877"), ("Explanation", "The sign was covered by a tree.")]
    if extra_field:
        fields.append((extra_field, "October 11, 2026"))
    pdf = FPDF(format="letter")  # Lob prints only US Letter pages
    pdf.set_creation_date(AT)
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(
        0,
        8,
        FORM_TITLE + "\nSan Francisco Municipal Transportation Agency\n"
        "Parking Citation Initial Review\n" + "\n".join(k for k, _ in fields),
    )
    if filled:
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 8, "\n".join(v for _, v in fields))
    writer = PdfWriter(clone_from=io.BytesIO(bytes(pdf.output())))
    widgets = []
    for index, (name, value) in enumerate(fields):
        widget = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Widget"),
                NameObject("/FT"): NameObject("/Tx"),
                NameObject("/T"): TextStringObject(name),
                NameObject("/V"): TextStringObject(value if filled else ""),
                NameObject("/Ff"): NumberObject(2),
                NameObject("/Rect"): ArrayObject(
                    [NumberObject(v) for v in (30, 500 - index * 40, 300, 525 - index * 40)]
                ),
            }
        )
        widgets.append(writer._add_object(widget))
    writer.pages[0][NameObject("/Annots")] = ArrayObject(widgets)
    writer._root_object[NameObject("/AcroForm")] = writer._add_object(
        DictionaryObject({NameObject("/Fields"): ArrayObject(widgets)})
    )
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def form_case(*, extra_field: str | None = None) -> tuple[ResearchResult, PacketPlan, bytes]:
    research = with_requirement(FactKey.MANDATORY_FORMS, FORM_TITLE, FORM_RULE)
    template = agency_form(filled=False, extra_field=extra_field)
    text = PdfReader(io.BytesIO(template)).pages[0].extract_text()
    page = replace(
        source(text),
        sha256=hashlib.sha256(template).hexdigest(),
        content_type="application/pdf",
        authority_class=AuthorityClass.OFFICIAL_FORM,
    )
    plan = PacketPlan(
        form_templates=(template,),
        customer_fields=(
            ("citation_number", "SF-998877"),
            ("customer_statement", "The sign was covered by a tree."),
        ),
    )
    research = replace(research, sources=(*research.sources, page), packet_plan=plan)
    return research, plan, agency_form(filled=True, extra_field=extra_field)


def customer_ink() -> bytes:
    ink = Image.new("RGB", (140, 40), "white")
    ImageDraw.Draw(ink).line([(5, 35), (30, 5), (55, 30), (100, 10)], fill="black", width=3)
    data = io.BytesIO()
    ink.save(data, "PNG")
    return data.getvalue()
