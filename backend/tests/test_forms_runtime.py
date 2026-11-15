"""Saved official forms flow through research, exact adoption, preview and freeze."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from pypdf import PdfReader

import test_case_service as fixtures
from item2_helpers import FORM_TITLE, form_case
from parkingbreaker import case_service
from parkingbreaker.appeal.drafting import Draft, DraftSentence
from parkingbreaker.appeal.fakes import FakeDrafter
from parkingbreaker.appeal.packet import ATTACHMENT_NAME
from parkingbreaker.case_state import CaseState
from parkingbreaker.research.fulfilment import _widgets
from parkingbreaker.research.questions import PacketPlan
from parkingbreaker.research.resolution import VehicleRelationship
from parkingbreaker.research.run import ResearchResult
from parkingbreaker.research_records import decode_research
from test_case_service import ALICE, WORKER
from test_case_service import fake_resolution as fake_resolution

STATEMENT = "The meter was blank.\nI photographed it."


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [None, "unmapped", "does_not_fit"])
async def test_real_form_research_to_inspected_frozen_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    intake = copy.deepcopy(fixtures.INTAKE)
    statement = "Customer words " * 150 if failure == "does_not_fit" else STATEMENT
    intake["customer_position"]["statement"] = statement
    monkeypatch.setattr(fixtures, "INTAKE", intake)
    research, template_plan, _ = form_case(
        extra_field="Hearing date" if failure == "unmapped" else None
    )
    research = replace(
        research,
        ticket=replace(
            research.ticket, issue_date=date.fromisoformat(intake["ticket"]["issue_date"])
        ),
    )
    client, real, case = await fixtures.researched_case(tmp_path, monkeypatch, research)
    plans: list[PacketPlan] = []

    def researched(*_args: object, **kwargs: object) -> ResearchResult:
        plan = kwargs["packet_plan"]
        assert isinstance(plan, PacketPlan)
        plans.append(plan)
        assert dict(plan.customer_fields)["customer_statement"] == statement
        assert dict(plan.customer_fields)["citation_number"] == intake["ticket"]["citation_number"]
        # Inject only the already fetched synthetic official template, not a prepared
        # packet or claim of fulfilment. Research must prove current fillability.
        return research.reassess(
            packet_plan=replace(template_plan, customer_fields=plan.customer_fields)
        )

    monkeypatch.setattr(case_service, "run_research", researched)
    object.__setattr__(
        real._deps,
        "drafter",
        FakeDrafter(lambda *_: Draft((DraftSentence(statement, (f"note:0-{len(statement)}",)),))),
    )
    try:
        response = await client.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
        )
        assert response.status_code == 200, response.text
        status = response.json()
        assert len(plans) == 1
        if failure is not None:
            assert status["state"] == "STOPPED_NO_CHARGE"
            assert status["stop"]["hard_stop"] == "S3"
            assert real._store.get_draft(case) is None
            return
        if status["state"] == "NEEDS_CUSTOMER":
            answered = await client.post(
                f"/cases/{case}/answers",
                headers=ALICE,
                json={
                    "question_id": "VEHICLE_RELATIONSHIP",
                    "answer": VehicleRelationship.REGISTERED_OWNER.value,
                },
            )
            assert answered.status_code == 200, answered.text
            status = answered.json()
        assert status["state"] == CaseState.DRAFTING.value
        saved_research = real._store.get_research(case)
        assert saved_research is not None
        restored = decode_research(saved_research.research_json)
        assert restored.packet_plan.frozen_pdf is None
        assert restored.packet_plan.form_templates == template_plan.form_templates
        draft_response = await client.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "draft"}
        )
        assert draft_response.status_code == 200, draft_response.text
        draft = (await client.get(f"/cases/{case}/draft", headers=ALICE)).json()
        assert f"Enclosures: Copy of citation; 1 evidence photo; {FORM_TITLE}" in draft["text"]
        adopted = await client.post(
            f"/cases/{case}/adoption",
            headers=ALICE,
            json={"text": draft["text"], "text_sha256": draft["text_sha256"]},
        )
        assert adopted.status_code == 200, adopted.text
        body = {"adopted_sha256": adopted.json()["adopted_sha256"]}
        preview_response = await client.post(
            f"/cases/{case}/packet-preview", headers=ALICE, json=body
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert (
            await client.post(f"/cases/{case}/packet-preview", headers=ALICE, json=body)
        ).json() == preview
        pdf = base64.b64decode(preview["pdf_base64"])
        assert hashlib.sha256(pdf).hexdigest() == preview["packet_sha256"]
        reader = PdfReader(io.BytesIO(pdf))
        assert len(reader.pages) == preview["page_count"] == 4
        assert reader.attachments[ATTACHMENT_NAME] == [draft["text"].encode()]
        assert reader.pages[-1].extract_text().startswith(FORM_TITLE)
        values = {str(w["/T"]): str(w["/V"]) for w in _widgets(reader.pages[-1])}
        assert values == {
            "Citation number": intake["ticket"]["citation_number"],
            "Explanation": statement,
        }
        approved = await client.post(
            f"/cases/{case}/approval",
            headers=ALICE,
            json={
                **body,
                "packet_sha256": preview["packet_sha256"],
                "envelope_sha256": preview["envelope_sha256"],
            },
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["state"] == CaseState.FROZEN.value
        record = real._intake(case)
        assert record.case_token is not None
        frozen = real._store.get(record.case_token, 1)
        assert frozen is not None
        assert frozen.request.pdf == pdf
    finally:
        await client.aclose()
