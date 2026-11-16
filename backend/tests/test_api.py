"""Behavioral contract checks via ASGI: no sockets, providers or network."""

import base64
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch
from uuid import UUID

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.api.auth import FakeBearerVerifier
from parkingbreaker.api.fakes import FakeServices
from parkingbreaker.case_service import CaseService
from parkingbreaker.case_state import HardStop, StopFamily
from parkingbreaker.config import ProviderMode, Settings
from test_blob_store import png

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
ALICE = {"Authorization": "Bearer fake-alice"}
BOB = {"Authorization": "Bearer fake-bob"}
WORKER = {"Authorization": "Bearer fake-worker"}


def scripted_approval(adopted_sha256: str) -> dict[str, str]:
    """Synthetic script inputs; FakeServices does not qualify an actual packet."""
    return {
        "adopted_sha256": adopted_sha256,
        "packet_sha256": "0" * 64,
        "envelope_sha256": "0" * 64,
    }


INTAKE: dict[str, Any] = {
    "rental_company_paid": "NOT_RENTAL",
    "ticket": {
        "citation_number": "DEMO-12345",
        "license_plate": "DEMO123",
        "license_plate_state": "CA",
        "issuer_hint": "Demo Municipal Parking Agency",
        "issued_city": "Demo City",
        "issued_state": "CA",
        "violation_code": "DEMO-7",
        "violation_description": "Meter expired",
        "issue_date": "2026-10-14",
        "stage": "first-level review",
    },
    "customer_position": {"statement": "I paid for parking and have the receipt."},
    "contact": {
        "name": "Alex Example",
        "phone": "415-555-0100",
        "email": "alex@example.test",
        "mailing_address": {
            "line1": "123 Example Street",
            "city": "Demo City",
            "state": "CA",
            "zip_code": "90000",
        },
    },
    "photo_refs": ["fake://workspace/receipt"],
}
STOP = s.Stop(
    case_id="replaced",
    family=StopFamily.NOT_YET_VERIFIED,
    hard_stop=HardStop.S2,
    reason_key="contest_destination_unverified",
    message="We could not verify where this contest must be mailed. You were not charged.",
)


@pytest.fixture
async def preview() -> AsyncIterator[tuple[AsyncClient, FakeServices]]:
    services = FakeServices(clock=lambda: NOW)
    async with AsyncClient(
        transport=ASGITransport(app=create_app(services)), base_url="http://preview.test"
    ) as client:
        yield client, services


async def create_case(client: AsyncClient) -> tuple[str, str]:
    workspace = await client.post("/workspaces", headers=ALICE, json={"notes": "Saved notes"})
    assert workspace.status_code == 201
    workspace_id = workspace.json()["workspace_id"]
    receipt = await client.post(f"/workspaces/{workspace_id}/intake", headers=ALICE, json=INTAKE)
    assert receipt.status_code == 200
    return workspace_id, receipt.json()["case_id"]


async def step(client: AsyncClient, case_id: str, step_id: str) -> dict[str, Any]:
    response = await client.post(
        f"/internal/cases/{case_id}/step", headers=WORKER, json={"step_id": step_id}
    )
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


async def ready_draft(client: AsyncClient, case_id: str) -> dict[str, Any]:
    await client.put(f"/cases/{case_id}/tier", headers=ALICE, json={"tier": "CERTIFIED"})
    await client.post(
        f"/cases/{case_id}/authorization",
        headers=ALICE,
        json={"terms_accepted": True, "payment_method_id": "pm_card_visa"},
    )
    await step(client, case_id, "authorization-confirmed")
    await step(client, case_id, "research-question")
    response = await client.post(
        f"/cases/{case_id}/answers",
        headers=ALICE,
        json={
            "question_id": "VEHICLE_RELATIONSHIP",
            "answer": "REGISTERED_OWNER",
        },
    )
    assert response.status_code == 200
    await step(client, case_id, "research-draft")
    return (await client.get(f"/cases/{case_id}/draft", headers=ALICE)).json()  # type: ignore[no-any-return]


DESTINATION_EXAMPLES: list[tuple[str, str, dict[str, Any] | None, int]] = [
    ("GET", "/disclosure", None, 200),
    (
        "POST",
        "/address",
        {
            "recipient": "Parking Review Office",
            "line1": "1 Example Plaza",
            "city": "Demo City",
            "state": "CA",
            "zip_code": "90000",
        },
        409,
    ),
    (
        "POST",
        "/consent",
        {
            "address_revision": 1,
            "address_fingerprint": "a" * 64,
            "disclosure_sha256": "b" * 64,
            "accepted": True,
        },
        409,
    ),
    ("POST", "/decline", None, 409),
]


async def contract_examples() -> list[dict[str, Any]]:
    """Executed request/response transcript used verbatim in api-contract.md."""
    examples: list[dict[str, Any]] = []
    services = FakeServices(clock=lambda: NOW)
    with patch("parkingbreaker.api.fakes.uuid4", return_value=UUID(int=1)):
        await _contract_examples(services, examples)
        async with AsyncClient(
            transport=ASGITransport(
                app=create_app(FakeServices(clock=lambda: NOW, pre_hold_stop=STOP))
            ),
            base_url="http://preview.test",
        ) as client:
            _, case = await create_case(client)
            path = f"/cases/{case}/pre-hold"
            response = await client.get(path, headers=ALICE)
            assert response.status_code == 200
            examples.append(
                {
                    "method": "GET",
                    "path": path,
                    "request": None,
                    "status": 200,
                    "response": response.json(),
                }
            )
            # The preview documents the destination-choice routes: the disclosure is real,
            # the choice itself needs the durable service.
            destination = f"/cases/{case}/destination"
            for method, suffix, body, expected in DESTINATION_EXAMPLES:
                response = await client.request(
                    method, destination + suffix, headers=ALICE, json=body
                )
                assert response.status_code == expected, response.text
                examples.append(
                    {
                        "method": method,
                        "path": destination + suffix,
                        "request": body,
                        "status": expected,
                        "response": response.json(),
                    }
                )
    return examples


async def _contract_examples(
    services: FakeServices, examples: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    async with AsyncClient(
        transport=ASGITransport(app=create_app(services)), base_url="http://preview.test"
    ) as client:

        async def call(
            method: str,
            path: str,
            body: dict[str, Any] | None = None,
            *,
            worker: bool = False,
            expected_status: int | None = None,
        ) -> dict[str, Any]:
            response = await client.request(
                method, path, headers=WORKER if worker else ALICE, json=body
            )
            if expected_status is None:
                assert response.status_code in {200, 201, 400}, response.text
            else:
                assert response.status_code == expected_status, response.text
            result: dict[str, Any] = response.json()
            examples.append(
                {
                    "method": method,
                    "path": path,
                    "request": body,
                    "status": response.status_code,
                    "response": result,
                }
            )
            return result

        await call("GET", "/health")
        workspace = (await call("POST", "/workspaces", {"notes": "Saved notes"}))["workspace_id"]
        await call(
            "PUT",
            f"/workspaces/{workspace}/intake-form",
            {
                "expected_revision": 1,
                "form": {"values": {"story": "Saved partial input"}, "step": 2},
            },
        )
        await call(
            "POST",
            f"/workspaces/{workspace}/photos",
            {"content_type": "image/png", "data_base64": base64.b64encode(png()).decode()},
        )
        case = (await call("POST", f"/workspaces/{workspace}/intake", INTAKE))["case_id"]
        await call("GET", f"/workspaces/{workspace}")
        await call("GET", "/auth/appeal")
        await call("GET", f"/cases/{case}/pre-hold")
        # Preview refuses free-beta entitlement reads and acceptance durably.
        refused_beta_offer = await call("GET", f"/cases/{case}/beta-offer", expected_status=409)
        assert refused_beta_offer == {"detail": "beta_requires_durable_service"}
        refused_beta = await call(
            "POST",
            f"/cases/{case}/beta-acceptance",
            {"terms_accepted": True, "disclosure_sha256": "a" * 64},
            expected_status=409,
        )
        assert refused_beta == {"detail": "beta_requires_durable_service"}
        await call("PUT", f"/cases/{case}/tier", {"tier": "CERTIFIED"})
        await call(
            "POST",
            f"/cases/{case}/authorization",
            {"terms_accepted": True, "payment_method_id": "pm_card_visa"},
        )
        await call(
            "POST", f"/internal/cases/{case}/step", {"step_id": "hold-confirmed"}, worker=True
        )
        await call(
            "POST", f"/internal/cases/{case}/step", {"step_id": "research-question"}, worker=True
        )
        await call("GET", f"/cases/{case}/status")
        await call(
            "PUT",
            f"/cases/{case}/required-copies",
            {"research_sha256": "0" * 64, "expected_revision": 0, "selections": []},
            expected_status=409,
        )
        await call(
            "GET",
            f"/cases/{case}/required-copies/{'0' * 64}/pages/1",
            expected_status=409,
        )
        await call(
            "POST",
            f"/cases/{case}/answers",
            {"question_id": "VEHICLE_RELATIONSHIP", "answer": "REGISTERED_OWNER"},
        )
        await call(
            "POST", f"/internal/cases/{case}/step", {"step_id": "research-draft"}, worker=True
        )
        draft = await call("GET", f"/cases/{case}/draft")
        draft = await call(
            "PUT",
            f"/cases/{case}/draft",
            {
                "text": draft["text"] + "\nReceipt enclosed.",
                "expected_sha256": draft["text_sha256"],
            },
        )
        adoption = await call(
            "POST",
            f"/cases/{case}/adoption",
            {
                "text": draft["text"],
                "text_sha256": draft["text_sha256"],
            },
        )
        refused_preview = await call(
            "POST",
            f"/cases/{case}/packet-preview",
            {"adopted_sha256": adoption["adopted_sha256"]},
            expected_status=409,
        )
        assert refused_preview == {"detail": "packet_preview_requires_durable_service"}
        await call("POST", f"/cases/{case}/approval", scripted_approval(adoption["adopted_sha256"]))
        await call(
            "POST", f"/internal/cases/{case}/step", {"step_id": "fake-mail-accepted"}, worker=True
        )
        await call("GET", f"/cases/{case}/tracking")
        refused_record = await call("GET", f"/cases/{case}/mailing-record", expected_status=409)
        assert refused_record == {"detail": "mailing_record_requires_durable_service"}
        await call("POST", "/internal/dispatch", worker=True)
        await call("POST", "/webhooks/stripe", {})
        await call("POST", "/webhooks/lob", {})
        await call(
            "POST",
            f"/cases/{case}/notices",
            {
                "request_id": "contract-notice",
                "pages": [{"role": "page", "photo_ref": "photo_" + "a" * 64}],
            },
            expected_status=409,
        )
        await call("GET", f"/cases/{case}/notices", expected_status=409)
        await call("GET", f"/cases/{case}/notices/{'0' * 32}/pages/1", expected_status=409)
        # The ticket-reading path is actual supported customer API, so the generated
        # contract documents its four authenticated routes. The preview capability is
        # honestly disabled and the three write commands refuse durably.
        await call("GET", f"/workspaces/{workspace}/ticket-reading")
        for suffix in ("ticket-reading", "ticket-review"):
            await call(
                "POST",
                f"/workspaces/{workspace}/{suffix}",
                {"request_id": f"contract-{suffix}", "expected_revision": 2},
                expected_status=409,
            )
        await call(
            "POST",
            f"/workspaces/{workspace}/ticket-confirmation",
            {
                "command_id": "contract-ticket-confirmation",
                "expected_revision": 2,
                "review_request_id": "contract-ticket-review",
                "expected_confirmation_generation": 0,
                "accuracy_acknowledged": True,
                "resolved_fields": {},
            },
            expected_status=409,
        )
    return examples


async def test_documented_examples_match_live_routes() -> None:
    contract = Path(__file__).parent / "fixtures/api-contract-launch1.json"
    examples = json.loads(contract.read_text())
    assert examples == (await contract_examples())
    schema = create_app().openapi()
    documented = {
        (
            example["method"].lower(),
            re.sub(
                r"ws_[a-f0-9]+",
                "{workspace_id}",
                re.sub(
                    r"/required-copies/[a-f0-9]{64}/pages/\d+$",
                    "/required-copies/{requirement_id}/pages/{page_number}",
                    re.sub(
                        r"/notices/[a-f0-9]{32}/pages/\d+$",
                        "/notices/{notice_id}/pages/{page_number}",
                        re.sub(r"case_[a-f0-9]+", "{case_id}", example["path"]),
                    ),
                ),
            ),
        )
        for example in examples
    }
    expected = {(method, path) for path, methods in schema["paths"].items() for method in methods}
    assert documented == expected


CUSTOMER_ROUTES = [
    ("GET", "/pre-hold", None),
    ("PUT", "/tier", {"tier": "CERTIFIED"}),
    ("POST", "/authorization", {"terms_accepted": True, "payment_method_id": "pm_card_visa"}),
    ("GET", "/beta-offer", None),
    (
        "POST",
        "/beta-acceptance",
        {"terms_accepted": True, "disclosure_sha256": "a" * 64},
    ),
    ("GET", "/status", None),
    ("POST", "/answers", {"question_id": "VEHICLE_RELATIONSHIP", "answer": "OTHER"}),
    ("GET", "/draft", None),
    ("PUT", "/draft", {"text": "Edit", "expected_sha256": "a" * 64}),
    ("POST", "/adoption", {"text": "Draft", "text_sha256": "a" * 64}),
    ("POST", "/packet-preview", {"adopted_sha256": "a" * 64}),
    ("POST", "/approval", scripted_approval("a" * 64)),
    ("GET", "/tracking", None),
    ("GET", "/mailing-record", None),
    ("GET", "/destination/disclosure", None),
    (
        "POST",
        "/destination/address",
        {
            "recipient": "Office",
            "line1": "1 Main",
            "city": "Town",
            "state": "CA",
            "zip_code": "90000",
        },
    ),
    (
        "POST",
        "/destination/consent",
        {
            "address_revision": 1,
            "address_fingerprint": "a" * 64,
            "disclosure_sha256": "b" * 64,
            "accepted": True,
        },
    ),
    ("POST", "/destination/decline", None),
]


@pytest.mark.parametrize(("method", "suffix", "body"), CUSTOMER_ROUTES)
async def test_every_case_route_hides_other_customers(
    preview: tuple[AsyncClient, FakeServices], method: str, suffix: str, body: dict[str, Any] | None
) -> None:
    client, _ = preview
    _, case = await create_case(client)
    other = await client.request(method, f"/cases/{case}{suffix}", headers=BOB, json=body)
    missing = await client.request(method, f"/cases/missing{suffix}", headers=BOB, json=body)
    assert other.status_code == missing.status_code == 404
    assert other.json() == missing.json() == {"detail": "not_found"}
    assert (await client.request(method, f"/cases/{case}{suffix}", json=body)).status_code == 401


async def test_workspace_ownership_and_worker_auth(
    preview: tuple[AsyncClient, FakeServices],
) -> None:
    client, _ = preview
    workspace, case = await create_case(client)
    assert (await client.get(f"/workspaces/{workspace}", headers=BOB)).status_code == 404
    assert (
        await client.post(f"/workspaces/{workspace}/intake", headers=BOB, json=INTAKE)
    ).status_code == 404
    assert (await client.post("/workspaces", json={})).status_code == 401
    for headers in ({}, ALICE, BOB):
        assert (
            await client.post(
                f"/internal/cases/{case}/step", headers=headers, json={"step_id": "x"}
            )
        ).status_code == 401
    assert (await client.get(f"/cases/{case}/status", headers=WORKER)).status_code == 401
    assert (
        await client.post("/internal/cases/missing/step", headers=WORKER, json={"step_id": "x"})
    ).status_code == 404


async def test_fake_bearer_verifier() -> None:
    verifier = FakeBearerVerifier()
    assert verifier.verify("fake-alice") is not None
    assert verifier.verify("fake-bob") is not None
    assert verifier.verify("arbitrary-customer-id") is None


async def test_pending_hold_countdown_and_replays(
    preview: tuple[AsyncClient, FakeServices],
) -> None:
    client, services = preview
    services.standard_offered = True  # exercises the post-launch $25 tier
    workspace, case = await create_case(client)
    first = await client.post(f"/workspaces/{workspace}/intake", headers=ALICE, json=INTAKE)
    assert first.json()["case_id"] == case
    assert len(services.cases) == 1
    assert (
        await client.post(
            f"/cases/{case}/authorization",
            headers=ALICE,
            json={"terms_accepted": True, "payment_method_id": "pm_card_visa"},
        )
    ).status_code == 409
    await client.put(f"/cases/{case}/tier", headers=ALICE, json={"tier": "STANDARD"})
    path = f"/cases/{case}/authorization"
    pending = await client.post(
        path, headers=ALICE, json={"terms_accepted": True, "payment_method_id": "pm_card_visa"}
    )
    assert pending.json()["amount_cents"] == 2500
    assert pending.json()["window_started_at"] is None
    assert (
        await client.post(
            path, headers=ALICE, json={"terms_accepted": True, "payment_method_id": "pm_card_visa"}
        )
    ).json() == pending.json()
    assert (
        await client.put(f"/cases/{case}/tier", headers=ALICE, json={"tier": "CERTIFIED"})
    ).status_code == 409
    confirmed = await step(client, case, "confirm")
    assert confirmed["payment_state"] == "AUTHORIZED"
    assert confirmed["seconds_remaining"] == 7200
    services.clock = lambda: NOW + timedelta(hours=1, minutes=56)
    assert (await step(client, case, "confirm"))["window_status"] == "WARNING"
    status = (await client.get(f"/cases/{case}/status", headers=ALICE)).json()
    assert status["window_started_at"] == confirmed["window_started_at"]
    services.clock = lambda: NOW + timedelta(hours=2)
    expired = (await client.get(f"/cases/{case}/status", headers=ALICE)).json()
    assert expired["state"] == "STOPPED_NO_CHARGE"
    assert expired["payment_state"] == "RELEASED"
    assert expired["stop"]["reason_key"] == "window_expired"
    assert (
        await client.post(
            path, headers=ALICE, json={"terms_accepted": True, "payment_method_id": "pm_card_visa"}
        )
    ).status_code == 409
    assert (await client.get(f"/workspaces/{workspace}", headers=ALICE)).json()["intake"][
        "customer_position"
    ]["statement"] == INTAKE["customer_position"]["statement"]


async def test_edit_invalidates_adoption_and_capture_follows_acceptance(
    preview: tuple[AsyncClient, FakeServices],
) -> None:
    client, _ = preview
    _, case = await create_case(client)
    draft = await ready_draft(client, case)
    adoption_body = {"text": draft["text"], "text_sha256": draft["text_sha256"]}
    adopted = (
        await client.post(f"/cases/{case}/adoption", headers=ALICE, json=adoption_body)
    ).json()
    assert (
        await client.post(f"/cases/{case}/adoption", headers=ALICE, json=adoption_body)
    ).json() == adopted
    edit = {"text": draft["text"] + " Revised.", "expected_sha256": draft["text_sha256"]}
    changed = (await client.put(f"/cases/{case}/draft", headers=ALICE, json=edit)).json()
    assert changed["adopted_sha256"] is None
    assert (await client.put(f"/cases/{case}/draft", headers=ALICE, json=edit)).json() == changed
    assert (
        await client.post(
            f"/cases/{case}/approval",
            headers=ALICE,
            json=scripted_approval(adopted["adopted_sha256"]),
        )
    ).status_code == 409
    assert (
        await client.post(f"/cases/{case}/adoption", headers=ALICE, json=adoption_body)
    ).status_code == 409
    await client.post(
        f"/cases/{case}/adoption",
        headers=ALICE,
        json={"text": changed["text"], "text_sha256": changed["text_sha256"]},
    )
    approved = await client.post(
        f"/cases/{case}/approval", headers=ALICE, json=scripted_approval(changed["text_sha256"])
    )
    assert approved.json()["state"] == "FROZEN"
    assert approved.json()["payment_state"] == "AUTHORIZED"
    mailed = await step(client, case, "mail")
    assert mailed["state"] == "IN_TRANSIT"
    assert mailed["payment_state"] == "CAPTURED"
    assert mailed["seconds_remaining"] is None
    assert mailed["window_status"] is None
    assert (await step(client, case, "mail")) == mailed
    tracking = (await client.get(f"/cases/{case}/tracking", headers=ALICE)).json()
    assert len(tracking["events"]) == 1
    assert tracking["events"][0]["event"] == "accepted"
    assert "not delivery" in tracking["events"][0]["description"]
    repeated_approval = await client.post(
        f"/cases/{case}/approval", headers=ALICE, json=scripted_approval(changed["text_sha256"])
    )
    assert repeated_approval.json() == mailed
    repeated_adoption = await client.post(
        f"/cases/{case}/adoption",
        headers=ALICE,
        json={"text": changed["text"], "text_sha256": changed["text_sha256"]},
    )
    assert repeated_adoption.status_code == 200
    assert repeated_adoption.json()["adopted_sha256"] == changed["text_sha256"]


async def test_stop_before_hold_preserves_work() -> None:
    async with AsyncClient(
        transport=ASGITransport(
            app=create_app(FakeServices(clock=lambda: NOW, pre_hold_stop=STOP))
        ),
        base_url="http://preview.test",
    ) as client:
        workspace, case = await create_case(client)
        stop = s.Stop.model_validate(
            (await client.get(f"/cases/{case}/pre-hold", headers=ALICE)).json()
        )
        assert stop.kind == "stop"
        assert stop.no_charge
        assert stop.reason_key == "contest_destination_unverified"
        assert (
            await client.post(
                f"/cases/{case}/authorization",
                headers=ALICE,
                json={"terms_accepted": True, "payment_method_id": "pm_card_visa"},
            )
        ).status_code == 409
        status = (await client.get(f"/cases/{case}/status", headers=ALICE)).json()
        assert status["state"] == "STOPPED_NO_CHARGE"
        assert status["payment_state"] is None
        assert (await client.get(f"/workspaces/{workspace}", headers=ALICE)).json()[
            "notes"
        ] == "Saved notes"


async def test_certified_only_offer_rejects_standard() -> None:
    async with AsyncClient(
        transport=ASGITransport(
            app=create_app(FakeServices(clock=lambda: NOW, standard_offered=False))
        ),
        base_url="http://preview.test",
    ) as client:
        _, case = await create_case(client)
        offer = (await client.get(f"/cases/{case}/pre-hold", headers=ALICE)).json()
        assert [tier["amount_cents"] for tier in offer["tiers"]] == [2999]
        assert (
            await client.put(f"/cases/{case}/tier", headers=ALICE, json={"tier": "STANDARD"})
        ).status_code == 409


async def test_logs_allowlist_no_customer_content(
    preview: tuple[AsyncClient, FakeServices],
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _ = preview
    caplog.set_level(logging.INFO, logger="parkingbreaker.api.access")
    _, case = await create_case(client)
    await client.get(f"/cases/{case}/status?secret=PRIVATE_QUERY", headers=ALICE)
    await client.post(
        "/workspaces", headers=ALICE, json={"notes": "PRIVATE_BODY", "extra": "PRIVATE_EXTRA"}
    )
    await client.post("/workspaces", headers=ALICE, content="PRIVATE_MALFORMED_JSON")
    await client.get(
        "/PRIVATE_PATH?email=PRIVATE_EMAIL", headers={"Authorization": "Bearer PRIVATE_TOKEN"}
    )
    records = [r for r in caplog.records if r.name == "parkingbreaker.api.access"]
    assert records
    for record in records:
        assert set(json.loads(record.getMessage())) == {"method", "route", "status", "duration_ms"}
        assert record.exc_info is None
    logged = "\n".join(r.getMessage() for r in records)
    for content in ("PRIVATE", "fake-alice", "DEMO-12345", "Alex Example", "Saved notes", case):
        assert content not in logged
    assert "/cases/{case_id}/status" in logged
    assert "<unmatched>" in logged


@pytest.mark.parametrize("family", list(StopFamily))
async def test_each_stop_family_is_a_no_charge_result(family: StopFamily) -> None:
    stop = STOP.model_copy(
        update={
            "family": family,
            "hard_stop": HardStop.S3
            if family in {StopFamily.PROCESS_INCOMPATIBLE, StopFamily.NOT_YET_VERIFIED}
            else None,
            "official_quote": "Demo rule: the original agency form is required."
            if family is StopFamily.PROCESS_INCOMPATIBLE
            else None,
        }
    )
    async with AsyncClient(
        transport=ASGITransport(
            app=create_app(FakeServices(clock=lambda: NOW, pre_hold_stop=stop))
        ),
        base_url="http://preview.test",
    ) as client:
        workspace, case = await create_case(client)
        result = await client.get(f"/cases/{case}/pre-hold", headers=ALICE)
        assert result.status_code == 200
        assert result.json() == stop.model_copy(update={"case_id": case}).model_dump(mode="json")
        receipt = await client.post(f"/workspaces/{workspace}/intake", headers=ALICE, json=INTAKE)
        assert receipt.json()["state"] == "STOPPED_NO_CHARGE"


@pytest.mark.parametrize(
    "authorization",
    ["", "Basic fake-alice", "Bearer arbitrary-id", "Bearer fake-alice extra"],
)
async def test_invalid_bearers_cannot_create_a_workspace(
    preview: tuple[AsyncClient, FakeServices], authorization: str
) -> None:
    client, services = preview
    response = await client.post("/workspaces", headers={"Authorization": authorization}, json={})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert not services.workspaces


async def test_rejects_free_text_answers_and_stale_edits(
    preview: tuple[AsyncClient, FakeServices],
) -> None:
    client, _ = preview
    _, case = await create_case(client)
    response = await client.post(
        f"/cases/{case}/answers",
        headers=ALICE,
        json={"question_id": "VEHICLE_RELATIONSHIP", "answer": "invent an exemption"},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_request"}
    draft = await ready_draft(client, case)
    response = await client.put(
        f"/cases/{case}/draft", headers=ALICE, json={"text": "Changed", "expected_sha256": "a" * 64}
    )
    assert response.status_code == 409
    assert (await client.get(f"/cases/{case}/draft", headers=ALICE)).json() == draft


async def test_launch_offers_certified_only(preview: tuple[AsyncClient, FakeServices]) -> None:
    client, _services = preview
    _workspace, case = await create_case(client)
    offer = (await client.get(f"/cases/{case}/pre-hold", headers=ALICE)).json()
    assert [tier["tier"] for tier in offer["tiers"]] == ["CERTIFIED"]
    assert offer["tiers"][0]["amount_cents"] == 2999
    chosen = await client.put(f"/cases/{case}/tier", headers=ALICE, json={"tier": "STANDARD"})
    assert chosen.status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"terms_accepted": True},
        {"terms_accepted": True, "payment_method_id": "4242424242424242"},
        {"terms_accepted": True, "payment_method_id": "pm_ok", "card_number": "4242424242424242"},
    ],
)
async def test_authorization_accepts_only_a_stripe_payment_method_id(
    preview: tuple[AsyncClient, FakeServices], body: dict[str, object]
) -> None:
    client, _services = preview
    _workspace, case = await create_case(client)
    await client.put(f"/cases/{case}/tier", headers=ALICE, json={"tier": "CERTIFIED"})
    response = await client.post(f"/cases/{case}/authorization", headers=ALICE, json=body)
    assert response.status_code == 422
    assert "4242" not in response.text


@pytest.mark.parametrize("mode", ["preview", "fake"])
async def test_health_identifies_the_configured_provider_mode(
    mode: Literal["preview", "fake", "test", "live"],
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=create_app(provider_mode=mode)), base_url="http://check.test"
    ) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["mode"] == mode
    # This app has no service wiring, so deterministic authorship is not claimed.
    assert body["customer_authored_mode"] is False
    assert body["customer_authored_mode_version"] is None


async def test_health_reports_customer_authored_mode_when_active(tmp_path: Path) -> None:
    from parkingbreaker.config import ProviderMode, Settings, build_services
    from test_config import SECRET

    service = build_services(Settings(ProviderMode.FAKE, tmp_path, SECRET, {}))
    assert isinstance(service, CaseService)
    async with AsyncClient(
        transport=ASGITransport(app=create_app(service, worker_secret=SECRET)),
        base_url="http://check.test",
    ) as client:
        response = await client.get("/health")
    body = response.json()
    assert body["customer_authored_mode"] is True
    assert body["customer_authored_mode_version"] == "customer-authored-v1"
    # Safe indicator only: no secrets, credentials or customer data.
    assert set(body) == {
        "status",
        "mode",
        "customer_authored_mode",
        "customer_authored_mode_version",
        "build_commit",
        "policy_hashes",
    }
    assert isinstance(body["policy_hashes"].get("free_beta_policy"), str)


@pytest.mark.parametrize("mode", ["test", "live"])
def test_direct_production_app_cannot_fall_back_to_preview_identity(
    mode: Literal["test", "live"],
) -> None:
    with pytest.raises(RuntimeError, match=r"^customer_auth_not_configured$"):
        create_app(provider_mode=mode)


@pytest.mark.parametrize("mode", [ProviderMode.TEST, ProviderMode.LIVE])
def test_environment_app_rejects_production_modes_before_building_services(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: ProviderMode
) -> None:
    import parkingbreaker.config as config
    from parkingbreaker.api.app import app_from_environment

    settings = Settings(mode, tmp_path, "worker-secret-test-value", {})
    monkeypatch.setattr(config, "load_settings", lambda: settings)

    def forbidden_build(_settings: Settings) -> None:
        pytest.fail("production mode reached provider/store construction")

    monkeypatch.setattr(config, "build_services", forbidden_build)
    with pytest.raises(RuntimeError, match=r"^customer_auth_not_configured$") as caught:
        app_from_environment()
    assert str(caught.value) == "customer_auth_not_configured"


@pytest.mark.parametrize("mode", [ProviderMode.PREVIEW, ProviderMode.FAKE])
async def test_environment_app_keeps_preview_and_fake_build_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: ProviderMode
) -> None:
    import parkingbreaker.config as config
    from parkingbreaker.api.app import app_from_environment

    settings = Settings(mode, tmp_path, "worker-secret-test-value", {})
    built: list[ProviderMode] = []

    def build(_settings: Settings) -> FakeServices | None:
        built.append(_settings.mode)
        return None if mode is ProviderMode.PREVIEW else FakeServices()

    monkeypatch.setattr(config, "load_settings", lambda: settings)
    monkeypatch.setattr(config, "build_services", build)
    async with AsyncClient(
        transport=ASGITransport(app=app_from_environment()), base_url="http://check.test"
    ) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["mode"] == mode.value
    assert body["customer_authored_mode"] is False
    assert built == [mode]


@pytest.mark.parametrize(
    "path", ["/webhooks/stripe", "/workspaces/missing/intake", "/internal/dispatch"]
)
async def test_request_stream_bound_precedes_json_provider_and_case_processing(path: str) -> None:
    from parkingbreaker.api.app import MAX_JSON_REQUEST_BYTES

    reads: list[int] = []

    async def chunks() -> AsyncIterator[bytes]:
        for number in range(20):
            reads.append(number)
            yield b"x" * (MAX_JSON_REQUEST_BYTES // 2)

    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://limit.test"
    ) as client:
        response = await client.post(
            path, content=chunks(), headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 413
    assert response.json() == {"detail": "request_too_large"}
    assert len(reads) == 3


async def test_declared_oversize_is_refused_without_reading_body() -> None:
    from parkingbreaker.api.app import MAX_JSON_REQUEST_BYTES

    async def forbidden() -> AsyncIterator[bytes]:
        pytest.fail("oversized declared request body was read")
        yield b""

    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://limit.test"
    ) as client:
        response = await client.post(
            "/webhooks/lob",
            content=forbidden(),
            headers={"Content-Length": str(MAX_JSON_REQUEST_BYTES + 1)},
        )
    assert response.status_code == 413


async def test_photo_route_has_its_existing_larger_bounded_budget() -> None:
    from parkingbreaker.api.app import MAX_JSON_REQUEST_BYTES

    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://limit.test"
    ) as client:
        response = await client.post(
            "/workspaces/missing/photos",
            json={"content_type": "image/png", "data_base64": "A" * (MAX_JSON_REQUEST_BYTES + 1)},
            headers=ALICE,
        )
    assert response.status_code == 404
