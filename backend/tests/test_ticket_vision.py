from __future__ import annotations

import base64
import inspect
import struct
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import fields
from io import BytesIO
from typing import Final

import pytest
from PIL import Image

from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import (
    ImageAttachment,
    LlmBudgetExceeded,
    LlmProviderError,
    Usage,
)
from parkingbreaker.ticket_vision import (
    CRITICAL_FIELD_ORDER,
    EXTRACT_SYSTEM,
    FIELD_ORDER,
    MAX_FIELD_CHARS,
    MAX_IMAGES,
    READING_SCHEMA,
    REVIEW_BASIS,
    REVIEW_SYSTEM,
    FieldReading,
    FieldReview,
    ReadingState,
    ReviewComparison,
    TicketField,
    TicketReading,
    TicketReview,
    TicketVisionInputError,
    TicketVisionReader,
    extract_user_prompt,
    input_binding_hash,
    review_schema,
)

CONFIRMED: Final[dict[str, str]] = {
    "citation_number": "SF-998877",
    "issue_date": "10/01/2026",
}
ONE_CONFIRMED: Final[dict[str, str]] = {"citation_number": "SF-998877"}


def b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def png_bytes(size: tuple[int, int] = (8, 8), colour: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(
    size: tuple[int, int] = (8, 8), colour: tuple[int, int, int] = (40, 50, 60)
) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG")
    return buffer.getvalue()


def animated_png_bytes() -> bytes:
    frames = [Image.new("RGB", (4, 4), (index, index, index)) for index in (1, 2)]
    buffer = BytesIO()
    frames[0].save(buffer, format="PNG", save_all=True, append_images=frames[1:])
    return buffer.getvalue()


def png_with_declared_size(width: int, height: int) -> bytes:
    """A tiny PNG whose IHDR claims the given dimensions (compressible large size)."""
    data = bytearray(png_bytes((1, 1)))
    struct.pack_into(">II", data, 16, width, height)
    struct.pack_into(">I", data, 29, zlib.crc32(bytes(data[12:29])) & 0xFFFFFFFF)
    return bytes(data)


def attachment(
    media_type: str = "image/png",
    *,
    payload: bytes | None = None,
    size: tuple[int, int] = (8, 8),
    description: str | None = None,
) -> ImageAttachment:
    if payload is None:
        payload = jpeg_bytes(size) if media_type == "image/jpeg" else png_bytes(size)
    return ImageAttachment(media_type=media_type, data_base64=b64(payload), description=description)


def reading_payload() -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {}
    for field in FIELD_ORDER:
        payload[field.value] = {"state": ReadingState.NOT_SHOWN.value, "value": None}
    return payload


def with_shown(payload: dict[str, JsonValue], field: TicketField, value: str) -> None:
    payload[field.value] = {"state": ReadingState.SHOWN.value, "value": value}


def match_payload(
    texts: Mapping[str, str] | None = None,
    comparison: ReviewComparison = ReviewComparison.MATCH,
) -> dict[str, JsonValue]:
    values = CONFIRMED if texts is None else texts
    return {
        key: {"comparison": comparison.value, "visible_text": value}
        for key, value in values.items()
    }


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


class FakeVisionClient:
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


class FakeVisionErrorClient:
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


def reading_from(payload: JsonValue, usage: Usage | None = None) -> TicketReading:
    client = FakeVisionClient((payload, usage or Usage()))
    return TicketVisionReader(client).extract([attachment()], max_cost_cents=10)


def review_from(
    payload: JsonValue,
    confirmed: Mapping[str, str] | None = None,
    usage: Usage | None = None,
) -> TicketReview:
    client = FakeVisionClient((payload, usage or Usage()))
    return TicketVisionReader(client).review(
        [attachment()], ONE_CONFIRMED if confirmed is None else confirmed, max_cost_cents=10
    )


# ---------------------------------------------------------------------------
# Extraction: success, actual images, closed prompts and schema
# ---------------------------------------------------------------------------


def test_extract_returns_bounded_suggestions_and_one_call() -> None:
    payload = reading_payload()
    with_shown(payload, TicketField.CITATION_NUMBER, "SF-998877")
    with_shown(payload, TicketField.ISSUE_DATE, "10/01/2026")
    usage = Usage(input_tokens=1200, output_tokens=90, cost_cents=3)
    image = attachment()
    client = FakeVisionClient((payload, usage))

    reading = TicketVisionReader(client).extract([image], max_cost_cents=25)

    assert reading.refused is False
    assert reading.usage == usage
    assert reading.input_hash == input_binding_hash("extract", [image])
    by_field = {item.field: item for item in reading.fields}
    assert by_field[TicketField.CITATION_NUMBER] == FieldReading(
        TicketField.CITATION_NUMBER, ReadingState.SHOWN, "SF-998877"
    )
    assert by_field[TicketField.ISSUE_DATE].value == "10/01/2026"
    assert by_field[TicketField.LICENSE_PLATE] == FieldReading(
        TicketField.LICENSE_PLATE, ReadingState.NOT_SHOWN, None
    )
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call.system == EXTRACT_SYSTEM
    assert call.user == extract_user_prompt()
    assert call.schema == READING_SCHEMA
    assert call.max_cost_cents == 25
    assert call.images == (image,)
    assert call.images is not None
    assert call.images[0] is image


@pytest.mark.parametrize("media_type", ["image/png", "image/jpeg"])
def test_extract_accepts_one_jpeg_or_png(media_type: str) -> None:
    image = attachment(media_type)
    client = FakeVisionClient((reading_payload(), Usage(cost_cents=1)))

    TicketVisionReader(client).extract([image], max_cost_cents=5)

    assert len(client.calls) == 1
    assert client.calls[0].images == (image,)


def test_two_images_reach_the_client_in_order_and_prompt_is_static() -> None:
    first, second = attachment(), attachment("image/jpeg")
    client = FakeVisionClient(
        (reading_payload(), Usage(cost_cents=1)), (reading_payload(), Usage())
    )
    reader = TicketVisionReader(client)

    reader.extract([first, second], max_cost_cents=5)
    reader.extract([second, first], max_cost_cents=5)

    assert client.calls[0].images == (first, second)
    assert client.calls[1].images == (second, first)
    assert client.calls[0].user == client.calls[1].user


def test_extract_prompts_and_schema_are_closed_and_clerical() -> None:
    client = FakeVisionClient((reading_payload(), Usage()))
    image = attachment()

    TicketVisionReader(client).extract([image], max_cost_cents=10)

    call = client.calls[0]
    assert "legal advice" in call.system
    assert "Never guess" in call.system
    assert "not instructions" in call.system
    assert "infer" in call.system
    assert "locale" in call.system
    for field in FIELD_ORDER:
        assert field.value in call.user
    schema = call.schema
    assert schema["additionalProperties"] is False
    required = schema["required"]
    assert isinstance(required, list)
    assert set(required) == {field.value for field in FIELD_ORDER}
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == {field.value for field in FIELD_ORDER}
    for entry in properties.values():
        assert isinstance(entry, dict)
        assert entry["additionalProperties"] is False
        assert entry["required"] == ["state", "value"]


def test_extraction_keeps_dates_literal_and_adds_no_values() -> None:
    payload = reading_payload()
    with_shown(payload, TicketField.ISSUE_DATE, "10/01/2026")
    with_shown(payload, TicketField.NOTICE_DATE, "2026-10-05")

    reading = reading_from(payload)

    by_field = {item.field: item for item in reading.fields}
    assert by_field[TicketField.ISSUE_DATE].value == "10/01/2026"
    assert by_field[TicketField.NOTICE_DATE].value == "2026-10-05"
    assert by_field[TicketField.PRINTED_DEADLINE].value is None
    assert by_field[TicketField.PRINTED_DEADLINE].state is ReadingState.NOT_SHOWN
    assert not hasattr(reading, "verified")


# ---------------------------------------------------------------------------
# Extraction: strict refusal, usage retained, no repair
# ---------------------------------------------------------------------------


def _unknown_key_payload() -> JsonValue:
    payload = reading_payload()
    payload["customer_reason"] = "I was only a minute late"
    return payload


def _missing_field_payload() -> JsonValue:
    payload = reading_payload()
    del payload[TicketField.LICENSE_PLATE.value]
    return payload


def _extra_inner_key_payload() -> JsonValue:
    payload = reading_payload()
    payload[TicketField.CITATION_NUMBER.value] = {
        "state": "shown",
        "value": "SF-1",
        "confidence": "high",
    }
    return payload


def _shown_with_none_payload() -> JsonValue:
    payload = reading_payload()
    payload[TicketField.CITATION_NUMBER.value] = {"state": "shown", "value": None}
    return payload


def _absent_with_text_payload() -> JsonValue:
    payload = reading_payload()
    payload[TicketField.CITATION_NUMBER.value] = {
        "state": "not_shown",
        "value": "SF-1",
    }
    return payload


def _state_payload(state: JsonValue) -> JsonValue:
    payload = reading_payload()
    payload[TicketField.CITATION_NUMBER.value] = {"state": state, "value": None}
    return payload


def _shown_value_payload(value: JsonValue) -> JsonValue:
    payload = reading_payload()
    payload[TicketField.CITATION_NUMBER.value] = {"state": "shown", "value": value}
    return payload


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not an object",
        [],
        {},
        {"citation_number": None},
        _unknown_key_payload(),
        _missing_field_payload(),
        _extra_inner_key_payload(),
        _shown_with_none_payload(),
        _absent_with_text_payload(),
        _state_payload(True),
        _state_payload(1),
        _state_payload("maybe"),
        _shown_value_payload(123),
        _shown_value_payload(True),
        _shown_value_payload("   "),
        _shown_value_payload("X" * (MAX_FIELD_CHARS + 1)),
        _shown_value_payload("two\nlines"),
    ],
)
def test_extract_refuses_bad_schema_and_retains_paid_usage(payload: JsonValue) -> None:
    usage = Usage(input_tokens=50, output_tokens=5, cost_cents=9)
    client = FakeVisionClient((payload, usage))

    reading = TicketVisionReader(client).extract([attachment()], max_cost_cents=20)

    assert reading.refused is True
    assert reading.fields == ()
    assert reading.usage == usage
    assert reading.usage.cost_cents == 9
    assert len(client.calls) == 1


def test_extract_never_repairs_or_retries_after_refusal() -> None:
    client = FakeVisionClient(("malformed", Usage(cost_cents=4)), (reading_payload(), Usage()))

    reading = TicketVisionReader(client).extract([attachment()], max_cost_cents=10)

    assert reading.refused is True
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "error",
    [LlmBudgetExceeded("no budget"), LlmProviderError("provider down", retryable=True)],
)
def test_extract_preserves_client_errors_without_retry(error: Exception) -> None:
    client = FakeVisionErrorClient(error)

    with pytest.raises(type(error)):
        TicketVisionReader(client).extract([attachment()], max_cost_cents=10)

    assert client.calls == 1


# ---------------------------------------------------------------------------
# Preflight: ceilings and images never reach the client
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ceiling", [0, -1, True, 1.0, None, "5"])
def test_extract_rejects_bad_ceiling_without_a_call(ceiling: object) -> None:
    client = FakeVisionClient((reading_payload(), Usage()))

    with pytest.raises(TicketVisionInputError, match="positive integer"):
        TicketVisionReader(client).extract(
            [attachment()],
            max_cost_cents=ceiling,  # type: ignore[arg-type]
        )

    assert client.calls == []


@pytest.mark.parametrize("ceiling", [0, -1, True, 1.0, None])
def test_review_rejects_bad_ceiling_without_a_call(ceiling: object) -> None:
    client = FakeVisionClient((match_payload(), Usage()))

    with pytest.raises(TicketVisionInputError, match="positive integer"):
        TicketVisionReader(client).review(
            [attachment()],
            ONE_CONFIRMED,
            max_cost_cents=ceiling,  # type: ignore[arg-type]
        )

    assert client.calls == []


@pytest.mark.parametrize("count", [0, MAX_IMAGES + 1])
def test_extract_rejects_missing_or_too_many_images(count: int) -> None:
    client = FakeVisionClient((reading_payload(), Usage()))
    images = [attachment() for _ in range(count)]

    with pytest.raises(TicketVisionInputError, match="ticket images"):
        TicketVisionReader(client).extract(images, max_cost_cents=10)

    assert client.calls == []


@pytest.mark.parametrize("count", [0, MAX_IMAGES + 1])
def test_review_rejects_missing_or_too_many_images(count: int) -> None:
    client = FakeVisionClient((match_payload(), Usage()))
    images = [attachment() for _ in range(count)]

    with pytest.raises(TicketVisionInputError, match="ticket images"):
        TicketVisionReader(client).review(images, ONE_CONFIRMED, max_cost_cents=10)

    assert client.calls == []


def _truncated_png() -> bytes:
    whole = png_bytes()
    return whole[: len(whole) // 2]


@pytest.mark.parametrize(
    "image",
    [
        ImageAttachment("application/pdf", b64(b"%PDF-1.4 ticket")),
        ImageAttachment("image/webp", b64(png_bytes())),
        ImageAttachment("image/png", b64(jpeg_bytes())),
        ImageAttachment("image/jpeg", b64(png_bytes())),
        ImageAttachment("image/png", ""),
        ImageAttachment("image/png", "not base64 !!"),
        ImageAttachment("image/png", b64(_truncated_png())),
        ImageAttachment("image/png", b64(animated_png_bytes())),
        ImageAttachment("image/png", b64(png_with_declared_size(20_000, 20_000))),
        ImageAttachment("image/png", b64(png_with_declared_size(5_000, 5_000))),
    ],
)
def test_extract_refuses_malformed_or_unsupported_images_without_a_call(
    image: ImageAttachment,
) -> None:
    client = FakeVisionClient((reading_payload(), Usage()))

    with pytest.raises(TicketVisionInputError, match="JPEG/PNG"):
        TicketVisionReader(client).extract([image], max_cost_cents=10)

    assert client.calls == []


@pytest.mark.parametrize(
    "image",
    [
        ImageAttachment("application/pdf", b64(b"%PDF-1.4 ticket")),
        ImageAttachment("image/webp", b64(png_bytes())),
        ImageAttachment("image/png", b64(jpeg_bytes())),
        ImageAttachment("image/png", b64(animated_png_bytes())),
        ImageAttachment("image/png", ""),
    ],
)
def test_review_refuses_malformed_or_unsupported_images_without_a_call(
    image: ImageAttachment,
) -> None:
    client = FakeVisionClient((match_payload(), Usage()))

    with pytest.raises(TicketVisionInputError, match="JPEG/PNG"):
        TicketVisionReader(client).review([image], ONE_CONFIRMED, max_cost_cents=10)

    assert client.calls == []


def test_non_sequence_and_non_attachment_images_are_refused() -> None:
    client = FakeVisionClient((reading_payload(), Usage()))
    reader = TicketVisionReader(client)

    with pytest.raises(TicketVisionInputError, match="list or tuple"):
        reader.extract("not images", max_cost_cents=10)  # type: ignore[arg-type]
    with pytest.raises(TicketVisionInputError, match="JPEG/PNG"):
        reader.extract([object()], max_cost_cents=10)  # type: ignore[list-item]

    assert client.calls == []


# ---------------------------------------------------------------------------
# Review: independent comparison, literal text, false matches become conflicts
# ---------------------------------------------------------------------------


def test_review_returns_per_field_comparison_and_literal_text() -> None:
    usage = Usage(input_tokens=800, output_tokens=40, cost_cents=2)
    image = attachment()
    client = FakeVisionClient((match_payload(), usage))

    review = TicketVisionReader(client).review([image], CONFIRMED, max_cost_cents=15)

    assert review.refused is False
    assert review.usage == usage
    assert review.input_hash == input_binding_hash("review", [image], CONFIRMED)
    by_field = {item.field: item for item in review.fields}
    assert by_field[TicketField.CITATION_NUMBER].comparison is ReviewComparison.MATCH
    assert by_field[TicketField.CITATION_NUMBER].visible_text == "SF-998877"
    assert by_field[TicketField.ISSUE_DATE].visible_text == "10/01/2026"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call.system == REVIEW_SYSTEM
    assert call.max_cost_cents == 15
    assert call.images == (image,)
    assert call.schema["additionalProperties"] is False
    required = call.schema["required"]
    assert isinstance(required, list)
    assert required == ["citation_number", "issue_date"]


def test_review_schema_is_closed_and_ordered_for_supplied_fields() -> None:
    schema = review_schema(["issue_date", "citation_number"])

    assert schema["additionalProperties"] is False
    required = schema["required"]
    assert isinstance(required, list)
    assert required == [
        field.value
        for field in CRITICAL_FIELD_ORDER
        if field.value in {"citation_number", "issue_date"}
    ]
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == {"citation_number", "issue_date"}


def test_review_matches_after_whitespace_normalisation() -> None:
    confirmed = {"citation_number": "SF  998877"}
    payload: JsonValue = {
        "citation_number": {"comparison": "match", "visible_text": "SF 998877"},
    }

    review = review_from(payload, confirmed)

    assert review.fields[0].comparison is ReviewComparison.MATCH
    assert review.fields[0].visible_text == "SF 998877"


def test_false_model_match_becomes_conflict_and_is_not_adopted() -> None:
    payload: JsonValue = {
        "citation_number": {"comparison": "match", "visible_text": "SF-000000"},
    }
    client = FakeVisionClient((payload, Usage(cost_cents=2)))

    review = TicketVisionReader(client).review([attachment()], ONE_CONFIRMED, max_cost_cents=10)

    item = review.fields[0]
    assert item.comparison is ReviewComparison.CONFLICT
    assert item.visible_text == "SF-000000"


def test_model_conflict_is_never_upgraded_to_match() -> None:
    payload: JsonValue = {
        "citation_number": {"comparison": "conflict", "visible_text": "SF-998877"},
    }

    review = review_from(payload)

    assert review.fields[0].comparison is ReviewComparison.CONFLICT


def test_review_not_shown_and_unreadable_carry_no_text() -> None:
    payload: dict[str, JsonValue] = {
        "citation_number": {"comparison": "not_shown", "visible_text": None},
        "issue_date": {"comparison": "unreadable", "visible_text": None},
    }

    review = review_from(payload, CONFIRMED)

    by_field = {item.field: item for item in review.fields}
    assert by_field[TicketField.CITATION_NUMBER].comparison is ReviewComparison.NOT_SHOWN
    assert by_field[TicketField.ISSUE_DATE].comparison is ReviewComparison.UNREADABLE
    assert by_field[TicketField.CITATION_NUMBER].visible_text is None


def test_review_basis_is_not_external_verification() -> None:
    review = review_from(match_payload())

    assert review.basis == REVIEW_BASIS
    assert "not external verification" in review.basis
    assert not hasattr(review, "verified")
    system = REVIEW_SYSTEM
    assert "not external verification" in system
    assert "Never decide merits" in system


# ---------------------------------------------------------------------------
# Review: strict refusal, usage retained, no repair
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not an object",
        [],
        {},
        {"license_plate": {"comparison": "match", "visible_text": "ABC123"}},
        {
            "citation_number": {
                "comparison": "match",
                "visible_text": "SF-998877",
                "rationale": "looks the same",
            }
        },
        {"citation_number": {"comparison": True, "visible_text": "SF-998877"}},
        {"citation_number": {"comparison": 1, "visible_text": "SF-998877"}},
        {"citation_number": {"comparison": "same", "visible_text": "SF-998877"}},
        {"citation_number": {"comparison": "match", "visible_text": None}},
        {"citation_number": {"comparison": "conflict", "visible_text": None}},
        {"citation_number": {"comparison": "not_shown", "visible_text": "SF-998877"}},
        {"citation_number": {"comparison": "unreadable", "visible_text": "SF-998877"}},
        {"citation_number": {"comparison": "match", "visible_text": "   "}},
        {"citation_number": {"comparison": "match", "visible_text": "X" * (MAX_FIELD_CHARS + 1)}},
        {"citation_number": {"comparison": "match", "visible_text": "two\nlines"}},
    ],
)
def test_review_refuses_bad_schema_and_retains_paid_usage(payload: JsonValue) -> None:
    usage = Usage(input_tokens=30, output_tokens=3, cost_cents=11)
    client = FakeVisionClient((payload, usage))

    review = TicketVisionReader(client).review([attachment()], ONE_CONFIRMED, max_cost_cents=40)

    assert review.refused is True
    assert review.fields == ()
    assert review.usage == usage
    assert review.usage.cost_cents == 11
    assert len(client.calls) == 1


def test_review_never_repairs_or_retries_after_refusal() -> None:
    client = FakeVisionClient(("malformed", Usage(cost_cents=4)), (match_payload(), Usage()))

    review = TicketVisionReader(client).review([attachment()], ONE_CONFIRMED, max_cost_cents=10)

    assert review.refused is True
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "error",
    [LlmBudgetExceeded("no budget"), LlmProviderError("provider down", retryable=True)],
)
def test_review_preserves_client_errors_without_retry(error: Exception) -> None:
    client = FakeVisionErrorClient(error)

    with pytest.raises(type(error)):
        TicketVisionReader(client).review([attachment()], ONE_CONFIRMED, max_cost_cents=10)

    assert client.calls == 1


# ---------------------------------------------------------------------------
# Review: separate fresh call, no reason, no mutation
# ---------------------------------------------------------------------------


def test_review_is_a_separate_call_that_excludes_extraction_output() -> None:
    extraction = reading_payload()
    with_shown(extraction, TicketField.CITATION_NUMBER, "EXTRACT-ONLY-CANARY")
    review_reply: JsonValue = {
        "citation_number": {"comparison": "conflict", "visible_text": "SF-000001"},
    }
    client = FakeVisionClient(
        (extraction, Usage(cost_cents=2)),
        (review_reply, Usage(cost_cents=3)),
    )
    reader = TicketVisionReader(client)
    image = attachment()

    reader.extract([image], max_cost_cents=10)
    review = reader.review([image], ONE_CONFIRMED, max_cost_cents=10)

    assert len(client.calls) == 2
    extract_call, review_call = client.calls
    assert "EXTRACT-ONLY-CANARY" not in review_call.system
    assert "EXTRACT-ONLY-CANARY" not in review_call.user
    assert "EXTRACT-ONLY-CANARY" not in str(review_call.schema)
    assert "SF-998877" in review_call.user
    assert review_call.user != extract_call.user
    assert review.usage == Usage(cost_cents=3)
    assert review.fields[0].comparison is ReviewComparison.CONFLICT


def test_review_prompt_depends_only_on_confirmed_fields() -> None:
    client = FakeVisionClient((match_payload(), Usage()), (match_payload(), Usage()))
    reader = TicketVisionReader(client)
    first, second = attachment(), attachment("image/jpeg")

    reader.review([first], CONFIRMED, max_cost_cents=10)
    reader.review([second], CONFIRMED, max_cost_cents=10)

    assert client.calls[0].user == client.calls[1].user
    assert "SF-998877" in client.calls[0].user
    assert "10/01/2026" in client.calls[0].user


def test_review_signature_has_no_customer_reason_parameter() -> None:
    parameters = inspect.signature(TicketVisionReader.review).parameters
    assert "reason" not in parameters
    assert "customer_reason" not in parameters


def test_review_rejects_reason_keyword_without_a_call() -> None:
    client = FakeVisionClient((match_payload(), Usage()))

    with pytest.raises(TypeError):
        TicketVisionReader(client).review(
            [attachment()],
            ONE_CONFIRMED,
            max_cost_cents=10,
            reason="I was only a minute late",  # type: ignore[call-arg]
        )

    assert client.calls == []


def test_review_rejects_reason_as_a_confirmed_field_without_a_call() -> None:
    client = FakeVisionClient((match_payload(), Usage()))

    with pytest.raises(TicketVisionInputError, match="closed critical fields"):
        TicketVisionReader(client).review(
            [attachment()],
            {"citation_number": "SF-998877", "reason": "I was only a minute late"},
            max_cost_cents=10,
        )

    assert client.calls == []


@pytest.mark.parametrize(
    "confirmed",
    [
        {},
        {"customer_reason": "I was only a minute late"},
        {"issuer_hint": "SFMTA"},
        {"citation_number": True},
        {"citation_number": 123},
        {"citation_number": ""},
        {"citation_number": "X" * (MAX_FIELD_CHARS + 1)},
    ],
)
def test_review_rejects_non_closed_or_invalid_confirmed_fields_without_a_call(
    confirmed: Mapping[str, str],
) -> None:
    client = FakeVisionClient((match_payload(), Usage()))

    with pytest.raises(TicketVisionInputError):
        TicketVisionReader(client).review([attachment()], confirmed, max_cost_cents=10)

    assert client.calls == []


def test_review_accepts_every_closed_field() -> None:
    confirmed = {field.value: f"VALUE-{index}" for index, field in enumerate(FIELD_ORDER)}
    payload: dict[str, JsonValue] = {
        key: {"comparison": "match", "visible_text": value} for key, value in confirmed.items()
    }
    client = FakeVisionClient((payload, Usage()))

    review = TicketVisionReader(client).review([attachment()], confirmed, max_cost_cents=10)

    assert [item.field.value for item in review.fields] == [field.value for field in FIELD_ORDER]
    assert all(item.comparison is ReviewComparison.MATCH for item in review.fields)


def test_review_does_not_mutate_or_adopt_customer_facts() -> None:
    confirmed = dict(CONFIRMED)
    original = dict(confirmed)
    client = FakeVisionClient((match_payload(), Usage()))

    review = TicketVisionReader(client).review([attachment()], confirmed, max_cost_cents=10)

    assert confirmed == original
    assert {item.field for item in review.fields} == {TicketField(key) for key in confirmed}
    assert [field.name for field in fields(FieldReview)] == [
        "field",
        "comparison",
        "visible_text",
    ]
    assert [field.name for field in fields(TicketReview)] == [
        "fields",
        "usage",
        "input_hash",
        "refused",
    ]
    assert not hasattr(review, "apply")


# ---------------------------------------------------------------------------
# Binding hash
# ---------------------------------------------------------------------------


def test_binding_hash_is_stable_and_changes_with_image_and_fields() -> None:
    first = attachment(payload=png_bytes((8, 8), (1, 2, 3)))
    second = attachment(payload=png_bytes((8, 8), (4, 5, 6)))

    assert input_binding_hash("extract", [first]) == input_binding_hash("extract", [first])
    assert input_binding_hash("extract", [first]) != input_binding_hash("extract", [second])
    assert input_binding_hash("extract", [first]) != input_binding_hash(
        "extract", [attachment("image/jpeg")]
    )
    assert input_binding_hash("extract", [first]) == input_binding_hash(
        "extract", [attachment(payload=png_bytes((8, 8), (1, 2, 3)), description="front")]
    )

    confirmed_a = {"citation_number": "SF-1"}
    confirmed_b = {"citation_number": "SF-2"}
    assert input_binding_hash("review", [first], confirmed_a) == input_binding_hash(
        "review", [first], confirmed_a
    )
    assert input_binding_hash("review", [first], confirmed_a) != input_binding_hash(
        "review", [first], confirmed_b
    )
    assert input_binding_hash("review", [first], confirmed_a) != input_binding_hash(
        "review", [first], {"license_plate": "SF-1"}
    )
    assert input_binding_hash("extract", [first]) != input_binding_hash(
        "review", [first], confirmed_a
    )


def test_binding_hash_ignores_mapping_insertion_order() -> None:
    image = attachment()
    forward = {"citation_number": "SF-1", "issue_date": "10/01/2026"}
    reverse = {"issue_date": "10/01/2026", "citation_number": "SF-1"}

    assert input_binding_hash("review", [image], forward) == input_binding_hash(
        "review", [image], reverse
    )


def test_refused_output_still_binds_the_exact_input() -> None:
    image = attachment()
    client = FakeVisionClient(("malformed", Usage(cost_cents=1)))

    reading = TicketVisionReader(client).extract([image], max_cost_cents=5)

    assert reading.refused is True
    assert reading.input_hash == input_binding_hash("extract", [image])
