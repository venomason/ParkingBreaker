"""Owner-scoped orchestration of ticket reading, independent review and confirmation.

This is the one injected coordinator that turns the accepted pure primitives
(:mod:`parkingbreaker.ticket_vision`, :mod:`parkingbreaker.ticket_fact_binding`) and
the accepted durable store (:mod:`parkingbreaker.ticket_intake_store`) into the actual
customer path. It reads no environment, constructs no provider and makes no call of
its own: the reader, clock, id factory, photo store and authority connection are
injected by the composition root.

Guarantees kept here:

* one reserved intake id per owned workspace, allocated before the first vision call
  and reused as the later case/prehold subject, so extraction, review and prehold share
  one existing 50-cent intake-reading ledger;
* claims are persisted before any provider call, and a replay/alias/pending/unknown
  claim never calls the provider again;
* only the owner-scoped original front/back images and ``binding.review_fields()`` ever
  reach the review model; story, contact, extraction output and adopted-only facts do not;
* a confirmation needs a completed successful review bound to the exact current images
  and full closed-field digest, and every non-match comparison is explicitly resolved;
* submit compares ALL13 adopted facts and the ordered original images before any prehold,
  and the same immutable facts/images/review are revalidated again at initial mailing.
"""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from PIL import Image, UnidentifiedImageError

from parkingbreaker.api import schemas as s
from parkingbreaker.blob_store import BlobStore
from parkingbreaker.intake_store import IntakeRecord
from parkingbreaker.llm import ImageAttachment, LlmBudgetExceeded, Usage
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.ticket_fact_binding import (
    TicketFactBinding,
    TicketFactBindingError,
    ordered_images_digest,
)
from parkingbreaker.ticket_intake_store import (
    ClaimStatus,
    ConfirmationRead,
    CustomerConfirmation,
    DiscrepancyStatus,
    IntakeOperation,
    OperationClaim,
    TicketIntakeConflict,
    TicketIntakeStore,
    review_confirmation_binding,
)
from parkingbreaker.ticket_vision import (
    EXTRACT_OPERATION,
    FieldReading,
    FieldReview,
    TicketField,
    TicketVisionInputError,
    TicketVisionReader,
    input_binding_hash,
    parse_reading,
    parse_review,
)
from parkingbreaker.transaction_store import SqliteTransactionStore

RESULT_VERSION: Final[int] = 1
RESULT_KIND_READING: Final[str] = "reading"
RESULT_KIND_REVIEW: Final[str] = "review"
DEFAULT_EXTRACTION_CEILING_CENTS: Final[int] = 25
DEFAULT_REVIEW_CEILING_CENTS: Final[int] = 25
_LITERAL_FIELDS: Final[frozenset[str]] = frozenset(field.value for field in TicketField)


class TicketIntakeServiceError(ValueError):
    """A refused ticket-intake request; carries only a stable reason key."""


def _error(reason: str) -> TicketIntakeServiceError:
    return TicketIntakeServiceError(reason)


@dataclass(frozen=True)
class TicketIntakeDeps:
    """Everything this coordinator needs, injected at the composition root."""

    store: SqliteTransactionStore
    intake: TicketIntakeStore
    photos: BlobStore
    clock: Callable[[], datetime]
    new_id: Callable[[], str]
    reader_for_subject: Callable[[str], TicketVisionReader]
    required: bool = False
    synthetic: bool = False
    extraction_ceiling_cents: int = DEFAULT_EXTRACTION_CEILING_CENTS
    review_ceiling_cents: int = DEFAULT_REVIEW_CEILING_CENTS


@dataclass(frozen=True)
class _Current:
    owner: str
    revision: int
    form: s.IntakeForm
    front_ref: str
    back_ref: str | None
    images: tuple[ImageAttachment, ...]
    images_digest: str
    binding: TicketFactBinding | None


@dataclass(frozen=True)
class _ReviewRecord:
    fields: tuple[FieldReview, ...]
    synthetic: bool
    images_digest: str
    fields_digest: str
    literal: tuple[tuple[str, str | None], ...]
    adopted: tuple[tuple[str, str | None], ...]
    customer_supplied: bool = False

    @property
    def envelope(self) -> TicketFactBinding:
        return TicketFactBinding.bind(dict(self.literal), dict(self.adopted))


def _usage_json(usage: Usage) -> str:
    return json.dumps(
        {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_cents": usage.cost_cents,
        },
        separators=(",", ":"),
    )


def _reading_result_json(
    fields: Sequence[FieldReading],
    *,
    refused: bool,
    input_hash: str,
    synthetic: bool,
    primary_model: str | None = None,
    escalation: str | None = None,
) -> str:
    return json.dumps(
        {
            "v": RESULT_VERSION,
            "kind": RESULT_KIND_READING,
            "synthetic": synthetic,
            "refused": refused,
            "input_hash": input_hash,
            "primary_model": primary_model,
            "escalation": escalation,
            "fields": {
                item.field.value: {
                    "state": item.state.value,
                    "value": item.value,
                    "confidence": item.confidence,
                }
                for item in fields
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _review_result_json(
    review_fields: tuple[FieldReview, ...],
    confirmed: Mapping[str, str],
    binding: TicketFactBinding,
    images_digest: str,
    *,
    refused: bool,
    input_hash: str,
    synthetic: bool,
) -> str:
    return json.dumps(
        {
            "v": RESULT_VERSION,
            "kind": RESULT_KIND_REVIEW,
            "synthetic": synthetic,
            "refused": refused,
            "input_hash": input_hash,
            "images_digest": images_digest,
            "fields_digest": binding.closed_field_digest,
            "literal": [[key, value] for key, value in binding.literal],
            "adopted": [[key, value] for key, value in binding.adopted],
            "confirmed": dict(confirmed),
            "fields": {
                item.field.value: {
                    "comparison": item.comparison.value,
                    "visible_text": item.visible_text,
                }
                for item in review_fields
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _refusal_result_json(kind: str, *, synthetic: bool, input_hash: str) -> str:
    return json.dumps(
        {
            "v": RESULT_VERSION,
            "kind": kind,
            "synthetic": synthetic,
            "refused": True,
            "input_hash": input_hash,
            "fields": {},
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _json_object(text: str | None) -> dict[str, object]:
    if text is None:
        raise StorageCorruption("missing ticket intake result")
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        raise StorageCorruption("malformed ticket intake result") from None
    if not isinstance(data, dict):
        raise StorageCorruption("malformed ticket intake result")
    return data


def _digest_text(value: object, what: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise StorageCorruption(f"malformed {what}")
    return value


def _parse_stored_reading(claim: OperationClaim) -> tuple[tuple[s.TicketReadingField, ...], bool]:
    data = _json_object(claim.result_json)
    if bool(data.get("refused")):
        return (), True
    fields = data.get("fields")
    if not isinstance(fields, dict):
        raise StorageCorruption("malformed ticket reading")
    parsed = parse_reading(fields)
    if parsed is None:
        raise StorageCorruption("malformed ticket reading")
    return (
        tuple(
            s.TicketReadingField(
                field=item.field,
                state=item.state.value,
                value=item.value,
                confidence=item.confidence,
            )
            for item in parsed
        ),
        False,
    )


def _customer_supplied(data: Mapping[str, object]) -> bool:
    value = data.get("customer_supplied", False)
    if type(value) is not bool:
        raise StorageCorruption("malformed review provenance")
    if value and (data.get("fields") != {} or data.get("confirmed") != {}):
        raise StorageCorruption("customer facts cannot contain model comparisons")
    return value


def _parse_stored_review(claim: OperationClaim) -> tuple[tuple[s.TicketReviewField, ...], bool]:
    data = _json_object(claim.result_json)
    if bool(data.get("refused")):
        return (), True
    comparisons = data.get("fields")
    confirmed = data.get("confirmed")
    if not isinstance(comparisons, dict) or not isinstance(confirmed, dict):
        raise StorageCorruption("malformed ticket review")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in confirmed.items()):
        raise StorageCorruption("malformed ticket review")
    parsed = () if _customer_supplied(data) else parse_review(comparisons, dict(confirmed))
    if parsed is None:
        raise StorageCorruption("malformed ticket review")
    return (
        tuple(
            s.TicketReviewField(
                field=item.field,
                comparison=item.comparison.value,
                visible_text=item.visible_text,
            )
            for item in parsed
        ),
        False,
    )


def _parse_review_record(claim: OperationClaim) -> _ReviewRecord:
    data = _json_object(claim.result_json)
    comparisons = data.get("fields")
    confirmed = data.get("confirmed")
    literal = data.get("literal")
    adopted = data.get("adopted")
    if (
        not isinstance(comparisons, dict)
        or not isinstance(confirmed, dict)
        or not isinstance(literal, list)
        or not isinstance(adopted, list)
    ):
        raise StorageCorruption("malformed ticket review")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in confirmed.items()):
        raise StorageCorruption("malformed ticket review")
    fields = () if _customer_supplied(data) else parse_review(comparisons, dict(confirmed))
    if fields is None:
        raise StorageCorruption("malformed ticket review")
    try:
        binding = TicketFactBinding.bind(
            {str(item[0]): item[1] for item in literal},
            {str(item[0]): item[1] for item in adopted},
        )
    except (IndexError, TypeError, TicketFactBindingError):
        raise StorageCorruption("malformed ticket review envelope") from None
    return _ReviewRecord(
        fields,
        bool(data.get("synthetic")),
        _digest_text(data.get("images_digest"), "ticket review images digest"),
        _digest_text(data.get("fields_digest"), "ticket review fields digest"),
        binding.literal,
        binding.adopted,
        _customer_supplied(data),
    )


def _expected_resolution(comparison: str) -> s.TicketDiscrepancyResolution:
    if comparison == "conflict":
        return s.TicketDiscrepancyResolution.CONFIRMED_VALUE_STANDS
    if comparison == "not_shown":
        return s.TicketDiscrepancyResolution.ABSENCE_ACKNOWLEDGED
    return s.TicketDiscrepancyResolution.UNREADABLE_ACKNOWLEDGED


def _media_type(data: bytes) -> str:
    """Detect JPEG/PNG from the stored bytes structurally; never trust a client label."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            detected = image.format
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        SyntaxError,
        ValueError,
    ):
        raise _error("ticket_photo_not_supported") from None
    if detected == "JPEG":
        return "image/jpeg"
    if detected == "PNG":
        return "image/png"
    raise _error("ticket_photo_not_supported")


class TicketIntakeCoordinator:
    """Injected, owner-scoped ticket-intake path over one authority connection."""

    def __init__(self, deps: TicketIntakeDeps) -> None:
        if type(deps.required) is not bool or type(deps.synthetic) is not bool:
            raise ValueError("ticket intake flags must be booleans")
        for name, value in (
            ("extraction_ceiling_cents", deps.extraction_ceiling_cents),
            ("review_ceiling_cents", deps.review_ceiling_cents),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self._deps = deps

    @property
    def required(self) -> bool:
        return self._deps.required

    @property
    def enabled(self) -> bool:
        return True

    @property
    def synthetic(self) -> bool:
        return self._deps.synthetic

    # Workspace and current-input preflight ------------------------------------------
    def _workspace(self, workspace_id: str) -> tuple[str, int, s.IntakeForm | None]:
        saved = self._deps.store.get_workspace(workspace_id)
        owner = self._deps.store.workspace_owner(workspace_id)
        if saved is None or owner is None:
            raise _error("not_found")
        raw = saved.workspace.answers.get("web_intake_v1")
        if raw is None:
            return owner, saved.revision, None
        try:
            form = s.IntakeForm.model_validate_json(raw)
        except ValueError:
            raise _error("ticket_form_invalid") from None
        return owner, saved.revision, form

    def _attachments(
        self, owner: str, front_ref: str | None, back_ref: str | None
    ) -> tuple[ImageAttachment, ...]:
        if front_ref is None:
            raise _error("ticket_photo_required")
        attachments: list[ImageAttachment] = []
        for ref in (front_ref, back_ref):
            if ref is None:
                continue
            data = self._deps.photos.get(owner, ref)
            if data is None:
                raise _error("ticket_photo_not_found")
            attachments.append(
                ImageAttachment(
                    media_type=_media_type(data),
                    data_base64=base64.b64encode(data).decode("ascii"),
                )
            )
        return tuple(attachments)

    def _load_current(self, workspace_id: str, *, require_binding: bool) -> _Current:
        owner, revision, form = self._workspace(workspace_id)
        if form is None:
            raise _error("ticket_form_required")
        images = self._attachments(owner, form.ticket_photo_ref, form.ticket_back_photo_ref)
        try:
            images_digest = ordered_images_digest(images)
        except TicketFactBindingError:
            raise _error("ticket_photo_not_supported") from None
        binding = None if form.ticket_fact_binding is None else form.ticket_fact_binding.binding
        if require_binding and binding is None:
            raise _error("ticket_facts_required")
        return _Current(
            owner,
            revision,
            form,
            form.ticket_photo_ref or "",
            form.ticket_back_photo_ref,
            images,
            images_digest,
            binding,
        )

    def _load_current_or_none(self, workspace_id: str) -> _Current | None:
        try:
            return self._load_current(workspace_id, require_binding=False)
        except TicketIntakeServiceError:
            return None

    # Reservation and claims ---------------------------------------------------------
    def reserved_intake_id(self, workspace_id: str, owner: str, proposed_intake_id: str) -> str:
        """Return (reserving once if needed) the one intake subject for this workspace."""
        if self._deps.store.workspace_owner(workspace_id) != owner:
            raise _error("not_found")
        reservation = self._deps.intake.reservation_for(workspace_id)
        if reservation is not None:
            return reservation.intake_id
        try:
            reserved = self._deps.intake.reserve_intake(
                proposed_intake_id, workspace_id, owner, self._deps.clock()
            )
        except TicketIntakeConflict as exc:
            raise _error(str(exc)) from None
        return reserved.intake_id

    def _reserve(self, workspace_id: str, current: _Current) -> str:
        reservation = self._deps.intake.reservation_for(workspace_id)
        if reservation is not None:
            return reservation.intake_id
        if self._deps.store.intake_for_workspace(workspace_id) is not None:
            raise _error("intake_already_submitted")
        return self.reserved_intake_id(workspace_id, current.owner, f"case_{self._deps.new_id()}")

    def _claim(
        self,
        request_id: str,
        intake_id: str,
        operation: IntakeOperation,
        binding_sha256: str,
        workspace_id: str,
        owner: str,
    ) -> OperationClaim:
        try:
            return self._deps.intake.claim_operation(
                request_id,
                intake_id,
                operation,
                binding_sha256,
                self._deps.clock(),
                workspace_token=workspace_id,
                owner=owner,
            )
        except TicketIntakeConflict as exc:
            raise _error(str(exc)) from None

    def _finish(
        self,
        claim: OperationClaim,
        status: ClaimStatus,
        result_json: str | None,
        usage_json: str | None,
    ) -> OperationClaim:
        token = claim.claim_token
        if token is None:
            raise _error("claim_token_required")
        try:
            return self._deps.intake.complete_operation(
                claim.request_id,
                status,
                result_json,
                usage_json,
                self._deps.clock(),
                claim_token=token,
                intake_id=claim.intake_id,
                operation=claim.operation,
                binding_sha256=claim.binding_sha256,
            )
        except TicketIntakeConflict as exc:
            raise _error(str(exc)) from None

    # Reading ------------------------------------------------------------------------
    def request_reading(
        self, workspace_id: str, request_id: str, expected_revision: int
    ) -> s.TicketReadingSnapshot:
        current = self._load_current(workspace_id, require_binding=False)
        if expected_revision != current.revision:
            raise _error("workspace_changed_reload_required")
        binding_sha256 = input_binding_hash(EXTRACT_OPERATION, current.images)
        existing = self._deps.intake.operation_claim(request_id)
        if existing is not None:
            self._validate_replay(
                existing, current.owner, workspace_id, IntakeOperation.EXTRACT, binding_sha256
            )
            return self._reading_snapshot(existing, current)
        if self._deps.store.intake_for_workspace(workspace_id) is not None:
            raise _error("intake_already_submitted")
        intake_id = self._reserve(workspace_id, current)
        claim = self._claim(
            request_id,
            intake_id,
            IntakeOperation.EXTRACT,
            binding_sha256,
            workspace_id,
            current.owner,
        )
        if not claim.should_call:
            return self._reading_snapshot(claim, current)
        reader = self._deps.reader_for_subject(intake_id)
        try:
            reading = reader.extract(
                list(current.images), max_cost_cents=self._deps.extraction_ceiling_cents
            )
        except LlmBudgetExceeded:
            self._finish(claim, ClaimStatus.REFUSED, None, _usage_json(Usage()))
            raise _error("ticket_reading_budget_exhausted") from None
        except TicketVisionInputError:
            self._finish(claim, ClaimStatus.REFUSED, None, _usage_json(Usage()))
            raise _error("ticket_photo_not_supported") from None
        except Exception:
            self._finish(claim, ClaimStatus.UNKNOWN, None, None)
            raise _error("ticket_reading_provider_unavailable") from None
        if reading.refused:
            completed = self._finish(
                claim,
                ClaimStatus.REFUSED,
                _refusal_result_json(
                    RESULT_KIND_READING,
                    synthetic=self._deps.synthetic,
                    input_hash=reading.input_hash,
                ),
                _usage_json(reading.usage),
            )
        else:
            completed = self._finish(
                claim,
                ClaimStatus.SUCCESS,
                _reading_result_json(
                    reading.fields,
                    refused=False,
                    input_hash=reading.input_hash,
                    synthetic=self._deps.synthetic,
                    primary_model=reading.primary_model,
                    escalation=reading.escalation,
                ),
                _usage_json(reading.usage),
            )
        return self._reading_snapshot(completed, current)

    def _validate_replay(
        self,
        claim: OperationClaim,
        owner: str,
        workspace_id: str,
        operation: IntakeOperation,
        binding_sha256: str,
    ) -> None:
        if (
            claim.owner != owner
            or claim.workspace_token != workspace_id
            or claim.operation is not operation
            or claim.binding_sha256 != binding_sha256
        ):
            raise _error("request_id_reused_for_another_claim")

    def _reading_snapshot(
        self, claim: OperationClaim | None, current: _Current | None
    ) -> s.TicketReadingSnapshot:
        synthetic = self._deps.synthetic
        if claim is None:
            return s.TicketReadingSnapshot(synthetic=synthetic)
        fields: tuple[s.TicketReadingField, ...] = ()
        if claim.status is ClaimStatus.SUCCESS or (
            claim.status is ClaimStatus.REFUSED and claim.result_json is not None
        ):
            fields, _refused = _parse_stored_reading(claim)
        is_current = False
        if current is not None:
            is_current = claim.binding_sha256 == input_binding_hash(
                EXTRACT_OPERATION, current.images
            )
        metadata = _json_object(claim.result_json) if claim.result_json is not None else {}
        return s.TicketReadingSnapshot.model_validate(
            {
                "request_id": claim.request_id,
                "status": claim.status.value,
                "current": is_current,
                "synthetic": synthetic,
                "fields": list(fields),
                "primary_model": metadata.get("primary_model"),
                "escalation": metadata.get("escalation"),
            }
        )

    def _validate_literal_provenance(
        self, workspace_id: str, images: Sequence[ImageAttachment], binding: TicketFactBinding
    ) -> None:
        expected: dict[str, str | None] = {field.value: None for field in TicketField}
        reservation = self._deps.intake.reservation_for(workspace_id)
        extraction_hash = input_binding_hash(EXTRACT_OPERATION, images)
        if reservation is not None:
            for claim in self._deps.intake.claims_for(reservation.intake_id):
                if (
                    claim.operation is IntakeOperation.EXTRACT
                    and claim.binding_sha256 == extraction_hash
                ):
                    if claim.status is ClaimStatus.SUCCESS:
                        fields, refused = _parse_stored_reading(claim)
                        if refused:
                            raise StorageCorruption("successful reading marked refused")
                        expected = {
                            item.field.value: item.value if item.state == "shown" else None
                            for item in fields
                        }
                    break
        if dict(binding.literal) != expected:
            raise _error("ticket_reading_provenance_mismatch")

    # Review -------------------------------------------------------------------------
    def request_review(
        self,
        workspace_id: str,
        request_id: str,
        expected_revision: int,
        *,
        customer_supplied: bool = False,
    ) -> s.TicketReviewSnapshot:
        current = self._load_current(workspace_id, require_binding=True)
        if expected_revision != current.revision:
            raise _error("workspace_changed_reload_required")
        binding = current.binding
        assert binding is not None
        self._validate_literal_provenance(workspace_id, current.images, binding)
        review_fields = binding.review_fields()
        customer_supplied = customer_supplied or not review_fields
        binding_sha256 = review_confirmation_binding(
            current.images_digest, binding.closed_field_digest, customer_supplied=customer_supplied
        )
        existing = self._deps.intake.operation_claim(request_id)
        if existing is not None:
            self._validate_replay(
                existing, current.owner, workspace_id, IntakeOperation.REVIEW, binding_sha256
            )
            return self._review_snapshot(existing, current)
        if self._deps.store.intake_for_workspace(workspace_id) is not None:
            raise _error("intake_already_submitted")
        intake_id = self._reserve(workspace_id, current)
        claim = self._claim(
            request_id,
            intake_id,
            IntakeOperation.REVIEW,
            binding_sha256,
            workspace_id,
            current.owner,
        )
        if not claim.should_call:
            return self._review_snapshot(claim, current)
        if customer_supplied:
            data = json.loads(
                _review_result_json(
                    (),
                    {},
                    binding,
                    current.images_digest,
                    refused=False,
                    input_hash="",
                    synthetic=self._deps.synthetic,
                )
            )
            data["customer_supplied"] = True
            completed = self._finish(
                claim, ClaimStatus.SUCCESS, json.dumps(data), _usage_json(Usage())
            )
            return self._review_snapshot(completed, current)
        reader = self._deps.reader_for_subject(intake_id)
        try:
            review = reader.review(
                list(current.images),
                review_fields,
                max_cost_cents=self._deps.review_ceiling_cents,
            )
        except LlmBudgetExceeded:
            self._finish(claim, ClaimStatus.REFUSED, None, _usage_json(Usage()))
            raise _error("ticket_review_budget_exhausted") from None
        except TicketVisionInputError:
            self._finish(claim, ClaimStatus.REFUSED, None, _usage_json(Usage()))
            raise _error("ticket_photo_not_supported") from None
        except Exception:
            self._finish(claim, ClaimStatus.UNKNOWN, None, None)
            raise _error("ticket_review_provider_unavailable") from None
        if review.refused:
            completed = self._finish(
                claim,
                ClaimStatus.REFUSED,
                _refusal_result_json(
                    RESULT_KIND_REVIEW,
                    synthetic=self._deps.synthetic,
                    input_hash=review.input_hash,
                ),
                _usage_json(review.usage),
            )
        else:
            completed = self._finish(
                claim,
                ClaimStatus.SUCCESS,
                _review_result_json(
                    review.fields,
                    review_fields,
                    binding,
                    current.images_digest,
                    refused=False,
                    input_hash=review.input_hash,
                    synthetic=self._deps.synthetic,
                ),
                _usage_json(review.usage),
            )
        return self._review_snapshot(completed, current)

    def _review_snapshot(
        self, claim: OperationClaim | None, current: _Current | None
    ) -> s.TicketReviewSnapshot:
        synthetic = self._deps.synthetic
        if claim is None:
            return s.TicketReviewSnapshot(synthetic=synthetic)
        fields: tuple[s.TicketReviewField, ...] = ()
        if claim.status is ClaimStatus.SUCCESS or (
            claim.status is ClaimStatus.REFUSED and claim.result_json is not None
        ):
            fields, _refused = _parse_stored_review(claim)
        customer_supplied = claim.status is ClaimStatus.SUCCESS and _customer_supplied(
            _json_object(claim.result_json)
        )
        is_current = False
        if current is not None and current.binding is not None:
            is_current = claim.binding_sha256 == review_confirmation_binding(
                current.images_digest,
                current.binding.closed_field_digest,
                customer_supplied=customer_supplied,
            )
        return s.TicketReviewSnapshot(
            request_id=claim.request_id,
            status=claim.status.value,
            current=is_current,
            synthetic=synthetic,
            fields=list(fields),
            basis="customer-supplied facts; no model comparison"
            if customer_supplied
            else "model comparison only; not external verification",
        )

    # Confirmation -------------------------------------------------------------------
    def _latest_confirmation(
        self, intake_id: str, workspace_id: str, owner: str
    ) -> CustomerConfirmation | None:
        sentinel = "0" * 64
        try:
            probe = self._deps.intake.current_confirmation(
                intake_id,
                images_sha256=sentinel,
                fields_sha256=sentinel,
                revision=1,
                generation=1,
                workspace_token=workspace_id,
                owner=owner,
            )
        except (TicketIntakeConflict, StorageCorruption):
            raise _error("ticket_confirmation_unavailable") from None
        return probe.confirmation

    def _freshness(
        self,
        confirmation: CustomerConfirmation,
        intake_id: str,
        workspace_id: str,
        owner: str,
        images_digest: str,
        fields_digest: str,
    ) -> ConfirmationRead:
        try:
            return self._deps.intake.current_confirmation(
                intake_id,
                images_sha256=images_digest,
                fields_sha256=fields_digest,
                revision=confirmation.revision,
                generation=confirmation.generation,
                review_request_id=confirmation.review_request_id,
                workspace_token=workspace_id,
                owner=owner,
            )
        except (TicketIntakeConflict, StorageCorruption):
            raise _error("ticket_confirmation_unavailable") from None

    def _review_claim_for(self, review_request_id: str, intake_id: str) -> OperationClaim:
        claim = self._deps.intake.operation_claim(review_request_id)
        if claim is None:
            raise _error("ticket_review_not_found")
        if claim.intake_id != intake_id or claim.operation is not IntakeOperation.REVIEW:
            raise _error("ticket_review_not_found")
        if claim.status is not ClaimStatus.SUCCESS or claim.result_json is None:
            raise _error("ticket_review_not_completed")
        return claim

    def confirm(
        self,
        workspace_id: str,
        command: s.TicketConfirmationRequest,
    ) -> s.TicketConfirmationReceipt:
        if not command.accuracy_acknowledged:
            raise _error("ticket_accuracy_confirmation_required")
        current = self._load_current(workspace_id, require_binding=True)
        if command.expected_revision != current.revision:
            raise _error("workspace_changed_reload_required")
        if self._deps.store.intake_for_workspace(workspace_id) is not None:
            raise _error("intake_already_submitted")
        binding = current.binding
        assert binding is not None
        reservation = self._deps.intake.reservation_for(workspace_id)
        if reservation is None:
            raise _error("ticket_review_not_found")
        self._validate_literal_provenance(workspace_id, current.images, binding)
        claim = self._review_claim_for(command.review_request_id, reservation.intake_id)
        record = _parse_review_record(claim)
        expected_binding = review_confirmation_binding(
            current.images_digest,
            binding.closed_field_digest,
            customer_supplied=record.customer_supplied,
        )
        if claim.binding_sha256 != expected_binding:
            raise _error("ticket_review_binding_mismatch")
        if (
            record.images_digest != current.images_digest
            or record.fields_digest != binding.closed_field_digest
            or record.envelope != binding
        ):
            raise _error("ticket_review_binding_mismatch")
        discrepancy = self._discrepancy_status(record.fields, command.resolved_fields)
        latest = self._latest_confirmation(reservation.intake_id, workspace_id, current.owner)
        if latest is not None and latest.command_id == command.command_id:
            if (
                latest.images_sha256 == current.images_digest
                and latest.fields_sha256 == binding.closed_field_digest
                and latest.review_request_id == command.review_request_id
                and latest.revision == command.expected_revision
                and latest.discrepancy_status is discrepancy
            ):
                return self._receipt(latest)
            raise _error("confirmation_command_reused")
        expected_generation = 0 if latest is None else latest.generation
        if command.expected_confirmation_generation != expected_generation:
            raise _error("confirmation_generation_stale")
        try:
            recorded = self._deps.intake.record_confirmation(
                command.command_id,
                reservation.intake_id,
                current.images_digest,
                binding.closed_field_digest,
                command.review_request_id,
                command.expected_revision,
                expected_generation + 1,
                discrepancy,
                self._deps.clock(),
                workspace_token=workspace_id,
                owner=current.owner,
            )
        except TicketIntakeConflict as exc:
            raise _error(str(exc)) from None
        return self._receipt(recorded)

    @staticmethod
    def _discrepancy_status(
        fields: Sequence[FieldReview],
        resolved: Mapping[str, s.TicketDiscrepancyResolution],
    ) -> DiscrepancyStatus:
        if any(key not in _LITERAL_FIELDS for key in resolved):
            raise _error("discrepancy_resolution_invalid")
        discrepancies = 0
        for item in fields:
            if item.comparison.value == "match":
                if item.field.value in resolved:
                    raise _error("discrepancy_resolution_unexpected")
                continue
            discrepancies += 1
            expected = _expected_resolution(item.comparison.value)
            if resolved.get(item.field.value) != expected:
                raise _error("discrepancy_resolution_required")
        if len(resolved) != discrepancies:
            raise _error("discrepancy_resolution_invalid")
        return DiscrepancyStatus.NONE if not discrepancies else DiscrepancyStatus.RESOLVED

    def _receipt(self, confirmation: CustomerConfirmation) -> s.TicketConfirmationReceipt:
        return s.TicketConfirmationReceipt(
            command_id=confirmation.command_id,
            generation=confirmation.generation,
            review_request_id=confirmation.review_request_id,
            discrepancy_status=confirmation.discrepancy_status.value,
            synthetic=self._deps.synthetic,
        )

    # Status restore -----------------------------------------------------------------
    def status(self, workspace_id: str) -> s.TicketReadingStatus:
        _owner, _revision, _form = self._workspace(workspace_id)
        reservation = self._deps.intake.reservation_for(workspace_id)
        if reservation is None:
            return s.TicketReadingStatus(
                enabled=True, required=self._deps.required, synthetic=self._deps.synthetic
            )
        claims = self._deps.intake.claims_for(reservation.intake_id)
        extraction_claim = next(
            (claim for claim in reversed(claims) if claim.operation is IntakeOperation.EXTRACT),
            None,
        )
        review_claim = next(
            (claim for claim in reversed(claims) if claim.operation is IntakeOperation.REVIEW),
            None,
        )
        current = self._load_current_or_none(workspace_id)
        latest = self._latest_confirmation(reservation.intake_id, workspace_id, _owner)
        confirmation = self._confirmation_snapshot(
            reservation.intake_id, workspace_id, _owner, latest, current
        )
        return s.TicketReadingStatus(
            enabled=True,
            required=self._deps.required,
            synthetic=self._deps.synthetic,
            extraction=self._reading_snapshot(extraction_claim, current),
            review=self._review_snapshot(review_claim, current),
            confirmation=confirmation,
            next_confirmation_generation=0 if latest is None else latest.generation,
        )

    def _confirmation_snapshot(
        self,
        intake_id: str,
        workspace_id: str,
        owner: str,
        latest: CustomerConfirmation | None,
        current: _Current | None,
    ) -> s.TicketConfirmationSnapshot:
        if latest is None:
            return s.TicketConfirmationSnapshot(state="missing")
        base = s.TicketConfirmationSnapshot(
            state="stale",
            command_id=latest.command_id,
            review_request_id=latest.review_request_id,
            generation=latest.generation,
            discrepancy_status=latest.discrepancy_status.value,
        )
        if current is None or current.binding is None:
            return base.model_copy(update={"reason": "current_inputs_unavailable"})
        read = self._freshness(
            latest,
            intake_id,
            workspace_id,
            owner,
            current.images_digest,
            current.binding.closed_field_digest,
        )
        if read.current:
            return base.model_copy(update={"state": "current", "reason": None})
        return base.model_copy(update={"reason": read.reason or "confirmation_stale"})

    # Submit and initial-mail revalidation -------------------------------------------
    def require_submission_confirmation(self, workspace_id: str, body: s.SubmitIntake) -> None:
        current = self._load_current(workspace_id, require_binding=True)
        binding = current.binding
        assert binding is not None
        self._validate_literal_provenance(workspace_id, current.images, binding)
        if not binding.matches(body.ticket):
            raise _error("ticket_facts_changed")
        if (
            body.citation_photo_ref != current.front_ref
            or body.citation_back_photo_ref != current.back_ref
        ):
            raise _error("ticket_photo_changed")
        command_id = body.ticket_confirmation_command_id
        if command_id is None:
            raise _error("ticket_confirmation_required")
        reservation = self._deps.intake.reservation_for(workspace_id)
        if reservation is None:
            raise _error("ticket_confirmation_required")
        latest = self._latest_confirmation(reservation.intake_id, workspace_id, current.owner)
        if latest is None or latest.command_id != command_id:
            raise _error("ticket_confirmation_stale")
        read = self._freshness(
            latest,
            reservation.intake_id,
            workspace_id,
            current.owner,
            current.images_digest,
            binding.closed_field_digest,
        )
        if not read.current:
            raise _error("ticket_confirmation_stale")

    def revalidate_for_mailing(self, record: IntakeRecord, body: s.SubmitIntake) -> None:
        """Recheck the immutable intake facts/images/review before the original mailing."""
        command_id = body.ticket_confirmation_command_id
        if command_id is None:
            raise _error("ticket_confirmation_required")
        reservation = self._deps.intake.reservation_for(record.workspace_token)
        if reservation is None or reservation.intake_id != record.intake_id:
            raise _error("ticket_confirmation_required")
        latest = self._latest_confirmation(
            reservation.intake_id, record.workspace_token, record.owner
        )
        if latest is None or latest.command_id != command_id:
            raise _error("ticket_confirmation_stale")
        claim = self._deps.intake.operation_claim(latest.review_request_id)
        if (
            claim is None
            or claim.intake_id != reservation.intake_id
            or claim.operation is not IntakeOperation.REVIEW
            or claim.status is not ClaimStatus.SUCCESS
        ):
            raise _error("ticket_confirmation_stale")
        record_review = _parse_review_record(claim)
        if (
            record_review.images_digest != latest.images_sha256
            or record_review.fields_digest != latest.fields_sha256
            or latest.fields_sha256 != record_review.envelope.closed_field_digest
            or claim.binding_sha256
            != review_confirmation_binding(
                latest.images_sha256,
                latest.fields_sha256,
                customer_supplied=record_review.customer_supplied,
            )
        ):
            raise _error("ticket_confirmation_stale")
        if not record_review.envelope.matches(body.ticket):
            raise _error("ticket_facts_changed")
        if body.citation_photo_ref is None:
            raise _error("ticket_photo_required")
        images = self._attachments(
            record.owner, body.citation_photo_ref, body.citation_back_photo_ref
        )
        try:
            images_digest = ordered_images_digest(images)
        except TicketFactBindingError:
            raise _error("ticket_photo_not_supported") from None
        if images_digest != latest.images_sha256:
            raise _error("ticket_photo_changed")
        self._validate_literal_provenance(record.workspace_token, images, record_review.envelope)
        read = self._freshness(
            latest,
            reservation.intake_id,
            record.workspace_token,
            record.owner,
            latest.images_sha256,
            latest.fields_sha256,
        )
        if not read.current:
            raise _error("ticket_confirmation_stale")
