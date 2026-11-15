"""Ordered chosen evidence extends existing workspace and immutable intake storage."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient
from pydantic import ValidationError

from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from test_appeal_packet import image
from test_case_service import ALICE, BOB, service

REFS = ["photo_" + c * 64 for c in "abc"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_legacy_evidence_selection_keeps_its_wire_representation() -> None:
    empty = s.IntakeForm()
    assert empty.selected_evidence_photo_refs == ()
    assert "evidence_photo_refs" not in empty.model_dump()
    legacy = s.IntakeForm(evidence_photo_ref=REFS[0])
    assert legacy.selected_evidence_photo_refs == (REFS[0],)
    assert "evidence_photo_refs" not in legacy.model_dump()
    assert s.IntakeForm.model_validate_json(legacy.model_dump_json()) == legacy


@pytest.mark.parametrize("selection", [[], REFS[:1], REFS, [REFS[2], REFS[0], REFS[2]]])
def test_canonical_evidence_keeps_exact_chosen_order_and_repetition(selection: list[str]) -> None:
    form = s.IntakeForm(evidence_photo_refs=selection)
    assert form.selected_evidence_photo_refs == tuple(selection)
    assert form.model_dump()["evidence_photo_refs"] == selection
    assert s.IntakeForm.model_validate_json(form.model_dump_json()) == form


def test_conflicting_old_and_new_photo_selections_are_refused_without_guessing() -> None:
    for selection in ([], [REFS[1]], REFS):
        with pytest.raises(ValidationError, match="conflicting evidence photo selections"):
            s.IntakeForm(evidence_photo_ref=REFS[0], evidence_photo_refs=selection)
    matching = s.IntakeForm(evidence_photo_ref=REFS[0], evidence_photo_refs=[REFS[0]])
    assert matching.selected_evidence_photo_refs == (REFS[0],)


@pytest.mark.anyio
async def test_multi_photo_autosave_is_owner_scoped_ordered_and_atomic(tmp_path: Path) -> None:
    real = service(tmp_path / "multi.sqlite")
    async with AsyncClient(
        transport=ASGITransport(app=create_app(real)), base_url="http://real.test"
    ) as client:
        workspace = (await client.post("/workspaces", headers=ALICE, json={})).json()[
            "workspace_id"
        ]
        other = (await client.post("/workspaces", headers=BOB, json={})).json()["workspace_id"]
        refs = []
        for i in range(4):
            owner = ALICE if i < 3 else BOB
            target = workspace if i < 3 else other
            uploaded = await client.post(
                f"/workspaces/{target}/photos",
                headers=owner,
                json={
                    "content_type": "image/png",
                    "data_base64": base64.b64encode(image(color=(i * 60, 30, 90))).decode(),
                },
            )
            assert uploaded.status_code == 201
            refs.append(uploaded.json()["photo_ref"])
        url = f"/workspaces/{workspace}/intake-form"
        bad = await client.put(
            url,
            headers=ALICE,
            json={"expected_revision": 1, "form": {"evidence_photo_refs": refs[:2] + refs[3:]}},
        )
        assert bad.status_code == 409
        unchanged = real._store.get_workspace(workspace)
        assert unchanged is not None
        assert unchanged.revision == 1
        assert len(real._store.workspace_history(workspace)) == 1
        body = {"expected_revision": 1, "form": {"evidence_photo_refs": refs[:3]}}
        first = await client.put(url, headers=ALICE, json=body)
        assert first.status_code == 200
        assert first.json()["intake_form"]["evidence_photo_refs"] == refs[:3]
        repeated = await client.put(url, headers=ALICE, json=body)
        assert repeated.json() == first.json()
        reordered = [refs[2], refs[0], refs[1]]
        moved = await client.put(
            url,
            headers=ALICE,
            json={"expected_revision": 2, "form": {"evidence_photo_refs": reordered}},
        )
        assert moved.status_code == 200
        view = (await client.get(f"/workspaces/{workspace}", headers=ALICE)).json()
        assert view["intake_form"]["evidence_photo_refs"] == reordered
        assert (await client.get(f"/workspaces/{workspace}", headers=BOB)).status_code == 404
        stale = await client.put(url, headers=ALICE, json=body)
        assert stale.status_code == 409
        removed = await client.put(
            url,
            headers=ALICE,
            json={"expected_revision": 3, "form": {"evidence_photo_refs": []}},
        )
        assert removed.status_code == 200
        assert removed.json()["intake_form"]["evidence_photo_refs"] == []
        saved_owner = real._store.workspace_owner(workspace)
        assert saved_owner is not None
        assert all(real._deps.photos.get(saved_owner, ref) is not None for ref in refs[:3])
