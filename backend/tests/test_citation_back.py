"""Optional citation backs follow the existing scoped intake and packet path."""

from __future__ import annotations

import base64
import copy
import io
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from httpx2 import ASGITransport, AsyncClient
from pypdf import PdfReader

from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.appeal.adoption import adopt
from parkingbreaker.appeal.letter import enclosure_line, render_letter
from parkingbreaker.appeal.packet import ATTACHMENT_NAME, PacketError, approval_gate, build_packet
from parkingbreaker.case_state import CustomerApproved
from parkingbreaker.research.fakes import fixed_clock
from test_appeal_letter import INPUT
from test_appeal_packet import AT, CITATION, image, letter_text
from test_case_service import ALICE, BOB, INTAKE, service
from test_case_service import fake_resolution as fake_resolution
from test_research_records import result_fixture

BACK = image(color=(10, 200, 10))
EVIDENCE = [image(color=(200, 10, 10))]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_optional_back_fields_preserve_legacy_wire_json() -> None:
    old = s.SubmitIntake.model_validate(INTAKE)
    assert "citation_back_photo_ref" not in old.model_dump()
    old_form = s.IntakeForm(values={"story": "My exact words."})
    assert "ticket_back_photo_ref" not in old_form.model_dump()
    assert s.SubmitIntake.model_validate_json(old.model_dump_json()) == old
    ref = "photo_" + "a" * 64
    back = s.SubmitIntake.model_validate({**INTAKE, "citation_back_photo_ref": ref})
    assert back.model_dump()["citation_back_photo_ref"] == ref
    form = s.IntakeForm.model_validate({"ticket_back_photo_ref": ref})
    assert form.model_dump()["ticket_back_photo_ref"] == ref


def test_back_page_order_manifest_exact_attachment_and_approval() -> None:
    text = render_letter(
        replace(INPUT, encloses_citation=True, encloses_citation_back=True, evidence_photos=1)
    ).text
    enclosures = enclosure_line(True, 1, encloses_citation_back=True)
    assert enclosures is not None
    assert enclosures in text
    packet = build_packet(
        text, CITATION, EVIDENCE, AT, citation_back_image=BACK, citation_number="SF-998877"
    )
    assert packet == build_packet(
        text, CITATION, EVIDENCE, AT, citation_back_image=BACK, citation_number="SF-998877"
    )
    pdf = PdfReader(io.BytesIO(packet.pdf))
    assert packet.page_count == 4
    assert pdf.attachments[ATTACHMENT_NAME] == [text.encode()]
    assert "Copy of citation (front and back)" in pdf.pages[0].extract_text()
    for page, label, color in zip(
        pdf.pages[1:],
        ["Copy of citation", "Copy of citation (back)", "Evidence photo 1 of 1"],
        [(10, 10, 200), (10, 200, 10), (200, 10, 10)],
        strict=True,
    ):
        assert page.extract_text().splitlines()[:2] == [label, "Uploaded by customer"]
        assert "Citation SF-998877" in page.extract_text()
        raster = page.images[0].image
        assert raster is not None
        assert raster.getpixel((0, 0)) == color
    assert (
        approval_gate(
            adopt(text, "customer", fixed_clock(AT)),
            packet,
            research=result_fixture(),
            citation_number="SF-998877",
        )
        == CustomerApproved()
    )


@pytest.mark.parametrize("listed", [False, True])
def test_back_cannot_be_hidden_by_absent_or_front_only_enclosure_list(listed: bool) -> None:
    text = letter_text()
    if listed:
        text += "\n\n" + str(enclosure_line(True, 0))
    with pytest.raises(PacketError, match="enclosure list"):
        build_packet(text, CITATION, [], AT, citation_back_image=BACK)


def test_unreadable_back_is_refused_and_omitted_back_is_unchanged() -> None:
    text = letter_text() + "\n\n" + str(enclosure_line(True, 0, encloses_citation_back=True))
    with pytest.raises(PacketError, match="citation back"):
        build_packet(text, CITATION, [], AT, citation_back_image=b"not an image")
    legacy = letter_text()
    assert build_packet(legacy, CITATION, [], AT) == build_packet(
        legacy, CITATION, [], AT, citation_back_image=None
    )


@pytest.mark.anyio
async def test_back_ownership_autosave_removal_intake_retry_and_reload(tmp_path: Path) -> None:
    real = service(tmp_path / "back.sqlite")
    async with AsyncClient(
        transport=ASGITransport(app=create_app(real)), base_url="http://real.test"
    ) as client:
        workspace = (await client.post("/workspaces", headers=ALICE, json={})).json()[
            "workspace_id"
        ]
        other = (await client.post("/workspaces", headers=BOB, json={})).json()["workspace_id"]

        async def upload(ws: str, owner: dict[str, str], content: bytes) -> str:
            response = await client.post(
                f"/workspaces/{ws}/photos",
                headers=owner,
                json={
                    "content_type": "image/png",
                    "data_base64": base64.b64encode(content).decode(),
                },
            )
            assert response.status_code == 201
            return str(response.json()["photo_ref"])

        front = await upload(workspace, ALICE, CITATION)
        back = await upload(workspace, ALICE, BACK)
        foreign = await upload(other, BOB, image(color=(90, 80, 70)))
        form: dict[str, Any] = {
            "values": {},
            "ticket_photo_ref": front,
            "ticket_back_photo_ref": foreign,
        }
        form_url = f"/workspaces/{workspace}/intake-form"
        bad = await client.put(form_url, headers=ALICE, json={"expected_revision": 1, "form": form})
        assert bad.status_code == 409
        form["ticket_back_photo_ref"] = back
        saved = await client.put(
            form_url, headers=ALICE, json={"expected_revision": 1, "form": form}
        )
        assert saved.status_code == 200
        assert saved.json()["intake_form"]["ticket_back_photo_ref"] == back
        assert (await client.get(f"/workspaces/{workspace}", headers=BOB)).status_code == 404
        form["ticket_back_photo_ref"] = None
        removed = await client.put(
            form_url, headers=ALICE, json={"expected_revision": 2, "form": form}
        )
        assert removed.status_code == 200
        assert "ticket_back_photo_ref" not in removed.json()["intake_form"]
        body = copy.deepcopy(INTAKE)
        body.update(citation_photo_ref=front, citation_back_photo_ref=foreign, photo_refs=[])
        url = f"/workspaces/{workspace}/intake"
        assert (await client.post(url, headers=ALICE, json=body)).status_code == 409
        assert real._store.intake_for_workspace(workspace) is None
        body["citation_back_photo_ref"] = back
        first = await client.post(url, headers=ALICE, json=body)
        assert first.status_code == 200
        repeated = await client.post(url, headers=ALICE, json=body)
        assert repeated.json() == first.json()
        view = (await client.get(f"/workspaces/{workspace}", headers=ALICE)).json()
        assert view["intake"]["citation_back_photo_ref"] == back
        body["citation_back_photo_ref"] = None
        assert (await client.post(url, headers=ALICE, json=body)).status_code == 409
