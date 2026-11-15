"""Pre-intake recovery uses the existing scoped workspace and revision history."""

from __future__ import annotations

import base64
import copy
from pathlib import Path
from typing import Any

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.transaction_store import SqliteTransactionStore
from test_auth_integration import ORIGIN, RecordingEmail, app_for, headers
from test_blob_store import png
from test_case_service import INTAKE

pytestmark = pytest.mark.anyio


async def test_partial_form_retries_conflicts_scopes_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "form.sqlite"
    mail = RecordingEmail()
    form: dict[str, Any] = {
        "values": {
            "story": "The sign was covered. The kiosk was blank.",
            "contact.name": "Test Person",
        },
        "step": 2,
    }
    with SqliteTransactionStore(path) as store:
        app = app_for(store, tmp_path, mail)
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as a,
            AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as b,
        ):
            for client in (a, b):
                assert (
                    await client.post("/auth/appeals", headers={"Origin": ORIGIN}, json={})
                ).status_code == 201
            workspace = (await a.post("/workspaces", headers=headers(a), json={})).json()[
                "workspace_id"
            ]
            url = f"/workspaces/{workspace}/intake-form"
            assert (
                await b.put(url, headers=headers(b), json={"expected_revision": 1, "form": form})
            ).status_code == 404
            assert (
                await a.put(url, json={"expected_revision": 1, "form": form})
            ).status_code == 403
            first = await a.put(
                url, headers=headers(a), json={"expected_revision": 1, "form": form}
            )
            assert first.status_code == 200
            assert first.json()["draft_revision"] == 2
            for _ in range(2):
                repeated = await a.put(
                    url, headers=headers(a), json={"expected_revision": 1, "form": form}
                )
                assert repeated.status_code == 200
                assert repeated.json()["draft_revision"] == 2
            changed = {"values": {"story": "Unsent new text"}, "step": 2}
            assert (
                await a.put(url, headers=headers(a), json={"expected_revision": 1, "form": changed})
            ).status_code == 409
            assert (await a.get(f"/workspaces/{workspace}")).json()["intake_form"] == {
                **form,
                "ticket_photo_ref": None,
                "evidence_photo_ref": None,
            }
            other = (await b.post("/workspaces", headers=headers(b), json={})).json()[
                "workspace_id"
            ]
            photo = await b.post(
                f"/workspaces/{other}/photos",
                headers=headers(b),
                json={"content_type": "image/png", "data_base64": base64.b64encode(png()).decode()},
            )
            foreign = {**form, "ticket_photo_ref": photo.json()["photo_ref"]}
            assert (
                await a.put(url, headers=headers(a), json={"expected_revision": 2, "form": foreign})
            ).status_code == 409
            cookies = dict(a.cookies)
        assert len(store.workspace_history(workspace)) == 2
    with SqliteTransactionStore(path) as reopened:
        app = app_for(reopened, tmp_path, mail)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=ORIGIN, cookies=cookies
        ) as a:
            saved = (await a.get("/auth/appeal")).json()
            assert saved["workspace_id"] == workspace
            assert saved["intake_form"]["values"]["story"] == form["values"]["story"]
            intake = copy.deepcopy(INTAKE)
            intake["photo_refs"] = []
            intake["ticket"]["issuer_kind"] = "PRIVATE"
            assert (
                await a.post(f"/workspaces/{workspace}/intake", headers=headers(a), json=intake)
            ).status_code == 200
            assert (
                await a.put(
                    url,
                    headers=headers(a),
                    json={
                        "expected_revision": 2,
                        "form": {"values": {"story": "Do not change submitted appeal"}},
                    },
                )
            ).status_code == 409
            assert (await a.get("/auth/appeal")).json()["intake_form"] == saved["intake_form"]
