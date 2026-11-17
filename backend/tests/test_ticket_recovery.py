"""Failure recovery uses customer facts without inventing extracted provenance."""

from pathlib import Path
from typing import Any

import pytest

import test_ticket_intake_service as h
from parkingbreaker import prehold
from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import Usage
from parkingbreaker.ticket_fact_binding import TicketFactBinding, TicketFactBindingError
from parkingbreaker.ticket_intake_service import TicketIntakeServiceError
from parkingbreaker.ticket_vision import FIELD_ORDER, TicketField
from samples import SAN_FRANCISCO


@pytest.fixture
def fake_resolution(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def resolve(query: Any, *_args: object, **_kwargs: object) -> h.Resolved:
        seen.append(query.printed_issuer)
        return h.Resolved(SAN_FRANCISCO)

    monkeypatch.setattr(prehold, "resolve_ticket_issuer", resolve)
    return seen


def empty_literal() -> dict[str, None]:
    return {field.value: None for field in FIELD_ORDER}


def save_binding(harnessed: h.Harness, workspace: str, binding: TicketFactBinding) -> None:
    form = harnessed.service.workspace_view(workspace).intake_form
    assert form is not None
    harnessed.service.save_intake_form(
        workspace,
        s.SaveIntakeForm(
            expected_revision=h.revision(harnessed, workspace),
            form=form.model_copy(update={"ticket_fact_binding": h.payload(binding)}),
        ),
    )


@pytest.mark.parametrize("failure", ["refused", "unreadable", "not_shown", "provider_error"])
def test_customer_recovers_without_another_provider_call(
    tmp_path: Path, fake_resolution: list[str], failure: str
) -> None:
    data: JsonValue = (
        {}
        if failure == "refused"
        else {
            field.value: {
                "state": "unreadable" if failure == "unreadable" else "not_shown",
                "value": None,
            }
            for field in FIELD_ORDER
        }
    )
    client = (
        h.FailingVisionClient(RuntimeError("simulated provider failure"))
        if failure == "provider_error"
        else h.ScriptedVisionClient((data, Usage()))
    )
    fixture = h.harness(tmp_path / "pb.sqlite", client, gateway=True)
    binding = h.binding_for(empty_literal())
    workspace, _, front, back = h.ready(fixture, binding=binding)
    if failure == "provider_error":
        with pytest.raises(TicketIntakeServiceError, match="provider_unavailable"):
            fixture.coordinator.request_reading(workspace, "extract", 2)
    else:
        fixture.coordinator.request_reading(workspace, "extract", 2)
    before = client.calls if isinstance(client, h.FailingVisionClient) else len(client.calls)
    review = fixture.coordinator.request_review(workspace, "req-review", 2)
    assert review.status == "success"
    assert review.basis == "customer-supplied facts; no model comparison"
    assert review.fields == []
    assert fixture.coordinator.request_review(workspace, "req-review", 2) == review
    alias = fixture.coordinator.request_review(workspace, "review-alias", 2)
    assert alias.status == "success"
    assert fake_resolution == []
    with pytest.raises(ServiceConflict, match="ticket_confirmation_required"):
        fixture.service.submit(
            workspace,
            h.submit_body(binding, front, back).model_copy(
                update={"ticket_confirmation_command_id": None}
            ),
        )
    assert fake_resolution == []
    command = h.confirm_command(fixture, workspace)
    receipt = fixture.coordinator.confirm(workspace, command)
    assert fixture.coordinator.confirm(workspace, command) == receipt
    assert fixture.coordinator.status(workspace).confirmation.state == "current"
    # Reopen the actual SQLite state; confirmation survives process reconstruction.
    fixture.store.close()
    fixture = h.harness(tmp_path / "pb.sqlite", client, gateway=True, photos=fixture.photos)
    assert fixture.coordinator.status(workspace).review.basis == review.basis
    assert fixture.coordinator.status(workspace).confirmation.state == "current"
    body = h.submit_body(binding, front, back)
    submitted = fixture.service.submit(workspace, body)
    assert len(fake_resolution) == 1
    assert fixture.service.submit(workspace, body) == submitted
    record = fixture.store.get_intake(submitted.case_id)
    assert record is not None
    fixture.coordinator.revalidate_for_mailing(record, body)
    assert (
        client.calls if isinstance(client, h.FailingVisionClient) else len(client.calls)
    ) == before
    assert len(fake_resolution) == 1
    fixture.store.close()


def test_partial_O_zero_correction_keeps_both_provenances(tmp_path: Path) -> None:
    literal = h.literal_values()
    literal["license_plate"] = "DEM0926"
    adopted = h.adopted()
    adopted["license_plate"] = "DEMO926"
    binding = h.binding_for(literal, adopted)
    data = h.reading_payload()
    h.shown(data, TicketField.LICENSE_PLATE, "DEM0926")
    client = h.ScriptedVisionClient(
        (data, Usage()), (h.review_payload(binding.review_fields()), Usage())
    )
    fixture = h.harness(tmp_path / "pb.sqlite", client)
    workspace, _, _, _ = h.ready(fixture, binding=binding)
    fixture.coordinator.request_reading(workspace, "extract", 2)
    fixture.coordinator.request_review(workspace, "req-review", 2)
    assert "license_plate: DEM0926" in client.calls[-1].user
    assert "DEMO926" not in client.calls[-1].user
    fixture.coordinator.confirm(workspace, h.confirm_command(fixture, workspace))
    saved = fixture.service.workspace_view(workspace).intake_form
    assert saved is not None
    assert saved.ticket_fact_binding is not None
    assert saved.ticket_fact_binding.literal["license_plate"] == "DEM0926"
    assert saved.ticket_fact_binding.adopted["license_plate"] == "DEMO926"
    fixture.store.close()


@pytest.mark.parametrize("after_success", [False, True])
def test_forged_extracted_value_refused_even_in_customer_mode(
    tmp_path: Path, after_success: bool
) -> None:
    client = h.ScriptedVisionClient((h.reading_payload() if after_success else {}, Usage()))
    fixture = h.harness(tmp_path / "pb.sqlite", client)
    workspace, _, _, _ = h.ready(fixture)
    fixture.coordinator.request_reading(workspace, "extract", 2)
    forged = h.literal_values()
    forged["citation_number"] = "CUSTOMER-INVENTED-AS-OCR"
    save_binding(fixture, workspace, h.binding_for(forged))
    for manual in (False, True):
        with pytest.raises(TicketIntakeServiceError, match="provenance_mismatch"):
            fixture.coordinator.request_review(
                workspace,
                f"review-{manual}",
                h.revision(fixture, workspace),
                customer_supplied=manual,
            )
    assert len(client.calls) == 1
    fixture.store.close()


def test_failed_comparison_can_use_explicit_customer_confirmation(tmp_path: Path) -> None:
    client = h.ScriptedVisionClient((h.reading_payload(), Usage()), ({}, Usage()))
    fixture = h.harness(tmp_path / "pb.sqlite", client)
    workspace, _, _, _ = h.ready(fixture)
    fixture.coordinator.request_reading(workspace, "extract", 2)
    assert fixture.coordinator.request_review(workspace, "failed-review", 2).status == "refused"
    result = fixture.service.request_ticket_review(
        workspace,
        s.TicketReviewRequest(request_id="req-review", expected_revision=2, customer_supplied=True),
    )
    assert result.basis == "customer-supplied facts; no model comparison"
    fixture.coordinator.confirm(workspace, h.confirm_command(fixture, workspace))
    assert fixture.coordinator.status(workspace).confirmation.state == "current"
    assert len(client.calls) == 2
    fixture.store.close()


def test_incomplete_customer_facts_cannot_form_confirmable_envelope() -> None:
    for key in ["citation_number", "issue_date", "license_plate", "issuer_hint", "stage"]:
        incomplete = h.adopted()
        incomplete[key] = ""
        with pytest.raises(TicketFactBindingError):
            h.binding_for(empty_literal(), incomplete)
