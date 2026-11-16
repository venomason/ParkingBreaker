"""Runtime composition: required policy, synthetic preview and legacy compatibility."""

from __future__ import annotations

import copy
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.case_service import CaseService
from parkingbreaker.config import (
    ProviderMode,
    build_services,
    load_settings,
    ticket_reading_required,
)
from parkingbreaker.ticket_fact_binding import TicketFactBinding
from test_case_service import INTAKE
from test_ticket_intake_service import (
    BACK,
    FRONT,
    b64,
    binding_for,
    payload,
)

pytestmark = pytest.mark.anyio
ORIGIN = "http://compose.test"
SECRET = "worker-secret-0123456789"
ALICE_HEADERS = {"Authorization": "Bearer fake-alice"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def fake_settings(tmp_path: Path, *, preview: bool) -> Any:
    values = {
        "PB_PROVIDER_MODE": "fake",
        "PB_WORKER_SECRET": SECRET,
        "PB_DATA_DIR": str(tmp_path),
    }
    if preview:
        values["PB_TICKET_READING_PREVIEW"] = "1"
    return load_settings(values, tmp_path / "no.env")


def form_json(binding: TicketFactBinding, front: str, back: str | None) -> dict[str, Any]:
    return s.IntakeForm(
        values={"story": "composed story"},
        ticket_photo_ref=front,
        ticket_back_photo_ref=back,
        ticket_fact_binding=payload(binding),
    ).model_dump(mode="json")


async def upload(api: AsyncClient, workspace: str, data: bytes) -> str:
    response = await api.post(
        f"/workspaces/{workspace}/photos",
        headers=ALICE_HEADERS,
        json={"content_type": "image/png", "data_base64": b64(data)},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["photo_ref"])


def test_required_policy_by_provider_mode() -> None:
    assert ticket_reading_required(ProviderMode.PREVIEW, {}) is False
    assert ticket_reading_required(ProviderMode.FAKE, {}) is False
    assert ticket_reading_required(ProviderMode.FAKE, {"PB_TICKET_READING_PREVIEW": "1"}) is True
    assert ticket_reading_required(ProviderMode.TEST, {}) is True
    assert ticket_reading_required(ProviderMode.LIVE, {}) is True


async def test_fake_default_stays_legacy_compatible_and_disabled(tmp_path: Path) -> None:
    settings = fake_settings(tmp_path, preview=False)
    services = build_services(settings)
    assert services is not None
    assert services.ticket_reading_required is False
    app = create_app(services, worker_secret=settings.worker_secret, provider_mode="fake")
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as api:
        workspace = (
            await api.post("/workspaces", headers=ALICE_HEADERS, json={"notes": ""})
        ).json()["workspace_id"]
        status = await api.get(f"/workspaces/{workspace}/ticket-reading", headers=ALICE_HEADERS)
        assert status.json()["enabled"] is False
        assert status.json()["required"] is False
        front = await upload(api, workspace, FRONT)
        intake = copy.deepcopy(INTAKE)
        intake["photo_refs"] = []
        intake["citation_photo_ref"] = front
        intake.pop("citation_back_photo_ref", None)
        receipt = await api.post(
            f"/workspaces/{workspace}/intake", headers=ALICE_HEADERS, json=intake
        )
        assert receipt.status_code == 200, receipt.text
        assert receipt.json()["case_id"].startswith("case_")


async def test_fake_preview_enables_full_synthetic_acceptance(tmp_path: Path) -> None:
    settings = fake_settings(tmp_path, preview=True)
    services = build_services(settings)
    assert services is not None
    assert isinstance(services, CaseService)
    assert services.ticket_reading_required is True
    assert services._deps.ticket_intake is not None
    app = create_app(services, worker_secret=settings.worker_secret, provider_mode="fake")

    binding = binding_for(dict.fromkeys(dict(binding_for().literal)))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as api:
        workspace = (
            await api.post("/workspaces", headers=ALICE_HEADERS, json={"notes": ""})
        ).json()["workspace_id"]
        front = await upload(api, workspace, FRONT)
        back = await upload(api, workspace, BACK)
        saved = await api.put(
            f"/workspaces/{workspace}/intake-form",
            headers=ALICE_HEADERS,
            json={"expected_revision": 1, "form": form_json(binding, front, back)},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["ticket_reading_required"] is True

        status = (
            await api.get(f"/workspaces/{workspace}/ticket-reading", headers=ALICE_HEADERS)
        ).json()
        assert status["required"] is True
        assert status["synthetic"] is True

        reading = await api.post(
            f"/workspaces/{workspace}/ticket-reading",
            headers=ALICE_HEADERS,
            json={"request_id": "req-synthetic-reading", "expected_revision": 2},
        )
        assert reading.status_code == 200, reading.text
        assert reading.json()["synthetic"] is True
        assert reading.json()["status"] == "success"
        assert all(item["value"] is None for item in reading.json()["fields"])

        review = await api.post(
            f"/workspaces/{workspace}/ticket-review",
            headers=ALICE_HEADERS,
            json={"request_id": "req-synthetic-review", "expected_revision": 2},
        )
        assert review.status_code == 200, review.text
        assert review.json()["synthetic"] is True
        resolved = {
            item["field"]: s.TicketDiscrepancyResolution.ABSENCE_ACKNOWLEDGED
            for item in review.json()["fields"]
        }
        assert resolved == {}
        assert review.json()["basis"] == "customer-supplied facts; no model comparison"

        confirmation = await api.post(
            f"/workspaces/{workspace}/ticket-confirmation",
            headers=ALICE_HEADERS,
            json={
                "command_id": "cmd-synthetic",
                "expected_revision": 2,
                "review_request_id": "req-synthetic-review",
                "expected_confirmation_generation": 0,
                "accuracy_acknowledged": True,
                "resolved_fields": {
                    name: resolution.value for name, resolution in resolved.items()
                },
            },
        )
        assert confirmation.status_code == 200, confirmation.text
        assert confirmation.json()["generation"] == 1
        assert confirmation.json()["synthetic"] is True

        intake = copy.deepcopy(INTAKE)
        intake["photo_refs"] = []
        intake["citation_photo_ref"] = front
        intake["citation_back_photo_ref"] = back
        intake["ticket_confirmation_command_id"] = "cmd-synthetic"
        receipt = await api.post(
            f"/workspaces/{workspace}/intake", headers=ALICE_HEADERS, json=intake
        )
        assert receipt.status_code == 200, receipt.text
        case_id = receipt.json()["case_id"]
        view = (await api.get(f"/workspaces/{workspace}", headers=ALICE_HEADERS)).json()
        assert view["case_id"] == case_id
        with sqlite3.connect(tmp_path / "parkingbreaker.sqlite") as database:
            row = database.execute(
                "SELECT intake_id FROM pb_ticket_intake_reservations WHERE workspace_token=?",
                (workspace,),
            ).fetchone()
        assert row is not None
        assert row[0] == case_id


def test_test_mode_composes_and_requires_the_real_path(tmp_path: Path) -> None:
    snapshot = tmp_path / "dotgov-2026-10-17.csv"
    snapshot.write_text(
        "Domain name,Domain type,Organization name,Suborganization name,City,State,"
        "Security contact email\n",
        encoding="utf-8",
    )
    values = {
        "PB_PROVIDER_MODE": "test",
        "PB_WORKER_SECRET": SECRET,
        "PB_DATA_DIR": str(tmp_path),
        "STRIPE_SECRET_KEY": "sk_test_offline",
        "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": "pk_test_offline",
        "LOB_API_KEY": "test_offline",
        "STRIPE_WEBHOOK_SECRET": "whsec_offline",
        "LOB_WEBHOOK_SECRET": "lob-offline-secret",
        "MINIMAX_API_KEY": "test-minimax-placeholder",
        "OPENAI_API_KEY": "test-openai-placeholder",
        "BRAVE_SEARCH_API_KEY": "test-placeholder",
        "PB_DOTGOV_SNAPSHOT": str(snapshot),
    }
    settings = load_settings(values, tmp_path / "no.env")
    services = build_services(settings)

    assert isinstance(services, CaseService)
    assert services.ticket_reading_required is True
    assert services._deps.ticket_intake is not None
    assert services._deps.ticket_intake.required is True
    # The composition carries no synthetic label outside preview.
    assert services._deps.ticket_intake.synthetic is False


def test_preview_fake_labels_every_output_synthetic() -> None:
    from parkingbreaker.config_fakes import SYNTHETIC_VISION_MODEL, SyntheticVisionLlm
    from parkingbreaker.llm import ImageAttachment
    from parkingbreaker.ticket_vision import READING_SCHEMA, review_schema

    client = SyntheticVisionLlm()
    image = ImageAttachment(media_type="image/png", data_base64=b64(FRONT))
    data, usage = client.complete_json(
        "synthetic system", "synthetic user", READING_SCHEMA, images=[image]
    )
    assert usage.cost_cents == 0
    assert isinstance(data, dict)
    assert all(item == {"state": "not_shown", "value": None} for item in data.values())
    review_data, _usage = client.complete_json(
        "synthetic system",
        "synthetic user",
        review_schema(["citation_number"]),
        images=[image],
    )
    assert review_data == {"citation_number": {"comparison": "not_shown", "visible_text": None}}
    assert SYNTHETIC_VISION_MODEL == "synthetic-vision-fixture"
