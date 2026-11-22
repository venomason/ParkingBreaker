"""The actual owner-scoped ticket-intake service: claims, confirmations and guards."""

from __future__ import annotations

import base64
import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from parkingbreaker import prehold
from parkingbreaker.ai_spend_store import WORKER_CAPS_CENTS
from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.blob_store import FakeBlobStore
from parkingbreaker.case_service import CaseService, CaseServiceDeps
from parkingbreaker.case_state import (
    CaseState,
    CustomerApproved,
    DraftReady,
    ResearchComplete,
)
from parkingbreaker.config_fakes import AcceptingLobClient
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import (
    ImageAttachment,
    LlmBudgetExceeded,
    LlmProviderError,
    Usage,
)
from parkingbreaker.llm_gateway import CallLoggingLlmClient, SpendLedger
from parkingbreaker.llm_input_budget import UnboundableRequestError
from parkingbreaker.oplog.ai_journal import AiCallStatus, AiWorker
from parkingbreaker.payment_records import PaymentClient
from parkingbreaker.prehold import PreholdDeps
from parkingbreaker.prices import CERTIFIED_CENTS
from parkingbreaker.service_policy import SERVICE_CONTROL, STATE_BASIS
from parkingbreaker.ticket_fact_binding import TicketFactBinding
from parkingbreaker.ticket_intake_service import (
    TicketIntakeCoordinator,
    TicketIntakeDeps,
    TicketIntakeServiceError,
)
from parkingbreaker.ticket_intake_store import (
    ClaimStatus,
    IntakeOperation,
    TicketIntakeStore,
)
from parkingbreaker.ticket_vision import (
    EXTRACT_OPERATION,
    FIELD_ORDER,
    TicketField,
    TicketVisionReader,
    input_binding_hash,
)
from parkingbreaker.transaction_store import SqliteTransactionStore
from samples import SAN_FRANCISCO
from test_case_service import INTAKE

NOW = datetime(2026, 10, 15, 21, tzinfo=UTC)
ALICE = "alice"
FRONT_COLOUR = (10, 20, 30)
BACK_COLOUR = (40, 50, 60)


def png_bytes(colour: tuple[int, int, int], size: tuple[int, int] = (8, 8)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


FRONT = png_bytes(FRONT_COLOUR)
BACK = png_bytes(BACK_COLOUR)
OTHER = png_bytes((200, 30, 30))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def attachment(data: bytes) -> ImageAttachment:
    return ImageAttachment(media_type="image/png", data_base64=b64(data))


def adopted() -> dict[str, object]:
    values = cast(dict[str, object], copy.deepcopy(INTAKE["ticket"]))
    # ALL13 explicitly supplied: no default issuer kind, stage, code or optional date.
    values.setdefault("issuer_kind", "GOVERNMENT")
    values.setdefault("notice_date", None)
    values.setdefault("printed_deadline", None)
    return values


def literal_values(
    *, citation: str = "SYN-0001", issue_date: str = "10/12/2026"
) -> dict[str, object]:
    values: dict[str, object] = {field.value: None for field in FIELD_ORDER}
    values["citation_number"] = citation
    values["issue_date"] = issue_date
    values["license_plate"] = "SYN123"
    return values


def binding_for(
    literal: Mapping[str, object] | None = None,
    adopted_values: Mapping[str, object] | None = None,
) -> TicketFactBinding:
    return TicketFactBinding.bind(
        literal_values() if literal is None else literal,
        adopted() if adopted_values is None else adopted_values,
    )


def payload(binding: TicketFactBinding) -> s.TicketFactBindingPayload:
    return s.TicketFactBindingPayload(literal=dict(binding.literal), adopted=dict(binding.adopted))


def reading_payload() -> dict[str, JsonValue]:
    # The default successful extraction must agree with the saved literal envelope.
    return {
        field.value: {
            "state": "shown" if literal_values()[field.value] is not None else "not_shown",
            "value": cast(JsonValue, literal_values()[field.value]),
        }
        for field in FIELD_ORDER
    }


def shown(payload_map: dict[str, JsonValue], field: TicketField, value: str) -> None:
    payload_map[field.value] = {"state": "shown", "value": value}


def review_payload(
    values: Mapping[str, str],
    *,
    comparisons: Mapping[str, str] | None = None,
    visible: Mapping[str, str] | None = None,
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for name, value in values.items():
        comparison = "match" if comparisons is None else comparisons.get(name, "match")
        if comparison in ("match", "conflict"):
            text = value if visible is None else visible.get(name, value)
            result[name] = {"comparison": comparison, "visible_text": text}
        else:
            result[name] = {"comparison": comparison, "visible_text": None}
    return result


class Call:
    def __init__(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        max_cost_cents: int | None,
        images: tuple[ImageAttachment, ...] | None,
    ) -> None:
        self.system = system
        self.user = user
        self.schema = schema
        self.max_cost_cents = max_cost_cents
        self.images = images


class ScriptedVisionClient:
    def __init__(self, *responses: tuple[JsonValue, Usage]) -> None:
        self._responses = list(responses)
        self.calls: list[Call] = []

    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
        images: Sequence[ImageAttachment] | None = None,
    ) -> tuple[JsonValue, Usage]:
        self.calls.append(
            Call(
                system,
                user,
                schema,
                max_cost_cents,
                tuple(images) if images is not None else None,
            )
        )
        if not self._responses:
            raise AssertionError("unexpected extra model call")
        return self._responses.pop(0)


class FailingVisionClient:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
        images: Sequence[ImageAttachment] | None = None,
    ) -> tuple[JsonValue, Usage]:
        self.calls += 1
        raise self.error


class Resolved:
    def __init__(self, agency: object) -> None:
        self.agency = agency
        self.stop = None
        self.code = None


@pytest.fixture(autouse=True)
def fake_resolution(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def resolve(query: Any, *_args: object, **_kwargs: object) -> Resolved:
        seen.append(query.printed_issuer)
        return Resolved(SAN_FRANCISCO)

    monkeypatch.setattr(prehold, "resolve_ticket_issuer", resolve)
    return seen


def unused_prehold() -> PreholdDeps:
    unused: Any = object()
    return PreholdDeps(
        unused, None, unused, unused, unused, lambda: NOW, 25, STATE_BASIS, SERVICE_CONTROL
    )


@dataclass
class Harness:
    service: CaseService
    store: SqliteTransactionStore
    photos: FakeBlobStore
    intake: TicketIntakeStore
    coordinator: TicketIntakeCoordinator
    client: ScriptedVisionClient | FailingVisionClient


def harness(
    path: Path,
    client: ScriptedVisionClient | FailingVisionClient,
    *,
    gateway: bool = False,
    photos: FakeBlobStore | None = None,
) -> Harness:
    store = SqliteTransactionStore(path)
    photos = FakeBlobStore() if photos is None else photos
    intake = TicketIntakeStore(store)
    counter = iter(range(1000))

    def reader_for_subject(subject: str) -> TicketVisionReader:
        if gateway:
            wrapped = CallLoggingLlmClient(
                client,
                subject,
                AiWorker.INTAKE_READING,
                "test-vision",
                store.try_append_ai_call,
                lambda: NOW,
                spend=cast(SpendLedger, store),
                cap_cents=WORKER_CAPS_CENTS[AiWorker.INTAKE_READING],
            )
            return TicketVisionReader(wrapped)
        return TicketVisionReader(client)

    coordinator = TicketIntakeCoordinator(
        TicketIntakeDeps(
            store=store,
            intake=intake,
            photos=photos,
            clock=lambda: NOW,
            new_id=lambda: f"{next(counter):032x}",
            reader_for_subject=reader_for_subject,
            required=True,
        )
    )
    service = CaseService(
        CaseServiceDeps(
            store,
            unused_prehold(),
            lambda: NOW,
            cast(PaymentClient, object()),
            photos,
            new_id=lambda: f"{next(counter):032x}",
            lob=cast(Any, AcceptingLobClient()),
            ticket_intake=coordinator,
        )
    )
    return Harness(service, store, photos, intake, coordinator, client)


def photo(data: bytes) -> s.PhotoUpload:
    return s.PhotoUpload(content_type="image/png", data_base64=b64(data))


def ready(
    harnessed: Harness,
    *,
    back: bool = False,
    story: str = "",
    form_values: Mapping[str, str] | None = None,
    binding: TicketFactBinding | None = None,
) -> tuple[str, TicketFactBinding, str, str | None]:
    service = harnessed.service
    workspace = service.create_workspace(ALICE, s.CreateWorkspace(notes="")).workspace_id
    front_ref = service.upload_photo(workspace, photo(FRONT)).photo_ref
    back_ref = service.upload_photo(workspace, photo(BACK)).photo_ref if back else None
    chosen = binding_for() if binding is None else binding
    values: dict[str, str] = {"story": story}
    if form_values is not None:
        values.update(form_values)
    form = s.IntakeForm(
        values=values,
        ticket_photo_ref=front_ref,
        ticket_back_photo_ref=back_ref,
        ticket_fact_binding=payload(chosen),
    )
    service.save_intake_form(workspace, s.SaveIntakeForm(expected_revision=1, form=form))
    return workspace, chosen, front_ref, back_ref


def revision(harnessed: Harness, workspace: str) -> int:
    return harnessed.service.workspace_view(workspace).draft_revision


def confirm_command(
    harnessed: Harness,
    workspace: str,
    *,
    command: str = "cmd-confirm",
    review_id: str = "req-review",
    resolved: Mapping[str, s.TicketDiscrepancyResolution] | None = None,
    expected_generation: int = 0,
) -> s.TicketConfirmationRequest:
    return s.TicketConfirmationRequest(
        command_id=command,
        expected_revision=revision(harnessed, workspace),
        review_request_id=review_id,
        expected_confirmation_generation=expected_generation,
        accuracy_acknowledged=True,
        resolved_fields={} if resolved is None else dict(resolved),
    )


def submit_body(
    binding: TicketFactBinding,
    front_ref: str,
    back_ref: str | None,
    command_id: str = "cmd-confirm",
    *,
    ticket: Mapping[str, object] | None = None,
) -> s.SubmitIntake:
    return s.SubmitIntake.model_validate(
        {
            **copy.deepcopy(INTAKE),
            "ticket": dict(binding.adopted) if ticket is None else dict(ticket),
            "customer_position": {"statement": "my exact words", "provision_selections": []},
            "ticket_confirmation_command_id": command_id,
            "photo_refs": [],
            "citation_photo_ref": front_ref,
            "citation_back_photo_ref": back_ref,
        }
    )


def reservation_id(harnessed: Harness, workspace: str) -> str:
    row = (
        harnessed.store._require_open()
        .execute(
            "SELECT intake_id FROM pb_ticket_intake_reservations WHERE workspace_token=?",
            (workspace,),
        )
        .fetchone()
    )
    assert row is not None
    return str(row[0])


# ---------------------------------------------------------------------------
# Actual owner-scoped images and fresh review scope
# ---------------------------------------------------------------------------


def test_actual_owner_images_reach_client_and_review_excludes_customer_text(
    tmp_path: Path,
) -> None:
    binding = binding_for()
    client = ScriptedVisionClient(
        (reading_payload(), Usage(input_tokens=1200, output_tokens=90, cost_cents=3)),
        (review_payload(binding.review_fields()), Usage(input_tokens=800, output_tokens=60)),
    )
    harnessed = harness(tmp_path / "pb.sqlite", client)
    workspace, binding, _front, _back = ready(
        harnessed,
        back=True,
        story="PRIVATE STORY NEVER SENT",
        form_values={"contact.name": "Private Person", "contact.email": "p@example.test"},
    )

    reading = harnessed.coordinator.request_reading(workspace, "req-extract", 2)

    assert reading.status == "success"
    assert reading.request_id == "req-extract"
    call = client.calls[0]
    assert call.images == (attachment(FRONT), attachment(BACK))
    assert call.user == (
        "Read the attached ticket image(s). Return exactly this closed field set: "
        + ", ".join(field.value for field in FIELD_ORDER)
        + ".\nDo not add fields. Do not explain. Return JSON only."
    )
    for prohibited in ("PRIVATE STORY", "Private Person", "p@example.test"):
        assert prohibited not in call.system
        assert prohibited not in call.user
        assert prohibited not in json.dumps(call.schema)

    reviewed = harnessed.coordinator.request_review(workspace, "req-review", 2)

    assert reviewed.status == "success"
    assert reviewed.basis == "model comparison only; not external verification"
    assert reviewed.current is True
    review_call = client.calls[1]
    assert review_call.images == (attachment(FRONT), attachment(BACK))
    for prohibited in ("PRIVATE STORY", "Private Person", "p@example.test"):
        assert prohibited not in review_call.system
        assert prohibited not in review_call.user
    for name, value in binding.review_fields().items():
        assert f"{name}: {value}" in review_call.user
    # Adopted-only facts and extraction output never enter the review prompt.
    for absent in ("issuer_hint:", "violation_code:", "stage:", "not_shown"):
        assert absent not in review_call.user


# ---------------------------------------------------------------------------
# One reserved subject, aggregate cap and duplicate attempts
# ---------------------------------------------------------------------------


def test_one_reserved_subject_shared_ledger_cap_and_duplicate_attempts(tmp_path: Path) -> None:
    binding = binding_for()
    client = ScriptedVisionClient(
        (reading_payload(), Usage(cost_cents=45)),
        (review_payload(binding.review_fields()), Usage(cost_cents=5)),
    )
    harnessed = harness(tmp_path / "pb.sqlite", client, gateway=True)
    workspace, binding, _front, _back = ready(harnessed)

    harnessed.coordinator.request_reading(workspace, "req-extract", 2)
    intake_id = reservation_id(harnessed, workspace)

    duplicate = harnessed.coordinator.request_reading(workspace, "req-extract", 2)
    alias = harnessed.coordinator.request_reading(workspace, "req-extract-alias", 2)
    assert duplicate.status == "success"
    assert alias.status == "success"
    assert len(client.calls) == 1

    harnessed.coordinator.request_review(workspace, "req-review", 2)
    assert len(client.calls) == 2
    assert (
        harnessed.store.ai_spent_cents(intake_id, AiWorker.INTAKE_READING)
        == WORKER_CAPS_CENTS[AiWorker.INTAKE_READING]
    )
    subjects = (
        harnessed.store._require_open()
        .execute("SELECT DISTINCT subject_token FROM pb_ai_spend")
        .fetchall()
    )
    assert subjects == [(intake_id,)]

    # A new binding needs a third call, but the shared 50c allowance is already spent.
    other_ref = harnessed.service.upload_photo(workspace, photo(OTHER)).photo_ref
    current = harnessed.service.workspace_view(workspace).intake_form
    assert current is not None
    changed = current.model_copy(update={"ticket_photo_ref": other_ref})
    harnessed.service.save_intake_form(
        workspace,
        s.SaveIntakeForm(expected_revision=revision(harnessed, workspace), form=changed),
    )
    with pytest.raises(TicketIntakeServiceError, match="ticket_reading_budget_exhausted"):
        harnessed.coordinator.request_reading(
            workspace, "req-third", revision(harnessed, workspace)
        )
    assert len(client.calls) == 2
    claim = harnessed.intake.operation_claim("req-third")
    assert claim is not None
    assert claim.status is ClaimStatus.REFUSED


# ---------------------------------------------------------------------------
# Malformed media, pending/unknown/alias truth and provider failures
# ---------------------------------------------------------------------------


def test_malformed_media_is_refused_before_reservation_or_client(tmp_path: Path) -> None:
    client = ScriptedVisionClient((reading_payload(), Usage()))
    harnessed = harness(tmp_path / "pb.sqlite", client)
    workspace = harnessed.service.create_workspace(ALICE, s.CreateWorkspace(notes="")).workspace_id
    broken_ref = "photo_" + "a" * 64
    harnessed.photos.items[(ALICE, broken_ref)] = b"not an image"
    form = s.IntakeForm(ticket_photo_ref=broken_ref)
    harnessed.service.save_intake_form(workspace, s.SaveIntakeForm(expected_revision=1, form=form))

    with pytest.raises(TicketIntakeServiceError, match="ticket_photo_not_supported"):
        harnessed.coordinator.request_reading(workspace, "req-broken", 2)

    assert client.calls == []
    assert harnessed.intake.reservation_for(workspace) is None


def test_pending_crash_replay_and_alias_never_call_provider(tmp_path: Path) -> None:
    binding = binding_for()
    client = ScriptedVisionClient((reading_payload(), Usage()))
    harnessed = harness(tmp_path / "pb.sqlite", client)
    workspace, binding, _front, _back = ready(harnessed)
    assert harnessed.coordinator.request_reading(workspace, "req-extract", 2).status == "success"

    carol = harnessed.service.create_workspace("carol", s.CreateWorkspace(notes="")).workspace_id
    front = harnessed.service.upload_photo(carol, photo(FRONT)).photo_ref
    form = s.IntakeForm(ticket_photo_ref=front, ticket_fact_binding=payload(binding))
    harnessed.service.save_intake_form(carol, s.SaveIntakeForm(expected_revision=1, form=form))
    reservation = harnessed.intake.reserve_intake("case_manual", carol, "carol", NOW)
    crash = harnessed.intake.claim_operation(
        "req-pending",
        reservation.intake_id,
        IntakeOperation.EXTRACT,
        input_binding_hash(EXTRACT_OPERATION, (attachment(FRONT),)),
        NOW,
        workspace_token=carol,
        owner="carol",
    )
    assert crash.should_call

    pending = harnessed.coordinator.request_reading(carol, "req-pending", 2)
    assert pending.status == "pending"
    assert len(client.calls) == 1

    harnessed.intake.complete_operation(
        "req-pending",
        ClaimStatus.UNKNOWN,
        None,
        None,
        NOW,
        claim_token=crash.claim_token,
    )
    alias = harnessed.coordinator.request_reading(carol, "req-alias", 2)
    assert alias.status == "unknown"
    assert len(client.calls) == 1


def test_schema_provider_and_budget_failures_retain_usage_truth(tmp_path: Path) -> None:
    malformed: JsonValue = {"citation_number": {"state": "shown", "value": "SYN-0001"}}
    refused_client = ScriptedVisionClient((malformed, Usage(cost_cents=4)))
    refused = harness(tmp_path / "refused.sqlite", refused_client)
    workspace, _binding, _front, _back = ready(refused)
    assert refused.coordinator.request_reading(workspace, "req-refused", 2).status == "refused"
    claim = refused.intake.operation_claim("req-refused")
    assert claim is not None
    assert claim.status is ClaimStatus.REFUSED
    assert claim.usage_json is not None
    assert '"cost_cents":4' in claim.usage_json
    assert refused.coordinator.request_reading(workspace, "req-refused", 2).status == "refused"
    assert len(refused_client.calls) == 1

    provider_client = FailingVisionClient(LlmProviderError("sanitized"))
    provider = harness(tmp_path / "provider.sqlite", provider_client)
    workspace, _binding, _front, _back = ready(provider)
    with pytest.raises(TicketIntakeServiceError, match="ticket_reading_provider_unavailable"):
        provider.coordinator.request_reading(workspace, "req-provider", 2)
    claim = provider.intake.operation_claim("req-provider")
    assert claim is not None
    assert claim.status is ClaimStatus.UNKNOWN
    assert claim.result_json is None
    assert claim.usage_json is None
    assert provider_client.calls == 1

    budget_client = FailingVisionClient(LlmBudgetExceeded("cap"))
    budget = harness(tmp_path / "budget.sqlite", budget_client)
    workspace, _binding, _front, _back = ready(budget)
    with pytest.raises(TicketIntakeServiceError, match="ticket_reading_budget_exhausted"):
        budget.coordinator.request_reading(workspace, "req-budget", 2)
    claim = budget.intake.operation_claim("req-budget")
    assert claim is not None
    assert claim.status is ClaimStatus.REFUSED
    assert claim.usage_json is not None
    assert '"cost_cents":0' in claim.usage_json
    assert budget_client.calls == 1


def test_pretransport_photo_limit_preserves_allowance_and_records_refusal(tmp_path: Path) -> None:
    client = FailingVisionClient(UnboundableRequestError("inline body too large"))
    harnessed = harness(tmp_path / "input-limit.sqlite", client, gateway=True)
    customer_binding = binding_for({field.value: None for field in FIELD_ORDER})
    workspace, _binding, _front, _back = ready(harnessed, binding=customer_binding, back=True)

    with pytest.raises(TicketIntakeServiceError, match="ticket_photo_reading_limit"):
        harnessed.coordinator.request_reading(workspace, "req-input-limit", 2)

    claim = harnessed.intake.operation_claim("req-input-limit")
    assert claim is not None
    assert claim.status is ClaimStatus.REFUSED
    assert claim.usage_json is not None
    assert json.loads(claim.usage_json)["cost_cents"] == 0
    assert harnessed.store.ai_spent_cents(claim.intake_id, AiWorker.INTAKE_READING) == 0
    (call,) = harnessed.store.ai_calls(claim.intake_id)
    assert call.status is AiCallStatus.SKIPPED
    assert call.cost_cents == 0

    review = harnessed.coordinator.request_review(workspace, "req-review", 2)
    assert review.status == "success"
    assert review.current
    assert review.basis == "customer-supplied facts; no model comparison"
    assert client.calls == 1


# ---------------------------------------------------------------------------
# Confirmation rules
# ---------------------------------------------------------------------------


def test_confirmation_requires_review_binding_and_explicit_resolutions(tmp_path: Path) -> None:
    binding = binding_for()
    confirmations = binding.review_fields()
    payload_map = review_payload(
        confirmations,
        comparisons={"citation_number": "conflict", "issue_date": "not_shown"},
        visible={"citation_number": "SYN-9999"},
    )
    client = ScriptedVisionClient(
        (reading_payload(), Usage(cost_cents=1)),
        (payload_map, Usage(cost_cents=1)),
    )
    harnessed = harness(tmp_path / "pb.sqlite", client)
    workspace, binding, _front, _back = ready(harnessed)
    harnessed.coordinator.request_reading(workspace, "req-extract", 2)
    reviewed = harnessed.coordinator.request_review(workspace, "req-review", 2)
    assert reviewed.status == "success"
    assert {item.field.value for item in reviewed.fields} == set(confirmations)

    with pytest.raises(TicketIntakeServiceError, match="discrepancy_resolution_required"):
        harnessed.coordinator.confirm(workspace, confirm_command(harnessed, workspace))
    wrong = {
        "citation_number": s.TicketDiscrepancyResolution.ABSENCE_ACKNOWLEDGED,
        "issue_date": s.TicketDiscrepancyResolution.ABSENCE_ACKNOWLEDGED,
    }
    with pytest.raises(TicketIntakeServiceError, match="discrepancy_resolution_required"):
        harnessed.coordinator.confirm(
            workspace, confirm_command(harnessed, workspace, resolved=wrong)
        )
    extra = {
        "citation_number": s.TicketDiscrepancyResolution.CONFIRMED_VALUE_STANDS,
        "issue_date": s.TicketDiscrepancyResolution.ABSENCE_ACKNOWLEDGED,
        "license_plate": s.TicketDiscrepancyResolution.CONFIRMED_VALUE_STANDS,
    }
    with pytest.raises(TicketIntakeServiceError, match="discrepancy_resolution_unexpected"):
        harnessed.coordinator.confirm(
            workspace, confirm_command(harnessed, workspace, resolved=extra)
        )
    resolutions = {
        "citation_number": s.TicketDiscrepancyResolution.CONFIRMED_VALUE_STANDS,
        "issue_date": s.TicketDiscrepancyResolution.ABSENCE_ACKNOWLEDGED,
    }
    receipt = harnessed.coordinator.confirm(
        workspace, confirm_command(harnessed, workspace, resolved=resolutions)
    )
    assert receipt.generation == 1
    assert receipt.discrepancy_status == "resolved"
    replay = harnessed.coordinator.confirm(
        workspace, confirm_command(harnessed, workspace, resolved=resolutions)
    )
    assert replay == receipt
    with pytest.raises(TicketIntakeServiceError, match="confirmation_generation_stale"):
        harnessed.coordinator.confirm(
            workspace,
            confirm_command(harnessed, workspace, command="cmd-other", resolved=resolutions),
        )
    second = harnessed.coordinator.confirm(
        workspace,
        confirm_command(
            harnessed,
            workspace,
            command="cmd-second",
            resolved=resolutions,
            expected_generation=1,
        ),
    )
    assert second.generation == 2
    # A changed photo no longer matches the review binding bound to the old images.
    other_ref = harnessed.service.upload_photo(workspace, photo(OTHER)).photo_ref
    form = harnessed.service.workspace_view(workspace).intake_form
    assert form is not None
    harnessed.service.save_intake_form(
        workspace,
        s.SaveIntakeForm(
            expected_revision=revision(harnessed, workspace),
            form=form.model_copy(update={"ticket_photo_ref": other_ref}),
        ),
    )
    with pytest.raises(TicketIntakeServiceError):
        harnessed.coordinator.confirm(
            workspace,
            confirm_command(
                harnessed,
                workspace,
                command="cmd-third",
                resolved=resolutions,
                expected_generation=2,
            ),
        )


# ---------------------------------------------------------------------------
# Story/contact edits keep confirmation; ticket edits invalidate by content
# ---------------------------------------------------------------------------


def test_story_edits_keep_envelope_and_confirmation_but_ticket_edits_invalidate(
    tmp_path: Path,
) -> None:
    binding = binding_for()
    client = ScriptedVisionClient(
        (reading_payload(), Usage(cost_cents=1)),
        (review_payload(binding.review_fields()), Usage(cost_cents=1)),
    )
    harnessed = harness(tmp_path / "pb.sqlite", client)
    workspace, binding, _front, _back = ready(harnessed, story="first words")
    harnessed.coordinator.request_reading(workspace, "req-extract", 2)
    harnessed.coordinator.request_review(workspace, "req-review", 2)
    harnessed.coordinator.confirm(workspace, confirm_command(harnessed, workspace))

    saved = harnessed.service.workspace_view(workspace).intake_form
    assert saved is not None
    story_edit = saved.model_copy(
        update={
            "values": {**saved.values, "story": "a completely different story"},
            "ticket_fact_binding": None,
            "step": 2,
        }
    )
    harnessed.service.save_intake_form(
        workspace,
        s.SaveIntakeForm(expected_revision=revision(harnessed, workspace), form=story_edit),
    )
    kept = harnessed.service.workspace_view(workspace).intake_form
    assert kept is not None
    assert kept.ticket_fact_binding is not None
    status = harnessed.coordinator.status(workspace)
    assert status.confirmation.state == "current"
    assert status.confirmation.generation == 1
    assert status.extraction.current is True
    assert status.review.current is True

    ticket_edit = kept.model_copy(
        update={
            "values": {**kept.values, "ticket.citation_number": "SYN-EDIT"},
            "ticket_fact_binding": None,
        }
    )
    harnessed.service.save_intake_form(
        workspace,
        s.SaveIntakeForm(expected_revision=revision(harnessed, workspace), form=ticket_edit),
    )
    dropped = harnessed.service.workspace_view(workspace).intake_form
    assert dropped is not None
    assert dropped.ticket_fact_binding is None
    assert harnessed.coordinator.status(workspace).confirmation.state == "stale"
    with pytest.raises(TicketIntakeServiceError, match="ticket_facts_required"):
        harnessed.coordinator.request_review(
            workspace, "req-review-2", revision(harnessed, workspace)
        )


# ---------------------------------------------------------------------------
# Submit compares ALL13 and the ordered image binding before any prehold
# ---------------------------------------------------------------------------


def test_submit_refuses_changed_facts_and_images_before_prehold(
    tmp_path: Path, fake_resolution: list[str]
) -> None:
    binding = binding_for()
    client = ScriptedVisionClient(
        (reading_payload(), Usage(cost_cents=1)),
        (review_payload(binding.review_fields()), Usage(cost_cents=1)),
    )
    harnessed = harness(tmp_path / "pb.sqlite", client)
    workspace, binding, front, back = ready(harnessed, story="my exact words")
    harnessed.coordinator.request_reading(workspace, "req-extract", 2)
    harnessed.coordinator.request_review(workspace, "req-review", 2)
    harnessed.coordinator.confirm(workspace, confirm_command(harnessed, workspace))
    reserved_id = reservation_id(harnessed, workspace)

    valid = submit_body(binding, front, back)
    without = valid.model_copy(update={"ticket_confirmation_command_id": None})
    with pytest.raises(ServiceConflict, match="ticket_confirmation_required"):
        harnessed.service.submit(workspace, without)
    assert fake_resolution == []

    changed_ticket = dict(binding.adopted)
    changed_ticket["violation_description"] = "A different description"
    with pytest.raises(ServiceConflict, match="ticket_facts_changed"):
        harnessed.service.submit(
            workspace, submit_body(binding, front, back, ticket=changed_ticket)
        )
    assert fake_resolution == []

    other_ref = harnessed.service.upload_photo(workspace, photo(OTHER)).photo_ref
    with pytest.raises(ServiceConflict, match="ticket_photo_changed"):
        harnessed.service.submit(workspace, submit_body(binding, other_ref, back))
    assert fake_resolution == []

    receipt = harnessed.service.submit(workspace, valid)

    assert receipt.case_id == reserved_id
    assert len(fake_resolution) == 1
    record = harnessed.store.get_intake(reserved_id)
    assert record is not None
    persisted = s.SubmitIntake.model_validate_json(record.intake_json)
    assert persisted.customer_position.statement == "my exact words"
    assert persisted.ticket_confirmation_command_id == "cmd-confirm"
    workspace_record = harnessed.store.get_workspace(workspace)
    assert workspace_record is not None
    assert workspace_record.workspace.position == "my exact words"

    replay = harnessed.service.submit(workspace, valid)
    assert replay == receipt
    assert len(fake_resolution) == 1


# ---------------------------------------------------------------------------
# Initial-mail recheck cannot be bypassed
# ---------------------------------------------------------------------------


def freeze_case(harnessed: Harness, workspace: str, intake_id: str) -> str:
    case = harnessed.store.start_case(
        workspace, "auth_ticket_test", NOW, NOW + timedelta(hours=1), CERTIFIED_CENTS
    )
    counter = iter(range(100))
    for event in (ResearchComplete(), DraftReady(), CustomerApproved()):
        case = harnessed.store.apply_letter_event(
            case.case_token,
            event,
            case.revision,
            occurred_at=NOW,
            new_effect_id=lambda: f"eff_{next(counter)}",
        )
    record = harnessed.store.get_intake(intake_id)
    assert record is not None
    harnessed.store.update_intake(
        intake_id,
        record.revision,
        selected_tier="CERTIFIED",
        case_token=case.case_token,
    )
    assert case.letter_state is CaseState.FROZEN
    return case.case_token


def test_initial_mail_recheck_refuses_tampered_immutable_intake(
    tmp_path: Path, fake_resolution: list[str]
) -> None:
    binding = binding_for()
    client = ScriptedVisionClient(
        (reading_payload(), Usage(cost_cents=1)),
        (review_payload(binding.review_fields()), Usage(cost_cents=1)),
    )
    harnessed = harness(tmp_path / "pb.sqlite", client)
    workspace, binding, front, back = ready(harnessed)
    harnessed.coordinator.request_reading(workspace, "req-extract", 2)
    harnessed.coordinator.request_review(workspace, "req-review", 2)
    harnessed.coordinator.confirm(workspace, confirm_command(harnessed, workspace))
    receipt = harnessed.service.submit(workspace, submit_body(binding, front, back))
    case_token = freeze_case(harnessed, workspace, receipt.case_id)

    harnessed.service._require_ticket_confirmation_for_mailing(case_token)

    record = harnessed.store.get_intake(receipt.case_id)
    assert record is not None
    tampered = s.SubmitIntake.model_validate_json(record.intake_json)
    tampered_ticket = tampered.ticket.model_dump()
    tampered_ticket["citation_number"] = "SYN-TAMPER"
    tampered_json = tampered.model_copy(
        update={"ticket": s.TicketFacts.model_validate(tampered_ticket)}
    ).model_dump_json()
    harnessed.store._require_open().execute(
        "UPDATE pb_intake_records SET intake_json=? WHERE intake_id=?",
        (tampered_json, receipt.case_id),
    )

    with pytest.raises(ServiceConflict, match="ticket_facts_changed"):
        harnessed.service._require_ticket_confirmation_for_mailing(case_token)
    lob = harnessed.service._deps.lob
    assert isinstance(lob, AcceptingLobClient)
    with pytest.raises(ServiceConflict):
        harnessed.service.dispatch(receipt.case_id)
    assert lob.by_key == {}


# ---------------------------------------------------------------------------
# Restore/readback after a restart
# ---------------------------------------------------------------------------


def test_status_restores_from_durable_claims_without_calling_provider(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    binding = binding_for()
    client = ScriptedVisionClient(
        (reading_payload(), Usage(cost_cents=1)),
        (review_payload(binding.review_fields()), Usage(cost_cents=1)),
    )
    first = harness(path, client)
    workspace, binding, _front, _back = ready(first)
    first.coordinator.request_reading(workspace, "req-extract", 2)
    first.coordinator.request_review(workspace, "req-review", 2)
    first.coordinator.confirm(workspace, confirm_command(first, workspace))
    intake_id = reservation_id(first, workspace)
    first.store.close()

    silent = ScriptedVisionClient()
    restored = harness(path, silent, photos=first.photos)
    status = restored.coordinator.status(workspace)

    assert status.enabled is True
    assert status.required is True
    assert status.extraction.status == "success"
    assert status.extraction.current is True
    assert status.review.status == "success"
    assert status.review.current is True
    assert status.confirmation.state == "current"
    assert status.confirmation.command_id == "cmd-confirm"
    assert status.next_confirmation_generation == 1
    assert silent.calls == []
    reservation = restored.intake.reservation_for(workspace)
    assert reservation is not None
    assert reservation.intake_id == intake_id
