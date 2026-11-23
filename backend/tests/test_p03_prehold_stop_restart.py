"""P03 regression: a pre-hold stop must not dead-end the assisted restart.

A stop decided before issuer research (too old, rental-company paid, or an
unverifiable issuer) saves a Stop pre-hold view and never creates a case row.
Before this fix:

* ``POST /workspaces`` answered 409 ``appeal_already_started``, while
* a corrected ``POST /workspaces/{id}/intake`` answered 409
  ``intake_already_submitted_create_new_workspace`` (which tells the customer
  to start a new workspace).

With cookie auth the route returned the existing workspace before the restart
path could run, so every authenticated customer hit the same dead end.

These tests cover the bearer path, the deployed cookie-auth route path, the
carried-notes replay rule, and the negative case where a live intake must
replay instead of restarting.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.transaction_store import SqliteTransactionStore
from test_auth_integration import (
    ORIGIN,
    WORKER,
    RecordingEmail,
    app_for,
    headers,
    sign_in,
)
from test_case_service import ALICE, INTAKE, PNG_B64, create_app, fake_resolution, service

assert fake_resolution
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fake_resolution")]


def _client(tmp_path: Path) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=create_app(service(tmp_path / "pb.sqlite"))),
        base_url="http://real.test",
    )


async def _photo(client: AsyncClient, workspace_id: str, auth: dict[str, str]) -> str:
    response = await client.post(
        f"/workspaces/{workspace_id}/photos",
        headers=auth,
        json={"content_type": "image/png", "data_base64": PNG_B64},
    )
    assert response.status_code == 201, response.text
    return response.json()["photo_ref"]


async def _submit(
    client: AsyncClient, workspace_id: str, auth: dict[str, str], *, rental_paid: bool
) -> dict[str, Any]:
    ref = await _photo(client, workspace_id, auth)
    body: dict[str, Any] = {
        **INTAKE,
        "photo_refs": [ref],
        "citation_photo_ref": ref,
    }
    if rental_paid:
        body["rental_company_paid"] = True
    receipt = await client.post(f"/workspaces/{workspace_id}/intake", headers=auth, json=body)
    assert receipt.status_code == 200, receipt.text
    return receipt.json()


async def test_prehold_stop_restart_carries_into_a_fresh_workspace(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        started = await client.post("/workspaces", headers=ALICE, json={"notes": ""})
        assert started.status_code == 201, started.text
        stopped_workspace = started.json()["workspace_id"]
        receipt = await _submit(client, stopped_workspace, ALICE, rental_paid=True)
        assert receipt["state"] == "STOPPED_NO_CHARGE"
        stopped_case = (await client.get(f"/workspaces/{stopped_workspace}", headers=ALICE)).json()[
            "case_id"
        ]
        assert stopped_case is not None

        restarted = await client.post("/workspaces", headers=ALICE, json={"notes": ""})
        assert restarted.status_code == 201, restarted.text
        fresh = restarted.json()["workspace_id"]
        assert fresh != stopped_workspace
        # A repeated start before submitting replays the fresh (still live) workspace.
        replay = await client.post("/workspaces", headers=ALICE, json={"notes": ""})
        assert replay.status_code == 201, replay.text
        assert replay.json()["workspace_id"] == fresh

        # The dead end is gone: a corrected intake creates a distinct live case.
        corrected = await _submit(client, fresh, ALICE, rental_paid=False)
        assert corrected["state"] == "INTAKE"
        assert corrected["case_id"] != stopped_case
        assert stopped_case != fresh
        # The stopped workspace keeps its own truthful stop and intake record.
        stopped_view = (await client.get(f"/workspaces/{stopped_workspace}", headers=ALICE)).json()
        assert stopped_view["intake"] is not None


async def test_live_intake_replays_instead_of_restarting(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        started = await client.post("/workspaces", headers=ALICE, json={"notes": ""})
        workspace = started.json()["workspace_id"]
        receipt = await _submit(client, workspace, ALICE, rental_paid=False)
        assert receipt["state"] == "INTAKE"
        replay = await client.post("/workspaces", headers=ALICE, json={"notes": ""})
        assert replay.status_code == 201, replay.text
        assert replay.json()["workspace_id"] == workspace


async def test_email_auth_restart_carried_notes_replay(tmp_path: Path) -> None:
    """The deployed cookie-auth route must restart, then replay with carried notes."""
    with SqliteTransactionStore(tmp_path / "auth.sqlite") as store:
        mail = RecordingEmail()
        app = app_for(store, tmp_path, mail)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
            await sign_in(client, mail, "restart@example.test")
            auth = headers(client)
            started = await client.post("/workspaces", headers=auth, json={"notes": "keep me"})
            assert started.status_code == 201, started.text
            stopped_workspace = started.json()["workspace_id"]
            receipt = await _submit(client, stopped_workspace, auth, rental_paid=True)
            assert receipt["state"] == "STOPPED_NO_CHARGE"

            # An empty notes body after a restart carries the earlier notes, so a
            # second start must replay the fresh workspace instead of 409.
            restarted = await client.post("/workspaces", headers=auth, json={"notes": ""})
            assert restarted.status_code == 201, restarted.text
            fresh = restarted.json()["workspace_id"]
            assert fresh != stopped_workspace
            assert restarted.json()["notes"] == "keep me"
            replay = await client.post("/workspaces", headers=auth, json={"notes": ""})
            assert replay.status_code == 201, replay.text
            assert replay.json()["workspace_id"] == fresh

            # A different non-empty note is still refused while the workspace is live.
            conflict = await client.post("/workspaces", headers=auth, json={"notes": "other"})
            assert conflict.status_code == 409, conflict.text
            assert conflict.json()["detail"] == "appeal_already_started"

            # Recovery must still work after a restart: a workspace-bound email
            # link is queued and confirms even though the owner now holds a restart
            # chain of two workspaces (the legacy guard used to drop it silently).
            assert (
                await client.post("/auth/logout", headers=headers(client), json={})
            ).status_code == 200
            before = len(mail.sent)
            link = await client.post(
                "/auth/link",
                headers={"Origin": ORIGIN},
                json={
                    "email": "restart@example.test",
                    "request_id": str(uuid.uuid4()),
                    "workspace_id": fresh,
                },
            )
            assert link.status_code == 202, link.text
            assert (await client.post("/internal/dispatch", headers=WORKER)).status_code == 200
            assert len(mail.sent) == before + 1
            token = mail.sent[-1].text.split("#token=", 1)[1].splitlines()[0]
            confirm = await client.post(
                "/auth/confirm", headers={"Origin": ORIGIN}, json={"token": token}
            )
            assert confirm.status_code == 200, confirm.text
            assert (
                await client.get(f"/workspaces/{fresh}", headers=headers(client))
            ).status_code == 200


async def test_bearer_restart_with_carried_notes_replays(tmp_path: Path) -> None:
    """The store replay rule treats an empty notes body as no preference."""
    async with _client(tmp_path) as client:
        started = await client.post("/workspaces", headers=ALICE, json={"notes": "keep me"})
        stopped_workspace = started.json()["workspace_id"]
        receipt = await _submit(client, stopped_workspace, ALICE, rental_paid=True)
        assert receipt["state"] == "STOPPED_NO_CHARGE"
        restarted = await client.post("/workspaces", headers=ALICE, json={"notes": ""})
        assert restarted.status_code == 201, restarted.text
        fresh = restarted.json()["workspace_id"]
        assert fresh != stopped_workspace
        assert restarted.json()["notes"] == "keep me"
        replay = await client.post("/workspaces", headers=ALICE, json={"notes": ""})
        assert replay.status_code == 201, replay.text
        assert replay.json()["workspace_id"] == fresh
