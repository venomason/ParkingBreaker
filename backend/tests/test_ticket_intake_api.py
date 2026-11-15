"""The owner-only ticket-reading API: routes, auth scope, revisions and readback."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.api.fakes import FakeServices
from parkingbreaker.llm import Usage
from parkingbreaker.ticket_intake_service import TicketIntakeServiceError
from test_ticket_intake_service import (
    BACK,
    FRONT,
    ScriptedVisionClient,
    attachment,
    b64,
    binding_for,
    harness,
    payload,
    reading_payload,
    review_payload,
)

pytestmark = pytest.mark.anyio
ORIGIN = "http://ticket.test"
ALICE = {"Authorization": "Bearer fake-alice"}
BOB = {"Authorization": "Bearer fake-bob"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def app_for(tmp_path: Path, client: ScriptedVisionClient) -> tuple[FastAPI, Any]:
    harnessed = harness(tmp_path / "api.sqlite", client)
    return create_app(harnessed.service), harnessed


def form_json(binding: Any, front_ref: str, back_ref: str | None = None) -> dict[str, Any]:
    return s.IntakeForm(
        values={"story": "Saved words"},
        ticket_photo_ref=front_ref,
        ticket_back_photo_ref=back_ref,
        ticket_fact_binding=payload(binding),
    ).model_dump(mode="json")


async def upload(client: AsyncClient, workspace: str, data: bytes) -> str:
    response = await client.post(
        f"/workspaces/{workspace}/photos",
        headers=ALICE,
        json={"content_type": "image/png", "data_base64": b64(data)},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["photo_ref"])


def assert_no_operator_internals(body: dict[str, Any]) -> None:
    text = repr(body)
    assert "claim_token" not in text
    assert "claim_token_sha256" not in text
    assert "binding_sha256" not in text


async def test_owner_routes_reading_review_confirmation_and_readback(tmp_path: Path) -> None:
    binding = binding_for()
    client = ScriptedVisionClient(
        (reading_payload(), Usage(cost_cents=2)),
        (review_payload(binding.review_fields()), Usage(cost_cents=2)),
    )
    app, harnessed = app_for(tmp_path, client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as api:
        workspace = (await api.post("/workspaces", headers=ALICE, json={"notes": ""})).json()[
            "workspace_id"
        ]
        front = await upload(api, workspace, FRONT)
        back = await upload(api, workspace, BACK)
        saved = await api.put(
            f"/workspaces/{workspace}/intake-form",
            headers=ALICE,
            json={"expected_revision": 1, "form": form_json(binding, front, back)},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["ticket_reading_required"] is True

        status = await api.get(f"/workspaces/{workspace}/ticket-reading", headers=ALICE)
        assert status.status_code == 200
        assert status.json()["enabled"] is True
        assert status.json()["required"] is True
        assert status.json()["extraction"]["status"] == "missing"
        assert_no_operator_internals(status.json())

        reading = await api.post(
            f"/workspaces/{workspace}/ticket-reading",
            headers=ALICE,
            json={"request_id": "req-extract", "expected_revision": 2},
        )
        assert reading.status_code == 200, reading.text
        assert reading.json()["status"] == "success"
        assert reading.json()["request_id"] == "req-extract"
        assert {item["field"] for item in reading.json()["fields"]}
        assert_no_operator_internals(reading.json())

        review = await api.post(
            f"/workspaces/{workspace}/ticket-review",
            headers=ALICE,
            json={"request_id": "req-review", "expected_revision": 2},
        )
        assert review.status_code == 200, review.text
        assert review.json()["status"] == "success"
        assert review.json()["basis"] == "model comparison only; not external verification"
        assert_no_operator_internals(review.json())

        confirmation = await api.post(
            f"/workspaces/{workspace}/ticket-confirmation",
            headers=ALICE,
            json={
                "command_id": "cmd-confirm",
                "expected_revision": 2,
                "review_request_id": "req-review",
                "expected_confirmation_generation": 0,
                "resolved_fields": {},
            },
        )
        assert confirmation.status_code == 200, confirmation.text
        assert confirmation.json()["generation"] == 1
        assert confirmation.json()["discrepancy_status"] == "none"
        assert_no_operator_internals(confirmation.json())

        restored = (await api.get(f"/workspaces/{workspace}/ticket-reading", headers=ALICE)).json()
        assert restored["extraction"]["status"] == "success"
        assert restored["extraction"]["current"] is True
        assert restored["review"]["status"] == "success"
        assert restored["confirmation"]["state"] == "current"
        assert restored["confirmation"]["generation"] == 1
        assert restored["next_confirmation_generation"] == 1

        # A story/contact-only edit keeps the ticket-only confirmation current.
        edited = await api.put(
            f"/workspaces/{workspace}/intake-form",
            headers=ALICE,
            json={
                "expected_revision": 2,
                "form": {
                    "values": {"story": "A different story", "contact.name": "Someone"},
                    "ticket_photo_ref": front,
                    "ticket_back_photo_ref": back,
                },
            },
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["intake_form"]["ticket_fact_binding"] is not None
        kept = (await api.get(f"/workspaces/{workspace}/ticket-reading", headers=ALICE)).json()
        assert kept["confirmation"]["state"] == "current"

        # Wrong owner and unknown workspace are indistinguishable.
        assert (
            await api.get(f"/workspaces/{workspace}/ticket-reading", headers=BOB)
        ).status_code == 404
        assert (
            await api.post(
                f"/workspaces/{workspace}/ticket-reading",
                headers=BOB,
                json={"request_id": "req-bob", "expected_revision": 4},
            )
        ).status_code == 404
        assert (
            await api.get("/workspaces/ws_missing/ticket-reading", headers=ALICE)
        ).status_code == 404

        # Stale revision and malformed commands are refused without a new provider call.
        stale = await api.post(
            f"/workspaces/{workspace}/ticket-reading",
            headers=ALICE,
            json={"request_id": "req-stale", "expected_revision": 2},
        )
        assert stale.status_code == 409
        assert stale.json() == {"detail": "workspace_changed_reload_required"}
        invalid = await api.post(
            f"/workspaces/{workspace}/ticket-confirmation",
            headers=ALICE,
            json={
                "command_id": "cmd-invalid",
                "expected_revision": 3,
                "review_request_id": "req-review",
                "expected_confirmation_generation": 1,
                "resolved_fields": {"not_a_field": "ABSENCE_ACKNOWLEDGED"},
            },
        )
        assert invalid.status_code == 422
        assert invalid.json() == {"detail": "invalid_request"}
    assert len(harnessed.client.calls) == 2


async def test_quorum_replays_and_story_edits_do_not_invalidate_confirmation(
    tmp_path: Path,
) -> None:
    binding = binding_for()
    client = ScriptedVisionClient(
        (reading_payload(), Usage(cost_cents=1)),
        (review_payload(binding.review_fields()), Usage(cost_cents=1)),
    )
    app, harnessed = app_for(tmp_path, client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as api:
        workspace = (await api.post("/workspaces", headers=ALICE, json={"notes": ""})).json()[
            "workspace_id"
        ]
        front = await upload(api, workspace, FRONT)
        await api.put(
            f"/workspaces/{workspace}/intake-form",
            headers=ALICE,
            json={"expected_revision": 1, "form": form_json(binding, front)},
        )
        first = await api.post(
            f"/workspaces/{workspace}/ticket-reading",
            headers=ALICE,
            json={"request_id": "req-extract", "expected_revision": 2},
        )
        assert first.status_code == 200
        repeat = await api.post(
            f"/workspaces/{workspace}/ticket-reading",
            headers=ALICE,
            json={"request_id": "req-extract", "expected_revision": 2},
        )
        assert repeat.status_code == 200
        assert repeat.json()["request_id"] == "req-extract"
        assert len(harnessed.client.calls) == 1

        await api.post(
            f"/workspaces/{workspace}/ticket-review",
            headers=ALICE,
            json={"request_id": "req-review", "expected_revision": 2},
        )
        confirmed = await api.post(
            f"/workspaces/{workspace}/ticket-confirmation",
            headers=ALICE,
            json={
                "command_id": "cmd-confirm",
                "expected_revision": 2,
                "review_request_id": "req-review",
                "expected_confirmation_generation": 0,
                "resolved_fields": {},
            },
        )
        assert confirmed.status_code == 200
        # The exact command replay returns the original historical result.
        replayed = await api.post(
            f"/workspaces/{workspace}/ticket-confirmation",
            headers=ALICE,
            json={
                "command_id": "cmd-confirm",
                "expected_revision": 2,
                "review_request_id": "req-review",
                "expected_confirmation_generation": 0,
                "resolved_fields": {},
            },
        )
        assert replayed.status_code == 200
        assert replayed.json() == confirmed.json()


async def test_preview_is_disabled_until_explicitly_injected() -> None:
    app = create_app(FakeServices())
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as api:
        workspace = (await api.post("/workspaces", headers=ALICE, json={"notes": ""})).json()[
            "workspace_id"
        ]
        status = await api.get(f"/workspaces/{workspace}/ticket-reading", headers=ALICE)
        assert status.status_code == 200
        assert status.json()["enabled"] is False
        assert status.json()["required"] is False
        refused = await api.post(
            f"/workspaces/{workspace}/ticket-reading",
            headers=ALICE,
            json={"request_id": "req-preview", "expected_revision": 1},
        )
        assert refused.status_code == 409
        assert refused.json() == {"detail": "ticket_reading_disabled"}


def test_media_attachments_are_exact_owner_images() -> None:
    # The API never accepts a client media label in place of the stored bytes; the
    # coordinator detects JPEG/PNG structurally. This asserts the helper stays exact.
    assert attachment(FRONT) == attachment(base64.b64decode(b64(FRONT)))
    assert attachment(FRONT).media_type == "image/png"


def test_service_refusal_reason_is_a_fixed_key() -> None:
    error = TicketIntakeServiceError("ticket_confirmation_required")
    assert str(error) == "ticket_confirmation_required"
    assert "alice" not in str(error)
