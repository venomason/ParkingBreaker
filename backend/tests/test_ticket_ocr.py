"""Provider routing must never turn uncertain or conflicting text into verified facts."""

from dataclasses import replace
from pathlib import Path

import pytest

import test_ticket_intake_service as h
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import LlmProviderError, Usage
from parkingbreaker.ticket_intake_service import TicketIntakeServiceError
from parkingbreaker.ticket_ocr import OcrPolicy, RoutedTicketReader
from parkingbreaker.ticket_vision import TicketField, TicketVisionReader, parse_reading


def payload(score: float | None = 0.95, plate: str = "DEM0926") -> JsonValue:
    data = h.reading_payload()
    h.shown(data, TicketField.LICENSE_PLATE, plate)
    for item in data.values():
        assert isinstance(item, dict)
        item["confidence"] = score if item["state"] == "shown" else None
    return data


def routed(
    score: float | None, secondary_score: float = 0.96, plate: str = "DEM0926"
) -> tuple[RoutedTicketReader, h.ScriptedVisionClient, h.ScriptedVisionClient]:
    primary = h.ScriptedVisionClient((payload(score), Usage(10, 10, 1)))
    secondary = h.ScriptedVisionClient((payload(secondary_score, plate), Usage(12, 12, 1)))
    reader = RoutedTicketReader(
        TicketVisionReader(primary, confidence=True),
        "minimax-m3.1",
        TicketVisionReader(secondary, confidence=True),
    )
    return reader, primary, secondary


@pytest.mark.parametrize(
    ("score", "escalates", "shown"),
    [
        (None, False, False),
        (0.0, False, False),
        (0.7999, False, False),
        (0.80, True, True),
        (0.8499, True, True),
        (0.85, True, True),
        (0.8999, True, True),
        (0.90, False, True),
        (0.99, False, True),
        (1.0, False, True),
    ],
)
def test_thresholds_are_per_field_and_escalation_is_bounded(
    score: float | None, escalates: bool, shown: bool
) -> None:
    reader, primary, secondary = routed(score)
    result = reader.extract([h.attachment(h.FRONT)], max_cost_cents=10)
    plate = next(f for f in result.fields if f.field is TicketField.LICENSE_PLATE)
    assert len(primary.calls) == 1
    assert len(secondary.calls) == int(escalates)
    assert (plate.value == "DEM0926") is shown
    if escalates:
        assert plate.confidence == 0.96  # secondary estimate, not a fabricated combined score
        assert "DEM0926" not in secondary.calls[0].user
        assert result.usage.cost_cents == 2
        assert primary.calls[0].max_cost_cents is not None
        assert secondary.calls[0].max_cost_cents is not None
        assert primary.calls[0].max_cost_cents + secondary.calls[0].max_cost_cents <= 10


def test_O_zero_disagreement_requires_manual_entry_without_normalization() -> None:
    reader, _, secondary = routed(0.85, 0.999, "DEMO926")
    result = reader.extract([h.attachment(h.FRONT)], max_cost_cents=10)
    plate = next(f for f in result.fields if f.field is TicketField.LICENSE_PLATE)
    assert plate.value is None
    assert plate.state.value == "unreadable"
    assert len(secondary.calls) == 1


def test_two_moderate_estimates_do_not_become_high_confidence() -> None:
    reader, _, _ = routed(0.85, 0.89)
    result = reader.extract([h.attachment(h.FRONT)], max_cost_cents=10)
    assert all(f.value is None for f in result.fields)


def test_conflict_overrides_high_primary_when_another_field_triggers_escalation() -> None:
    data = payload(0.95)
    assert isinstance(data, dict)
    assert isinstance(data["citation_number"], dict)
    data["citation_number"]["confidence"] = 0.85
    first = h.ScriptedVisionClient((data, Usage()))
    second = h.ScriptedVisionClient((payload(0.99, "DEMO926"), Usage()))
    reader = RoutedTicketReader(
        TicketVisionReader(first, confidence=True),
        "deepseek-flash",
        TicketVisionReader(second, confidence=True),
    )
    result = reader.extract([h.attachment(h.FRONT)], max_cost_cents=10)
    plate = next(f for f in result.fields if f.field is TicketField.LICENSE_PLATE)
    assert plate.value is None
    citation = next(f for f in result.fields if f.field is TicketField.CITATION_NUMBER)
    assert citation.value == "SYN-0001"


def test_no_remaining_budget_skips_secondary_and_requests_customer_input() -> None:
    reader, _, secondary = routed(0.85)
    result = reader.extract([h.attachment(h.FRONT)], max_cost_cents=1)
    assert secondary.calls == []
    assert result.escalation == "budget_exhausted"
    assert all(f.value is None for f in result.fields)


@pytest.mark.parametrize("secondary", [None, "failure"])
def test_missing_or_failed_secondary_uses_manual_entry(secondary: str | None) -> None:
    primary = h.ScriptedVisionClient((payload(0.85), Usage()))
    failing = h.FailingVisionClient(LlmProviderError("offline failure"))
    reader = RoutedTicketReader(
        TicketVisionReader(primary, confidence=True),
        "deepseek-flash",
        TicketVisionReader(failing) if secondary else None,
    )
    result = reader.extract([h.attachment(h.FRONT)], max_cost_cents=10)
    assert all(f.value is None for f in result.fields)
    assert result.escalation == ("failed" if secondary else "unavailable")


@pytest.mark.parametrize("score", [True, False, -0.1, 1.1, float("nan"), float("inf"), "90%"])
def test_invalid_confidence_is_refused(score: JsonValue) -> None:
    data = payload()
    assert isinstance(data, dict)
    assert isinstance(data["license_plate"], dict)
    data["license_plate"]["confidence"] = score
    assert parse_reading(data) is None


@pytest.mark.parametrize(("low", "high"), [(0.9, 0.8), (-1, 0.9), (0.8, 90), (True, 0.9)])
def test_invalid_policy_fails_before_provider_use(low: float, high: float) -> None:
    with pytest.raises(ValueError, match="OCR thresholds"):
        OcrPolicy(low, high)


def test_routed_reading_replays_after_restart_without_more_calls(tmp_path: Path) -> None:
    reader, primary, secondary = routed(0.85)
    fixture = h.harness(tmp_path / "state.sqlite", primary)
    fixture.coordinator._deps = replace(
        fixture.coordinator._deps, reader_for_subject=lambda _: reader
    )
    workspace, _, _, _ = h.ready(fixture)
    first = fixture.coordinator.request_reading(workspace, "read-once", 2)
    assert first.primary_model == "minimax-m3.1"
    assert first.escalation == "completed"
    assert first.fields[1].confidence == 0.96
    fixture.store.close()
    reopened = h.harness(tmp_path / "state.sqlite", primary, photos=fixture.photos)
    assert reopened.coordinator.request_reading(workspace, "read-once", 2) == first
    assert reopened.coordinator.request_reading(workspace, "alias", 2).fields == first.fields
    assert len(primary.calls) == len(secondary.calls) == 1
    reopened.store.close()


def test_explicit_accuracy_acknowledgment_required_even_with_successful_review(
    tmp_path: Path,
) -> None:
    client = h.ScriptedVisionClient(
        (h.reading_payload(), Usage()), (h.review_payload(h.binding_for().review_fields()), Usage())
    )
    fixture = h.harness(tmp_path / "state.sqlite", client)
    workspace, _, _, _ = h.ready(fixture)
    fixture.coordinator.request_reading(workspace, "extract", 2)
    fixture.coordinator.request_review(workspace, "req-review", 2)
    command = h.confirm_command(fixture, workspace)
    with pytest.raises(TicketIntakeServiceError, match="accuracy_confirmation_required"):
        fixture.coordinator.confirm(
            workspace, command.model_copy(update={"accuracy_acknowledged": False})
        )
    assert fixture.coordinator.confirm(workspace, command).state == "current"
    fixture.store.close()
