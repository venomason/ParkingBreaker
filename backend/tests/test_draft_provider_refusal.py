from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import ClassVar

import pytest
from httpx2 import ASGITransport, AsyncClient

from paid_draft_support import install_paid_drafter
from parkingbreaker import config
from parkingbreaker.api.app import create_app
from parkingbreaker.case_service import CaseService
from parkingbreaker.config import ProviderMode, Settings, build_services
from parkingbreaker.config_fakes import FixtureLlm
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import LlmBudgetExceeded, LlmProviderError, Usage
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_config import ALICE, SECRET, WORKER, held_case


class RejectDraftLlm(FixtureLlm):
    instances: ClassVar[list[RejectDraftLlm]] = []
    failure: ClassVar[Exception | None] = None
    draft_calls: ClassVar[int] = 0

    def __init__(self, html: str) -> None:
        super().__init__(html)
        type(self).instances.append(self)

    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
    ) -> tuple[JsonValue, Usage]:
        properties = schema.get("properties", {})
        if isinstance(properties, dict) and "sentences" in properties:
            type(self).draft_calls += 1
            failure = type(self).failure
            if failure is not None:
                raise failure
        return super().complete_json(system, user, schema, max_cost_cents=max_cost_cents)


@pytest.fixture
async def refusal_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[CaseService, AsyncClient]]:
    RejectDraftLlm.instances.clear()
    RejectDraftLlm.draft_calls = 0
    RejectDraftLlm.failure = None
    monkeypatch.setattr(config, "FixtureLlm", RejectDraftLlm)
    settings = Settings(ProviderMode.FAKE, tmp_path, SECRET, {})
    service = build_services(settings)
    assert isinstance(service, CaseService)
    install_paid_drafter(service)
    async with AsyncClient(
        transport=ASGITransport(app=create_app(service, worker_secret=SECRET)),
        base_url="http://fake.test",
    ) as client:
        yield service, client


async def get_to_drafting(client: AsyncClient, case: str) -> None:
    response = await client.post(
        f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
    )
    assert response.status_code == 200, response.text
    status = response.json()
    if status["state"] == "NEEDS_CUSTOMER":
        response = await client.post(
            f"/cases/{case}/answers",
            headers=ALICE,
            json={"question_id": "VEHICLE_RELATIONSHIP", "answer": "REGISTERED_OWNER"},
        )
        assert response.status_code == 200, response.text
        status = response.json()
    assert status["state"] == "DRAFTING"


@pytest.mark.parametrize(
    "failure",
    [
        LlmProviderError("private retryable detail", retryable=True),
        LlmProviderError("private nonretryable detail", retryable=False),
        LlmBudgetExceeded("private local ceiling detail"),
    ],
    ids=["provider-retryable", "provider-nonretryable", "local-budget"],
)
@pytest.mark.anyio
async def test_draft_refusal_is_terminal_across_sweeps_and_store_reopen(
    refusal_runtime: tuple[CaseService, AsyncClient],
    failure: Exception,
    tmp_path: Path,
) -> None:
    service, client = refusal_runtime
    RejectDraftLlm.failure = failure
    case = await held_case(client)
    await get_to_drafting(client, case)
    attempted = await client.post(
        f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "draft"}
    )
    assert attempted.status_code == 200, attempted.text
    status = attempted.json()
    assert status["state"] == "STOPPED_NO_CHARGE"
    assert status["stop"]["reason_key"].endswith("drafting_unavailable")
    assert status["stop"]["message"].startswith("We could not prepare your letter right now.")
    assert "private" not in status["stop"]["message"]
    assert "jurisdiction" not in status["stop"]["message"].casefold()
    assert "Nothing was charged." in status["stop"]["message"]
    record = service._store.get_intake(case)
    assert record is not None
    assert record.case_token is not None
    case_record = service._store.get_case(record.case_token)
    assert case_record is not None
    assert case_record.payment_state is PaymentState.RELEASED
    assert "try again" in status["stop"]["message"]
    assert RejectDraftLlm.draft_calls == 1

    for _ in range(3):
        service.advance_due()
    assert RejectDraftLlm.draft_calls == 1
    await client.aclose()
    service._store.close()

    settings = Settings(ProviderMode.FAKE, tmp_path, SECRET, {})
    reopened_store = SqliteTransactionStore(tmp_path / "parkingbreaker.sqlite")
    reopened_services = build_services(settings, store=reopened_store)
    assert isinstance(reopened_services, CaseService)
    reopened = reopened_services
    install_paid_drafter(reopened)
    async with AsyncClient(
        transport=ASGITransport(app=create_app(reopened, worker_secret=SECRET)),
        base_url="http://fake.test",
    ) as restarted_client:
        repeated = await restarted_client.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "draft"}
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["state"] == "STOPPED_NO_CHARGE"
        for _ in range(3):
            reopened.advance_due()
    assert RejectDraftLlm.draft_calls == 1
    reopened_record = reopened._store.get_intake(case)
    assert reopened_record is not None
    assert reopened_record.case_token is not None
    # The drafting provider is no longer composed in production, so there is no drafting
    # AI-call row to assert. The invariant that matters is unchanged: the recorded refusal
    # is terminal across restarts and never repeats the paid call.
    assert RejectDraftLlm.draft_calls == 1
    reopened_case = reopened._store.get_case(reopened_record.case_token)
    assert reopened_case is not None
    assert reopened_case.payment_state is PaymentState.RELEASED
    reopened._store.close()


@pytest.mark.anyio
async def test_call_logger_fault_does_not_repeat_failed_draft(
    refusal_runtime: tuple[CaseService, AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client = refusal_runtime
    RejectDraftLlm.failure = LlmProviderError("private detail", retryable=True)
    case = await held_case(client)
    await get_to_drafting(client, case)

    def logger_fault(_subject: str, _call: object, _now: object) -> bool:
        raise OSError("synthetic call journal failure")

    monkeypatch.setattr(service._store, "try_append_ai_call", logger_fault)
    result = await client.post(
        f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "draft"}
    )
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "STOPPED_NO_CHARGE"
    assert RejectDraftLlm.draft_calls == 1
    for _ in range(3):
        service.advance_due()
    assert RejectDraftLlm.draft_calls == 1


@pytest.mark.anyio
async def test_successful_draft_for_another_case_still_advances(
    refusal_runtime: tuple[CaseService, AsyncClient],
) -> None:
    _service, client = refusal_runtime
    case = await held_case(client)
    await get_to_drafting(client, case)
    response = await client.post(
        f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "draft"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "AWAITING_ADOPTION"
    assert RejectDraftLlm.draft_calls == 1
