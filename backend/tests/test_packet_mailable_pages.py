"""Every mailed packet page must print on US Letter, within Lob's page limit.

Lob's Letters API: "All pages of a supplied PDF file must be sized at 8.5"x11"", and a
domestic letter holds at most 60 single-sided pages, one of which is the inserted address
page. A packet that breaks either rule would only be refused by Lob after the customer
approved it, so the builder and the approval gate refuse it first.
"""

from __future__ import annotations

import base64
import hashlib
import io
from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject, RectangleObject

from parkingbreaker import case_state
from parkingbreaker.appeal.adoption import adopt
from parkingbreaker.appeal.packet import (
    ApprovalRefused,
    PacketError,
    approval_gate,
    build_packet,
)
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.mail_pages import MAX_OWN_PAGES
from parkingbreaker.research.fakes import fixed_clock
from parkingbreaker.research.resolution import resolution_event
from parkingbreaker.research.run import ResearchResult
from test_appeal_packet import AT, CITATION, letter_text
from test_case_service import ALICE, BOB
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution
from test_effect_dispatch import WORKER, approved

ENCLOSURES = "\n\nEnclosures: Copy of citation; Parking Citation Initial Review Form"


@pytest.mark.parametrize("size", ["A4", "legal"])
def test_a_completed_form_that_is_not_us_letter_is_refused(
    size: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import item2_helpers
    from item2_helpers import form_case

    original = item2_helpers.FPDF  # type: ignore[attr-defined]
    monkeypatch.setattr(item2_helpers, "FPDF", lambda **_: original(format=size))
    research, _plan, form = form_case()
    box = PdfReader(io.BytesIO(form)).pages[0].mediabox
    assert (box.width, box.height) != (612, 792)
    # Research finds this before drafting: the form can never be mailed, so the case
    # stops (and its hold is released) instead of failing after the customer approves.
    event = resolution_event(research.resolve())
    assert isinstance(event, case_state.Stopped)
    with pytest.raises(PacketError, match="US Letter"):
        build_packet(
            letter_text() + ENCLOSURES, CITATION, [], AT, completed_forms=[form], research=research
        )


@pytest.mark.parametrize("change", ["legal", "crop", "rotate", "unit"])
def test_approval_refuses_a_frozen_page_that_would_not_print_as_letter(change: str) -> None:
    from item2_helpers import form_case

    research, _plan, form = form_case()
    text = letter_text() + ENCLOSURES
    packet = build_packet(text, CITATION, [], AT, completed_forms=[form], research=research)
    adoption = adopt(text, "customer", fixed_clock(AT))
    writer = PdfWriter(clone_from=io.BytesIO(packet.pdf))
    page = writer.pages[-1]
    if change == "legal":
        page.mediabox = RectangleObject((0, 0, 612, 1008))
    elif change == "crop":
        page.cropbox = RectangleObject((0, 0, 500, 700))
    elif change == "rotate":
        page[NameObject("/Rotate")] = NumberObject(90)
    else:
        page[NameObject("/UserUnit")] = NumberObject(2)
    output = io.BytesIO()
    writer.write(output)
    changed = replace(
        packet, pdf=output.getvalue(), sha256=hashlib.sha256(output.getvalue()).hexdigest()
    )
    with pytest.raises(ApprovalRefused, match="US Letter"):
        approval_gate(adoption, changed, research=research)


def test_a_packet_over_lobs_page_limit_is_refused() -> None:
    text = letter_text()
    packet = build_packet(text, CITATION, [], AT)
    writer = PdfWriter(clone_from=io.BytesIO(packet.pdf))
    while len(writer.pages) <= MAX_OWN_PAGES:
        writer.add_blank_page(612, 792)
    output = io.BytesIO()
    writer.write(output)
    long = replace(
        packet, pdf=output.getvalue(), sha256=hashlib.sha256(output.getvalue()).hexdigest()
    )
    with pytest.raises(ApprovalRefused, match=f"longer than {MAX_OWN_PAGES} pages"):
        approval_gate(adopt(text, "customer", fixed_clock(AT)), long, research=_research())


def _research() -> ResearchResult:
    from test_research_records import result_fixture

    return result_fixture()


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_the_customer_can_download_the_mailing_record_once_lob_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient(
        [
            LobResponse(
                200, "ltr_original", tracking_number="9400111", evidence=LetterEvidence.PRODUCTION
            )
        ]
    )
    api, real, case = await approved(tmp_path, monkeypatch, lob)
    try:
        early = await api.get(f"/cases/{case}/mailing-record", headers=ALICE)
        assert early.status_code == 409
        assert early.json()["detail"] == "mailing_record_not_ready"
        step = await api.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "dispatch"}
        )
        assert step.json()["state"] == "IN_TRANSIT"
        record = await api.get(f"/cases/{case}/mailing-record", headers=ALICE)
        assert record.status_code == 200
        body = record.json()
        pdf = base64.b64decode(body["pdf_base64"])
        token = real._intake(case).case_token
        assert token is not None
        prepared = real._store.get(token, 1)
        assert prepared is not None
        assert pdf == prepared.request.pdf  # exactly the packet Lob accepted
        assert body["pdf_sha256"] == hashlib.sha256(pdf).hexdigest()
        assert (body["letter_id"], body["tracking_number"]) == ("ltr_original", "9400111")
        stranger = await api.get(f"/cases/{case}/mailing-record", headers=BOB)
        assert stranger.status_code == 404
    finally:
        await api.aclose()
