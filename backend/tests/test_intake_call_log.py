from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.api.app import create_app
from parkingbreaker.case_service import CaseService
from parkingbreaker.config import ProviderMode, build_services, load_settings
from parkingbreaker.oplog.ai_journal import AiCall, AiCallStatus, AiWorker

ALICE = {"Authorization": "Bearer fake-alice"}
BOB = {"Authorization": "Bearer fake-bob"}
WORKER = "worker-secret-0123456789"


def png_b64() -> str:
    return base64.b64encode(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de0000000b49444154789c636000020000050001a5f645400000000049454e44ae426082"
        )
    ).decode()


def intake(*, age_days: int = 3, issuer_kind: str = "GOVERNMENT") -> dict[str, Any]:
    issued = (date.today() - timedelta(days=age_days)).isoformat()
    return {
        "rental_company_paid": "NOT_RENTAL",
        "ticket": {
            "citation_number": "SYN-LOG-1",
            "license_plate": "SYN123",
            "license_plate_state": "CA",
            "issuer_hint": "San Francisco Municipal Transportation Agency"
            if issuer_kind == "GOVERNMENT"
            else "Synthetic Parking LLC",
            "issued_city": "San Francisco",
            "issued_state": "CA",
            "issuer_kind": issuer_kind,
            "violation_code": "METER",
            "violation_description": "Meter expired",
            "issue_date": issued,
            "stage": "first-level review",
        },
        "customer_position": {"statement": "The meter was broken."},
        "contact": {
            "name": "Example Person",
            "phone": "415-555-0100",
            "email": "person@example.test",
            "mailing_address": {
                "line1": "1 Main Street",
                "city": "San Francisco",
                "state": "CA",
                "zip_code": "94103",
            },
        },
    }


@pytest.fixture
async def fake_service(
    tmp_path: Path,
) -> AsyncIterator[tuple[CaseService, AsyncClient]]:
    settings = load_settings(
        {"PB_PROVIDER_MODE": "fake", "PB_WORKER_SECRET": WORKER, "PB_DATA_DIR": str(tmp_path)},
        tmp_path / "no.env",
    )
    assert settings.mode is ProviderMode.FAKE
    service = build_services(settings)
    assert service is not None
    assert isinstance(service, CaseService)
    async with AsyncClient(
        transport=ASGITransport(app=create_app(service)), base_url="http://fake.test"
    ) as client:
        yield service, client


async def submit(
    client: AsyncClient, body: dict[str, Any], owner: dict[str, str] = ALICE
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    workspace = (await client.post("/workspaces", headers=owner, json={"notes": ""})).json()[
        "workspace_id"
    ]
    photo = (
        await client.post(
            f"/workspaces/{workspace}/photos",
            headers=owner,
            json={"content_type": "image/png", "data_base64": png_b64()},
        )
    ).json()["photo_ref"]
    payload = {**body, "citation_photo_ref": photo, "photo_refs": [photo]}
    response = await client.post(f"/workspaces/{workspace}/intake", headers=owner, json=payload)
    assert response.status_code == 200, response.text
    return workspace, payload, response.json()


@pytest.mark.anyio
async def test_real_fake_config_intake_logs_calls_per_subject_and_replay_is_idempotent(
    fake_service: tuple[CaseService, AsyncClient],
) -> None:
    service, client = fake_service
    workspace, payload, receipt = await submit(client, intake())
    case_id = receipt["case_id"]
    rows = service._store.ai_calls(case_id)
    assert len(rows) >= 2  # issuer assessment plus linked-page assessment in the real fake resolver
    assert all(row.worker is AiWorker.INTAKE_READING for row in rows)
    assert all(row.model == "offline-fixture" and row.status is AiCallStatus.OK for row in rows)
    replay = await client.post(f"/workspaces/{workspace}/intake", headers=ALICE, json=payload)
    assert replay.status_code == 200
    assert replay.json()["case_id"] == case_id
    assert service._store.ai_calls(case_id) == rows
    _workspace2, _payload2, second = await submit(client, intake(), BOB)
    assert second["case_id"] != case_id
    assert service._store.ai_calls(second["case_id"])
    assert service._store.ai_calls(case_id) == rows


@pytest.mark.anyio
async def test_age_and_private_intakes_create_no_intake_model_rows(
    fake_service: tuple[CaseService, AsyncClient],
) -> None:
    service, client = fake_service
    _workspace, _payload, old = await submit(client, intake(age_days=11), BOB)
    assert service._store.ai_calls(old["case_id"]) == ()
    _workspace, _payload, private = await submit(client, intake(issuer_kind="PRIVATE"), ALICE)
    assert service._store.ai_calls(private["case_id"]) == ()


def test_unknown_failure_usage_roundtrips_and_legacy_calls_remain_integer() -> None:

    unknown = AiCall(
        AiWorker.INTAKE_READING, "fake-model", AiCallStatus.FALLBACK_AFTER_FAILURE, None, None, None
    )
    assert AiCall.from_json(unknown.to_json()) == unknown
    old = AiCall.from_json(
        '{"worker":"RESEARCH","model":"offline-fixture","status":"OK","input_tokens":0,"output_tokens":0,"cost_cents":0}'
    )
    assert (old.input_tokens, old.output_tokens, old.cost_cents) == (0, 0, 0)
    for values in ((None, 1, None), (1, None, 1), (None, None, None)):
        with pytest.raises(ValueError, match="unknown usage"):
            AiCall(AiWorker.INTAKE_READING, "fake-model", AiCallStatus.OK, *values)
    with pytest.raises(ValueError, match="unknown usage"):
        AiCall(
            AiWorker.INTAKE_READING,
            "fake-model",
            AiCallStatus.FALLBACK_AFTER_FAILURE,
            None,
            None,
            1,
        )
    for invalid in (True, -1):
        with pytest.raises(ValueError, match="bounded count"):
            AiCall(AiWorker.INTAKE_READING, "fake-model", AiCallStatus.OK, invalid, 0, 0)
