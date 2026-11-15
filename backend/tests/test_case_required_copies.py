"""Actual private API, durable collection, drafting and frozen mail packet checks."""

from __future__ import annotations

import base64
import io
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader

from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.appeal.fakes import FakeDrafter
from parkingbreaker.blob_store import FakeBlobStore
from parkingbreaker.case_service import CaseService
from parkingbreaker.case_state import CaseState
from parkingbreaker.research.findings import Finding, FindingStatus
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research_records import decode_research, research_json
from parkingbreaker.transaction_store import SqliteTransactionStore
from samples import source
from test_appeal_packet import image
from test_case_service import ALICE, BOB, NOW, drafting_case
from test_case_service import fake_resolution as fake_resolution

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def require_copy(real: CaseService, case: str) -> None:
    saved = real._store.get_research(case)
    assert saved is not None
    result = decode_research(saved.research_json)
    quote = "You must include a copy of registration."
    page = source("Parking Citation Initial Review\n" + quote, url="https://policy.test/copies")
    finding = Finding(
        FactKey.REQUIRED_ITEMS,
        ["copy of registration"],
        FindingStatus.CONFIRMED,
        quote,
        (page.sha256,),
        jurisdiction=result.ticket.jurisdiction,
        stage=result.ticket.stage,
    )
    result = replace(
        result,
        sources=(*(item for item in result.sources if item.url != page.url), page),
        findings=tuple(finding if f.key is FactKey.REQUIRED_ITEMS else f for f in result.findings),
    )
    real._store.save_research(case, research_json(result), saved.answers_json, NOW, saved.revision)


def selection(real: CaseService, case: str, pages: list[str]) -> s.SaveRequiredCopies:
    view = real.status(case).required_copies
    assert view is not None
    return s.SaveRequiredCopies(
        research_sha256=view.research_sha256,
        expected_revision=view.revision,
        selections=[
            s.RequiredCopySelection(
                requirement_id=view.items[0].requirement_id, page_refs=pages, confirmed=True
            )
        ]
        if pages
        else [],
    )


async def test_complete_copy_survives_restart_and_exact_preview_is_prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        require_copy(real, case)
        status = real.step(case, s.WorkerStep(step_id="draft"))
        assert status.state is CaseState.DRAFTING
        assert status.required_copies is not None
        assert not status.required_copies.ready
        assert isinstance(real._deps.drafter, FakeDrafter)
        assert not real._deps.drafter.calls
        record = real._intake(case)
        pages = [image(color=color) for color in [(20, 100, 80), (30, 40, 130)]]
        refs = [real._deps.photos.put(record.owner, page) for page in pages]
        payload = selection(real, case, refs)
        response = await client.put(
            f"/cases/{case}/required-copies", headers=ALICE, json=payload.model_dump()
        )
        assert response.status_code == 200, response.text
        assert response.json()["required_copies"]["ready"] is True
        replay = await client.put(
            f"/cases/{case}/required-copies", headers=ALICE, json=payload.model_dump()
        )
        assert replay.status_code == 200
        assert replay.json()["required_copies"]["revision"] == 1
        restarted = CaseService(
            replace(real._deps, store=SqliteTransactionStore(tmp_path / "pb.sqlite"))
        )
        view = restarted.status(case).required_copies
        assert view is not None
        assert view.items[0].page_refs == refs
        for index, page in enumerate(pages, 1):
            fetched = await client.get(
                f"/cases/{case}/required-copies/{view.items[0].requirement_id}/pages/{index}",
                headers=ALICE,
            )
            assert fetched.status_code == 200
            assert fetched.content == page
            assert fetched.headers["cache-control"] == "no-store"
        restarted.step(case, s.WorkerStep(step_id="draft"))
        assert restarted.status(case).state is CaseState.AWAITING_ADOPTION
        draft = restarted.draft(case)
        assert "copy of registration" in draft.text
        restarted.adopt_text(case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256))
        preview = restarted.packet_preview(
            case, s.PacketPreviewRequest(adopted_sha256=draft.text_sha256)
        )
        pdf = base64.b64decode(preview.pdf_base64)
        packet_pages = PdfReader(io.BytesIO(pdf)).pages
        assert len(packet_pages) == 5  # letter, citation, optional evidence, two required pages
        for index in range(2):
            assert packet_pages[index + 3].extract_text().startswith("copy of registration")
            assert f"Page {index + 1} of 2" in packet_pages[index + 3].extract_text()
        restarted.approve(
            case,
            s.Approve(
                adopted_sha256=draft.text_sha256,
                packet_sha256=preview.packet_sha256,
                envelope_sha256=preview.envelope_sha256,
            ),
        )
        assert record.case_token is not None
        prepared = restarted._store.get(record.case_token, 1)
        assert prepared is not None
        assert prepared.request.pdf == pdf
        locked = await client.put(
            f"/cases/{case}/required-copies",
            headers=ALICE,
            json=selection(real, case, []).model_dump(),
        )
        assert locked.status_code == 409
        restarted._store.close()
    finally:
        await client.aclose()


async def test_collection_rejects_foreign_unknown_unconfirmed_and_stale_selections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        require_copy(real, case)
        ref = real._deps.photos.put("bob", image())
        body = selection(real, case, [ref]).model_dump()
        path = f"/cases/{case}/required-copies"
        assert (await client.put(path, headers=BOB, json=body)).status_code == 404
        assert (await client.put(path, headers=ALICE, json=body)).status_code == 409
        ref = real._deps.photos.put(real._intake(case).owner, image())
        body = selection(real, case, [ref]).model_dump()
        for bad in [False, 1, "true"]:
            invalid = {**body, "selections": [{**body["selections"][0], "confirmed": bad}]}
            assert (await client.put(path, headers=ALICE, json=invalid)).status_code == 422
        invalid = {**body, "research_sha256": "0" * 64}
        assert (await client.put(path, headers=ALICE, json=invalid)).status_code == 409
        invalid = {**body, "selections": [{**body["selections"][0], "requirement_id": "0" * 64}]}
        assert (await client.put(path, headers=ALICE, json=invalid)).status_code == 409
        assert (await client.put(path, headers=ALICE, json=body)).status_code == 200
        view = real.status(case).required_copies
        assert view is not None
        private_page = f"{path}/{view.items[0].requirement_id}/pages/1"
        assert (await client.get(private_page, headers=BOB)).status_code == 404
        assert (await client.get(private_page)).status_code == 401
        real.save_required_copies(case, selection(real, case, []))
        missing = real.status(case).required_copies
        assert missing is not None
        assert not missing.ready
        assert (await client.put(path, headers=ALICE, json=body)).status_code == 409
    finally:
        await client.aclose()


@pytest.mark.parametrize("change", ["selection", "research", "new_requirement"])
async def test_concurrent_edit_before_drafting_is_rejected_without_lock_or_paid_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        if change != "new_requirement":
            require_copy(real, case)
            ref = real._deps.photos.put(real._intake(case).owner, image())
            real.save_required_copies(case, selection(real, case, [ref]))
        original = real._store.lock_destination_for_draft

        def changed(*args: Any, **kwargs: Any) -> object:
            if change == "selection":
                real.save_required_copies(case, selection(real, case, []))
            else:
                require_copy(real, case)
            return original(*args, **kwargs)

        monkeypatch.setattr(real._store, "lock_destination_for_draft", changed)
        with pytest.raises(ServiceConflict, match="revision_stale"):
            real.step(case, s.WorkerStep(step_id="draft"))
        assert not real._store.has_destination_lock(case, real._intake(case).owner)
        assert isinstance(real._deps.drafter, FakeDrafter)
        assert not real._deps.drafter.calls
        assert real._store.get_draft(case) is None
    finally:
        await client.aclose()


async def test_changed_stored_image_cannot_be_previewed_or_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        require_copy(real, case)
        owner = real._intake(case).owner
        ref = real._deps.photos.put(owner, image())
        real.save_required_copies(case, selection(real, case, [ref]))
        real.step(case, s.WorkerStep(step_id="draft"))
        draft = real.draft(case)
        real.adopt_text(case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256))
        preview = real.packet_preview(
            case, s.PacketPreviewRequest(adopted_sha256=draft.text_sha256)
        )
        assert isinstance(real._deps.photos, FakeBlobStore)
        real._deps.photos.items[(owner, ref)] = image(color=(0, 0, 0))
        with pytest.raises(ServiceConflict, match="required_copies_not_ready"):
            real.approve(
                case,
                s.Approve(
                    adopted_sha256=draft.text_sha256,
                    packet_sha256=preview.packet_sha256,
                    envelope_sha256=preview.envelope_sha256,
                ),
            )
        record = real._intake(case)
        assert record.case_token is not None
        assert real._store.get(record.case_token, 1) is None
    finally:
        await client.aclose()
