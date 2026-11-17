"""OCR decision/timing evidence and guidance-only canaries cannot alter commitments."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from httpx2 import ASGITransport, AsyncClient
from pydantic import ValidationError

import test_ticket_intake_service as h
from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.llm import LlmBudgetExceeded, LlmProviderError, Usage
from parkingbreaker.ticket_intake_service import TicketIntakeCoordinator, TicketIntakeServiceError
from parkingbreaker.ticket_intake_store import ClaimStatus
from parkingbreaker.ticket_observability import (
    OcrFieldReason,
    ReviewCanaryConfig,
    ReviewCanaryPolicy,
    TicketObservabilityConflict,
    build_ocr_observation_from_reader,
    load_review_canary_config,
)
from parkingbreaker.ticket_ocr import RoutedTicketReader
from parkingbreaker.ticket_vision import TicketVisionReader
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_ticket_ocr import payload, routed


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def observation(
    session: s.TicketReviewSession, nonce: str = "a" * 32
) -> s.TicketReviewObservationRequest:
    return s.TicketReviewObservationRequest(
        client_observation_id=nonce,
        event_kind="exposure",
        outcome="observed",
        treatment_variant=session.variant,
        treatment_version=session.effective_treatment_version,
        rendered_variant=session.variant,
        visible_elapsed_ms=20,
        busy_elapsed_ms=5,
        hidden_elapsed_ms=15,
        visibility_changes=2,
        timing_state="closed",
        reading_status="success",
        review_status="missing",
    )


def business_rows(store: SqliteTransactionStore) -> dict[str, list[Any]]:
    database = store._require_open()
    names = [
        row[0]
        for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        if row[0] not in {"pb_ticket_review_sessions", "pb_ticket_review_observations"}
    ]
    return {
        name: database.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall()
        for name in names
    }


def assert_review_does_not_change_commitments(
    store: SqliteTransactionStore,
    coordinator: TicketIntakeCoordinator,
    workspace: str,
) -> None:
    """Called after a real paid case has adopted its exact packet and reached FROZEN."""
    before = business_rows(store)
    coordinator._deps = replace(
        coordinator._deps,
        review_canary=ReviewCanaryConfig(
            policy=ReviewCanaryPolicy.CANARY, canary_percentage=100, version="proof-v1"
        ),
    )
    command = s.TicketReviewSessionRequest(client_session_id="1" * 32)
    session = coordinator.start_review_session(workspace, command)
    assert session.variant == "manual_guidance_v1"
    body = observation(session)
    receipt = coordinator.record_review_observation(workspace, session.session_id, body)
    assert coordinator.record_review_observation(workspace, session.session_id, body) == receipt
    coordinator._deps = replace(
        coordinator._deps, review_canary=ReviewCanaryConfig(version="rollback-v2")
    )
    assert coordinator.start_review_session(workspace, command) == session
    fresh = coordinator.start_review_session(
        workspace, s.TicketReviewSessionRequest(client_session_id="2" * 32)
    )
    assert fresh.variant == "baseline"
    assert fresh.canary_version == "rollback-v2"
    coordinator.record_review_observation(workspace, fresh.session_id, observation(fresh))
    assert business_rows(store) == before


@pytest.mark.parametrize(
    ("score", "reason"),
    [
        (None, "field_missing_confidence"),
        (0.79, "field_low_confidence"),
        (0.85, "corroborated_suggestion"),
        (0.95, "high_confidence_suggestion"),
    ],
)
def test_closed_reasons_separated_spans_and_policy(score: float | None, reason: str) -> None:
    reader, _, _ = routed(score)
    ticks = iter([1.0, 3.0, 4.0, 9.0])
    reader._monotonic = lambda: next(ticks)
    reading = reader.extract([h.attachment(h.FRONT)], max_cost_cents=10)
    obs = build_ocr_observation_from_reader(
        reader, reading=reading, coord_start=0.0, coord_end=10.0
    )
    assert obs.reason_counts[OcrFieldReason(reason)] == 3
    assert sum(obs.reason_counts.values()) == 11
    assert obs.primary_duration_ms == 2000
    assert obs.coordinator_duration_ms == 10_000
    assert obs.primary_start_offset_ms == 1000
    assert obs.primary_end_offset_ms == 3000
    assert obs.escalation_duration_ms == (5000 if score == 0.85 else None)
    assert obs.escalation_start_offset_ms == (4000 if score == 0.85 else None)
    assert obs.decision_policy == "ocr-routing-v1:0.8:0.9"
    assert obs.shown_suggestion_count == (3 if score is not None and score >= 0.80 else 0)


@pytest.mark.parametrize(
    ("second", "limit", "reason"),
    [
        ("missing", 10, "escalation_unavailable"),
        ("budget", 1, "escalation_budget_exhausted"),
        ("failure", 10, "escalation_failed"),
        ("refused", 10, "escalation_refused"),
        ("conflict", 10, "independent_disagreement"),
    ],
)
def test_escalation_reason_is_actual_path(second: str, limit: int, reason: str) -> None:
    primary = TicketVisionReader(
        h.ScriptedVisionClient((payload(0.85), Usage(cost_cents=1))), confidence=True
    )
    secondary = (
        None
        if second == "missing"
        else TicketVisionReader(
            h.FailingVisionClient(LlmProviderError("private error"))
            if second == "failure"
            else h.ScriptedVisionClient(
                (None if second == "refused" else payload(0.96, "DIFFERENT"), Usage())
            )
        )
    )
    reader = RoutedTicketReader(
        primary, "test-primary", secondary, secondary_model="test-secondary"
    )
    reading = reader.extract([h.attachment(h.FRONT)], max_cost_cents=limit)
    obs = build_ocr_observation_from_reader(reader, reading=reading)
    assert obs.reason_counts[OcrFieldReason(reason)] >= 1
    assert "private error" not in repr(obs)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (LlmBudgetExceeded("private"), "budget_refused"),
        (LlmProviderError("private"), "provider_error"),
    ],
)
def test_primary_failures_have_reasons_without_raw_error(
    tmp_path: Path, error: Exception, status: str
) -> None:
    fx = h.harness(tmp_path / "failed.sqlite", h.FailingVisionClient(error))
    try:
        ws, _, _, _ = h.ready(fx)
        with pytest.raises(TicketIntakeServiceError):
            fx.coordinator.request_reading(ws, "extract", 2)
        view = fx.coordinator.ticket_observability(ws)
        detail = view.ocr_observations[0].observation
        assert detail is not None
        assert detail.primary_status == status
        assert "private" not in view.model_dump_json()
        assert detail.escalation_duration_ms is None
    finally:
        fx.store.close()


def test_ocr_completion_atomic_replay_and_restart(tmp_path: Path) -> None:
    reader, primary, secondary = routed(0.85)
    fx = h.harness(tmp_path / "durable.sqlite", primary)
    fx.coordinator._deps = replace(fx.coordinator._deps, reader_for_subject=lambda _: reader)
    ws, _, _, _ = h.ready(fx)
    first = fx.coordinator.request_reading(ws, "extract", 2)
    before = fx.coordinator.ticket_observability(ws)
    assert len(before.ocr_observations) == 1
    assert before.ocr_observations[0].coverage == "recorded"
    assert "DEM0926" not in before.model_dump_json()
    fx.store.close()
    reopened = h.harness(tmp_path / "durable.sqlite", primary, photos=fx.photos)
    try:
        assert reopened.coordinator.request_reading(ws, "extract", 2) == first
        assert reopened.coordinator.request_reading(ws, "alias", 2).fields == first.fields
        assert reopened.coordinator.ticket_observability(ws) == before
        assert len(primary.calls) == len(secondary.calls) == 1
        db = reopened.store._require_open()
        for table in ("pb_ticket_ocr_observations",):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(f"DELETE FROM {table}")
    finally:
        reopened.store.close()


def test_observation_failure_rolls_back_completion_without_retrying_provider(
    tmp_path: Path,
) -> None:
    client = h.ScriptedVisionClient((h.reading_payload(), Usage()))
    fx = h.harness(tmp_path / "atomic.sqlite", client)
    try:
        ws, _, _, _ = h.ready(fx)
        db = fx.store._require_open()
        db.execute(
            "CREATE TRIGGER fail_observation BEFORE INSERT ON pb_ticket_ocr_observations "
            "BEGIN SELECT RAISE(ABORT, 'synthetic observation failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="synthetic observation failure"):
            fx.coordinator.request_reading(ws, "extract", 2)
        claim = fx.intake.operation_claim("extract")
        assert claim is not None
        assert claim.status is ClaimStatus.PENDING
        assert not fx.store.journey_events(workspace_token=ws).events
        assert fx.coordinator.ticket_observability(ws).ocr_observations[0].coverage == "pending"
        assert fx.coordinator.request_reading(ws, "extract", 2).status == "pending"
        assert len(client.calls) == 1
    finally:
        fx.store.close()


def test_pinned_canary_is_durable_and_client_claims_are_not_business_events(tmp_path: Path) -> None:
    fx = h.harness(tmp_path / "canary.sqlite", h.ScriptedVisionClient())
    ws, _, _, _ = h.ready(fx)
    try:
        assert_review_does_not_change_commitments(fx.store, fx.coordinator, ws)
        view = fx.coordinator.ticket_observability(ws)
        assert len(view.review_sessions) == 2
        assert len(view.review_observations) == 2
        session = view.review_sessions[0]
        with pytest.raises(TicketIntakeServiceError, match="conflict"):
            fx.coordinator.record_review_observation(
                ws,
                session.session_id,
                observation(session).model_copy(update={"visible_elapsed_ms": 999}),
            )
        for table in ("pb_ticket_review_sessions", "pb_ticket_review_observations"):
            with pytest.raises(sqlite3.IntegrityError):
                fx.store._require_open().execute(f"DELETE FROM {table}")
    finally:
        fx.store.close()
    reopened = h.harness(tmp_path / "canary.sqlite", h.ScriptedVisionClient(), photos=fx.photos)
    try:
        assert reopened.coordinator.ticket_observability(ws) == view
        assert (
            reopened.coordinator.start_review_session(
                ws, s.TicketReviewSessionRequest(client_session_id="1" * 32)
            )
            == session
        )
    finally:
        reopened.store.close()


@pytest.mark.anyio
async def test_owner_api_privacy_bounds_and_no_authoritative_confirmation(tmp_path: Path) -> None:
    fx = h.harness(tmp_path / "api.sqlite", h.ScriptedVisionClient())
    ws, _, _, _ = h.ready(fx)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(fx.service)), base_url="http://test"
        ) as api:
            headers = {"Authorization": "Bearer fake-alice"}
            path = f"/workspaces/{ws}/ticket-review-sessions"
            result = await api.post(path, headers=headers, json={"client_session_id": "b" * 32})
            assert result.status_code == 200, result.text
            session = s.TicketReviewSession.model_validate(result.json())
            assert (
                await api.post(
                    path,
                    headers={"Authorization": "Bearer fake-bob"},
                    json={"client_session_id": "c" * 32},
                )
            ).status_code == 404
            assert (
                await api.get(
                    f"/workspaces/{ws}/ticket-observability",
                    headers={"Authorization": "Bearer fake-bob"},
                )
            ).status_code == 404
            for bad in (
                {"client_session_id": "alice@example.test"},
                {"client_session_id": "b" * 32, "email": "private"},
            ):
                assert (await api.post(path, headers=headers, json=bad)).status_code == 422
            endpoint = path + f"/{session.session_id}/observations"
            body = observation(session).model_dump(mode="json")
            for key, value in (
                ("visible_elapsed_ms", -1),
                ("visible_elapsed_ms", True),
                ("plate", "PRIVATE"),
                ("outcome", "server_confirmed"),
            ):
                assert (
                    await api.post(endpoint, headers=headers, json={**body, key: value})
                ).status_code == 422
            good = await api.post(endpoint, headers=headers, json=body)
            assert good.status_code == 200, good.text
            assert (await api.post(endpoint, headers=headers, json=body)).json() == good.json()
            assert good.json()["source"] == "client_observation"
            assert not fx.store.journey_events(workspace_token=ws).events
    finally:
        fx.store.close()


@pytest.mark.parametrize(
    "values",
    [
        {"PB_REVIEW_GUIDANCE_CANARY_PERCENT": "101"},
        {"PB_REVIEW_GUIDANCE_CANARY_POLICY": "unknown"},
        {"PB_REVIEW_GUIDANCE_CANARY_VERSION": "https://secret.test"},
    ],
)
def test_invalid_canary_config_refused(values: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="PB_REVIEW_GUIDANCE"):
        load_review_canary_config(values)


def test_default_canary_is_off() -> None:
    assert load_review_canary_config({}).policy is ReviewCanaryPolicy.BASELINE
    with pytest.raises(ValidationError):
        s.TicketReviewSessionRequest(client_session_id="someone-name")


def test_unexpected_secondary_failure_preserves_unknown_completion(tmp_path: Path) -> None:
    primary = h.ScriptedVisionClient((payload(0.85), Usage()))
    reader = RoutedTicketReader(
        TicketVisionReader(primary, confidence=True),
        "test-primary",
        TicketVisionReader(h.FailingVisionClient(RuntimeError("private provider content"))),
    )
    fx = h.harness(tmp_path / "unexpected.sqlite", primary)
    fx.coordinator._deps = replace(fx.coordinator._deps, reader_for_subject=lambda _: reader)
    try:
        ws, _, _, _ = h.ready(fx)
        with pytest.raises(TicketIntakeServiceError, match="provider_unavailable"):
            fx.coordinator.request_reading(ws, "extract", 2)
        detail = fx.coordinator.ticket_observability(ws).ocr_observations[0]
        assert detail.operation_status == "unknown"
        assert detail.observation is not None
        assert detail.observation.primary_status == "completed"
        assert detail.observation.escalation_status == "failed"
        assert "private provider content" not in detail.model_dump_json()
    finally:
        fx.store.close()


def test_completed_ocr_rejects_conflicting_observation_metadata(tmp_path: Path) -> None:
    reader, primary, _ = routed(0.95)
    fx = h.harness(tmp_path / "conflict.sqlite", primary)
    fx.coordinator._deps = replace(fx.coordinator._deps, reader_for_subject=lambda _: reader)
    try:
        ws, _, _, _ = h.ready(fx)
        fx.coordinator.request_reading(ws, "extract", 2)
        claim = fx.intake.operation_claim("extract")
        assert claim is not None
        before = fx.coordinator.ticket_observability(ws)
        # A completed request may not be relabeled with new routing/timing evidence.
        changed = build_ocr_observation_from_reader(reader)
        with pytest.raises(TicketObservabilityConflict, match="ocr_observation_conflict"):
            fx.intake.complete_operation(
                "extract",
                claim.status,
                claim.result_json,
                claim.usage_json,
                h.NOW,
                ocr_observation=changed,
            )
        assert fx.coordinator.ticket_observability(ws) == before
    finally:
        fx.store.close()


def test_session_and_event_growth_are_bounded(tmp_path: Path) -> None:
    fx = h.harness(tmp_path / "bounded.sqlite", h.ScriptedVisionClient())
    try:
        ws, _, _, _ = h.ready(fx)
        command = s.TicketReviewSessionRequest(client_session_id="1" * 32)
        session = fx.coordinator.start_review_session(ws, command)
        for index in range(64):
            fx.coordinator.record_review_observation(
                ws, session.session_id, observation(session, f"{index:032x}")
            )
        # Existing evidence still replays at the cap; fresh evidence is refused.
        fx.coordinator.record_review_observation(
            ws, session.session_id, observation(session, "0" * 32)
        )
        with pytest.raises(TicketIntakeServiceError, match="observation_limit"):
            fx.coordinator.record_review_observation(
                ws, session.session_id, observation(session, "f" * 32)
            )
        for index in range(2, 65):
            fx.coordinator.start_review_session(
                ws, s.TicketReviewSessionRequest(client_session_id=f"{index:032x}")
            )
        assert fx.coordinator.start_review_session(ws, command) == session
        with pytest.raises(TicketIntakeServiceError, match="session_limit"):
            fx.coordinator.start_review_session(
                ws, s.TicketReviewSessionRequest(client_session_id="f" * 32)
            )
    finally:
        fx.store.close()
