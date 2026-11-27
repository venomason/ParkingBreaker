from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker import config
from parkingbreaker.api.app import create_app
from parkingbreaker.case_service import CaseService
from parkingbreaker.config import ProviderMode, build_services, load_settings
from parkingbreaker.config_fakes import FixtureLlm
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import LlmProviderError, Usage
from parkingbreaker.oplog.ai_journal import (
    AiCall,
    AiCallStatus,
    AiWorker,
)
from test_config import ALICE, WORKER, held_case
from test_intake_call_log import intake, submit


class ObservedFixtureLlm(FixtureLlm):
    instances: ClassVar[list[ObservedFixtureLlm]] = []

    def __init__(self, html: str) -> None:
        super().__init__(html)
        self.observed: list[tuple[str, int | None]] = []
        self.failure: Exception | None = None
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
        assert isinstance(properties, dict)
        if "findings" in properties:
            kind = "research"
        elif "sentences" in properties:
            kind = "draft"
        else:
            kind = "intake"
        self.observed.append((kind, max_cost_cents))
        if self.failure is not None and kind == "research":
            raise self.failure
        result, _usage = super().complete_json(system, user, schema, max_cost_cents=max_cost_cents)
        return result, Usage(13, 5, 2)


@pytest.fixture
async def fake_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[CaseService, AsyncClient, ObservedFixtureLlm]]:
    ObservedFixtureLlm.instances.clear()
    monkeypatch.setattr(config, "FixtureLlm", ObservedFixtureLlm)
    settings = load_settings(
        {
            "PB_PROVIDER_MODE": "fake",
            "PB_WORKER_SECRET": "worker-secret-0123456789",
            "PB_DATA_DIR": str(tmp_path),
        },
        tmp_path / "missing.env",
    )
    assert settings.mode is ProviderMode.FAKE
    service = build_services(settings)
    assert isinstance(service, CaseService)
    llm = ObservedFixtureLlm.instances[0]
    async with AsyncClient(
        transport=ASGITransport(app=create_app(service, worker_secret=settings.worker_secret)),
        base_url="http://offline.test",
    ) as client:
        yield service, client, llm


async def research_and_draft(client: AsyncClient, case: str) -> None:
    result = await client.post(
        f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
    )
    assert result.status_code == 200, result.text
    state = result.json()["state"]
    if state == "NEEDS_CUSTOMER":
        answer = await client.post(
            f"/cases/{case}/answers",
            headers=ALICE,
            json={"question_id": "VEHICLE_RELATIONSHIP", "answer": "REGISTERED_OWNER"},
        )
        assert answer.status_code == 200, answer.text
        state = answer.json()["state"]
    assert state == "DRAFTING"
    draft = await client.post(
        f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "draft"}
    )
    assert draft.status_code == 200, draft.text
    assert draft.json()["state"] == "AWAITING_ADOPTION"


def case_token_for(service: CaseService, case: str) -> str:
    record = service._store.get_intake(case)
    assert record is not None
    assert record.case_token is not None
    return record.case_token


@pytest.mark.anyio
async def test_runtime_research_and_drafting_log_one_row_per_provider_call(
    fake_runtime: tuple[CaseService, AsyncClient, ObservedFixtureLlm],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client, llm = fake_runtime
    case = await held_case(client)
    case_token = case_token_for(service, case)
    observed = list(llm.observed)
    rows = service._store.ai_calls(case)
    await research_and_draft(client, case)
    assert llm.observed == observed
    assert len(rows) == len(observed)
    assert [row.worker for row in rows] == [
        AiWorker.INTAKE_READING if kind == "intake" else AiWorker.RESEARCH for kind, _ in observed
    ]
    assert all(row.model == "offline-fixture" and row.status is AiCallStatus.OK for row in rows)
    assert all((row.input_tokens, row.output_tokens, row.cost_cents) == (13, 5, 2) for row in rows)
    assert [cap for _, cap in observed] == llm.ceilings
    assert all(isinstance(cap, int) and cap >= 0 for _, cap in observed)
    assert {kind for kind, _ in observed} == {"intake", "research"}

    call_count = len(llm.observed)
    replay_research = await client.post(
        f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
    )
    replay_draft = await client.post(
        f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "draft"}
    )
    assert replay_research.status_code == replay_draft.status_code == 200
    assert len(llm.observed) == call_count
    assert service._store.ai_calls(case) == rows

    monkeypatch.setitem(ALICE, "Authorization", "Bearer fake-bob")
    other_case = await held_case(client)
    other_token = case_token_for(service, other_case)
    other_before_calls = len(llm.observed)
    other_step = await client.post(
        f"/internal/cases/{other_case}/step", headers=WORKER, json={"step_id": "research"}
    )
    assert other_step.status_code == 200, other_step.text
    other_state = other_step.json()["state"]
    if other_state == "NEEDS_CUSTOMER":
        answered = await client.post(
            f"/cases/{other_case}/answers",
            headers={"Authorization": "Bearer fake-bob"},
            json={"question_id": "VEHICLE_RELATIONSHIP", "answer": "REGISTERED_OWNER"},
        )
        assert answered.status_code == 200, answered.text
        other_state = answered.json()["state"]
    if other_state == "DRAFTING":
        drafted = await client.post(
            f"/internal/cases/{other_case}/step", headers=WORKER, json={"step_id": "draft"}
        )
        assert drafted.status_code == 200, drafted.text
        other_state = drafted.json()["state"]
    assert other_state == "AWAITING_ADOPTION"
    other_delta = llm.observed[other_before_calls:]
    other_rows = service._store.ai_calls(other_case)
    assert not other_delta
    assert other_rows
    assert all(row.status is AiCallStatus.OK for row in other_rows)
    assert service._store.ai_calls(case) == rows
    assert other_token != case_token


@pytest.mark.anyio
async def test_research_failure_is_logged_with_unknown_usage(
    fake_runtime: tuple[CaseService, AsyncClient, ObservedFixtureLlm],
) -> None:
    service, client, llm = fake_runtime
    llm.failure = LlmProviderError("synthetic provider failure with private detail")
    _, _, receipt = await submit(client, intake())
    case = receipt["case_id"]
    assert receipt["state"] == "STOPPED_NO_CHARGE"
    assert service._store.pending_authorization_attempt(case) is None
    failed = [row for row in service._store.ai_calls(case) if row.worker is AiWorker.RESEARCH]
    assert len(failed) == 1
    assert failed[0].status is AiCallStatus.FALLBACK_AFTER_FAILURE
    assert (failed[0].input_tokens, failed[0].output_tokens, failed[0].cost_cents) == (
        None,
        None,
        None,
    )


@pytest.mark.anyio
async def test_runtime_research_outcome_survives_call_logger_fault(
    fake_runtime: tuple[CaseService, AsyncClient, ObservedFixtureLlm],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client, _llm = fake_runtime
    case = await held_case(client)
    case_token = case_token_for(service, case)
    initial_rows = service._store.ai_calls(case_token)

    def broken_logger(_subject: str, _call: AiCall, _when: datetime) -> object:
        raise OSError("synthetic logger failure")

    monkeypatch.setattr(service._store, "try_append_ai_call", broken_logger)
    success = await client.post(
        f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
    )
    assert success.status_code == 200, success.text
    assert success.json()["state"] in {"DRAFTING", "NEEDS_CUSTOMER"}
    assert service._store.get_research(case) is not None
    assert service._store.ai_calls(case_token) == initial_rows
