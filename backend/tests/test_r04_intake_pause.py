"""R04: the all-new-intake kill-switch, and that accepted work keeps running.

The roadmap requires the three stop controls to be exercised separately. This file covers
the third one through the real application paths: a new intake is refused, an identical
replay of an already accepted intake is not, and a case that was already accepted still
reaches its mailing outcome while the switch is down.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.api.app import create_app
from parkingbreaker.intake_pause_store import IntakePauseConflict
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_case_service import ALICE, INTAKE, PNG_B64, fake_resolution, service, stripe
from test_stripe_payment import fixture

PROCESSED_AT = datetime(2026, 11, 21, 12, tzinfo=UTC)
# Re-exported so pytest resolves the fixture this module requests.
assert fake_resolution


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _workspace_and_body(client: AsyncClient) -> tuple[str, dict[str, object]]:
    workspace = (await client.post("/workspaces", headers=ALICE, json={"notes": ""})).json()
    workspace_id = workspace["workspace_id"]
    uploaded = await client.post(
        f"/workspaces/{workspace_id}/photos",
        headers=ALICE,
        json={"content_type": "image/png", "data_base64": PNG_B64},
    )
    assert uploaded.status_code == 201, uploaded.text
    ref = uploaded.json()["photo_ref"]
    return workspace_id, {**INTAKE, "photo_refs": [ref], "citation_photo_ref": ref}


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_paused_switch_refuses_a_new_intake_and_allows_it_after_resume(
    tmp_path: Path,
) -> None:
    payments, _transport = stripe(fixture(), fixture())
    real = service(tmp_path / "pause.sqlite", payments)
    client = AsyncClient(transport=ASGITransport(app=create_app(real)), base_url="http://real.test")
    async with client:
        workspace_id, body = await _workspace_and_body(client)

        real._store.set_intake_pause(
            True, 0, "pause-1", "Amir", "stop all new intake", now=PROCESSED_AT
        )
        refused = await client.post(f"/workspaces/{workspace_id}/intake", headers=ALICE, json=body)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"] == "intake_paused"

        real._store.set_intake_pause(
            False, 1, "resume-1", "Amir", "resume intake", now=PROCESSED_AT
        )
        allowed = await client.post(f"/workspaces/{workspace_id}/intake", headers=ALICE, json=body)
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["case_id"]

        # An identical replay is not new work and stays allowed while paused.
        real._store.set_intake_pause(
            True, 2, "pause-2", "Amir", "stop all new intake again", now=PROCESSED_AT
        )
        replay = await client.post(f"/workspaces/{workspace_id}/intake", headers=ALICE, json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()["case_id"] == allowed.json()["case_id"]


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_paused_switch_keeps_an_accepted_case_completing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_effect_dispatch import approved

    lob = FakeLobClient([LobResponse(200, "ltr_paused_case", evidence=LetterEvidence.PRODUCTION)])
    client, real, case_id = await approved(tmp_path, monkeypatch, lob)
    try:
        real._store.set_intake_pause(
            True, 0, "pause-3", "Amir", "stop all new intake", now=PROCESSED_AT
        )
        real.dispatch(case_id)
        assert real.status(case_id).state == "IN_TRANSIT"
        assert len(lob.calls) == 1
    finally:
        await client.aclose()


def test_paused_switch_survives_a_store_reopen_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite"
    with SqliteTransactionStore(path) as store:
        saved = store.set_intake_pause(
            True, 0, "pause-4", "Amir", "stop all new intake", now=PROCESSED_AT
        )
        assert saved.revision == 1
        assert saved.paused is True
        replay = store.set_intake_pause(
            True, 0, "pause-4", "Amir", "stop all new intake", now=PROCESSED_AT
        )
        assert replay.replayed is True
        with pytest.raises(IntakePauseConflict, match="stale_expected_revision"):
            store.set_intake_pause(False, 0, "resume-4", "Amir", "resume", now=PROCESSED_AT)
    with SqliteTransactionStore(path) as store:
        assert store.intake_paused() is True
        status = store.intake_pause_status()
        assert status.revision == 1
        assert status.actor == "Amir"
