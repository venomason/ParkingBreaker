"""P03 regression: a pre-hold stop must not dead-end the assisted restart.

A stop decided before issuer research (too old, rental-company paid, or an
unverifiable issuer) saves a Stop pre-hold view and never creates a case row.
Before this fix the restart gate only looked at case rows, so:

* ``POST /workspaces`` answered 409 ``appeal_already_started``, while
* a corrected ``POST /workspaces/{id}/intake`` answered 409
  ``intake_already_submitted_create_new_workspace`` (which tells the customer
  to start a new workspace).

That pair left the customer unable to appeal at all. The fix treats the saved
pre-hold Stop as a finished transaction so the assisted restart path can carry
their words and photos into a fresh workspace.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.transaction_store import SqliteTransactionStore
from test_auth_integration import ORIGIN, RecordingEmail, app_for, headers, sign_in
from test_case_service import ALICE, INTAKE, PNG_B64, create_app, fake_resolution, service

assert fake_resolution
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fake_resolution")]


def _client(tmp_path: Path) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=create_app(service(tmp_path / "pb.sqlite"))),
        base_url="http://real.test",
    )


async def _submit_stopped_intake(client: AsyncClient) -> str:
    """Submit a rental-company-paid ticket: a pre-hold stop with no case row."""
    workspace = (await client.post("/workspaces", headers=ALICE, json={"notes": ""})).json()
    path = f"/workspaces/{workspace['workspace_id']}"
    photo = await client.post(
        f"{path}/photos",
        headers=ALICE,
        json={"content_type": "image/png", "data_base64": PNG_B64},
    )
    assert photo.status_code == 201, photo.text
    ref = photo.json()["photo_ref"]
    body: dict[str, Any] = {
        **INTAKE,
        "rental_company_paid": True,
        "photo_refs": [ref],
        "citation_photo_ref": ref,
    }
    receipt = await client.post(f"{path}/intake", headers=ALICE, json=body)
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["state"] == "STOPPED_NO_CHARGE"
    return receipt.json()["workspace_id"]


async def test_prehold_stop_restart_creates_a_fresh_prefilled_workspace(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        stopped_workspace = await _submit_stopped_intake(client)
        restarted = await client.post("/workspaces", headers=ALICE, json={"notes": ""})
        assert restarted.status_code == 201, restarted.text
        fresh = restarted.json()["workspace_id"]
        assert fresh != stopped_workspace
        # A repeated start before submitting replays the same fresh workspace.
        replay = await client.post("/workspaces", headers=ALICE, json={"notes": ""})
        assert replay.status_code == 201, replay.text
        assert replay.json()["workspace_id"] == fresh
        # The dead end is gone: the customer can now submit a corrected appeal.
        uploaded = await client.post(
            f"/workspaces/{fresh}/photos",
            headers=ALICE,
            json={"content_type": "image/png", "data_base64": PNG_B64},
        )
        assert uploaded.status_code == 201, uploaded.text
        ref = uploaded.json()["photo_ref"]
        corrected = await client.post(
            f"/workspaces/{fresh}/intake",
            headers=ALICE,
            json={**INTAKE, "photo_refs": [ref], "citation_photo_ref": ref},
        )
        assert corrected.status_code == 200, corrected.text
        assert corrected.json()["case_id"] != stopped_workspace
        # The stopped workspace keeps its own truthful stop and intake record.
        stopped_view = (await client.get(f"/workspaces/{stopped_workspace}", headers=ALICE)).json()
        assert stopped_view["intake"] is not None


async def test_email_auth_restart_after_prehold_stop(tmp_path: Path) -> None:
    """The deployed email-auth route must not short-circuit a finished transaction.

    With cookie auth, ``POST /workspaces`` used to return the owner's existing
    workspace unconditionally, so the pre-hold-stop restart never ran there even
    though the service could restart. This drives the auth route itself.
    """
    with SqliteTransactionStore(tmp_path / "auth.sqlite") as store:
        mail = RecordingEmail()
        app = app_for(store, tmp_path, mail)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
            await sign_in(client, mail, "restart@example.test")
            started = await client.post("/workspaces", headers=headers(client), json={"notes": ""})
            assert started.status_code == 201, started.text
            stopped_workspace = started.json()["workspace_id"]
            path = f"/workspaces/{stopped_workspace}"
            photo = await client.post(
                f"{path}/photos",
                headers=headers(client),
                json={"content_type": "image/png", "data_base64": PNG_B64},
            )
            assert photo.status_code == 201, photo.text
            ref = photo.json()["photo_ref"]
            body: dict[str, Any] = {
                **INTAKE,
                "rental_company_paid": True,
                "photo_refs": [ref],
                "citation_photo_ref": ref,
            }
            receipt = await client.post(f"{path}/intake", headers=headers(client), json=body)
            assert receipt.status_code == 200, receipt.text
            assert receipt.json()["state"] == "STOPPED_NO_CHARGE"

            restarted = await client.post(
                "/workspaces", headers=headers(client), json={"notes": ""}
            )
            assert restarted.status_code == 201, restarted.text
            fresh = restarted.json()["workspace_id"]
            assert fresh != stopped_workspace
