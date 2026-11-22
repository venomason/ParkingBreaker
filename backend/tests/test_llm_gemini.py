from __future__ import annotations

import base64
import random
import struct
import zlib
from collections.abc import Mapping
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from typing import cast

import httpx2 as httpx
import openai
import pytest
from PIL import Image

from parkingbreaker import llm_gemini
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import (
    ImageAttachment,
    LlmBudgetExceeded,
    LlmProviderError,
    Usage,
    schema_json,
)
from parkingbreaker.llm_gemini import GeminiLlmClient
from parkingbreaker.llm_input_budget import (
    MIN_TOKENS_PER_IMAGE,
    UnboundableRequestError,
    text_request_tokens,
)
from parkingbreaker.llm_models import (
    GEMINI_CONTEXT_TOKENS,
    LlmModelConfig,
    ModelPricing,
    gemini_3_5_flash_lite_config,
    gemini_3_8_flash_config,
    request_output_tokens,
    usage_cost_cents,
    worst_case_cents,
)

PRICING = ModelPricing(Decimal("75"), Decimal("18.75"), Decimal("375"))
SYSTEM = "system ✓"
USER = "prompt 🙂"
SCHEMA: dict[str, JsonValue] = {"type": "object"}


def config(model_id: str = "gemini-3.8-flash") -> LlmModelConfig:
    return (
        gemini_3_8_flash_config(pricing=PRICING)
        if model_id == "gemini-3.8-flash"
        else gemini_3_5_flash_lite_config()
    )


def b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def png_bytes(size: tuple[int, int] = (8, 8), colour: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(size: tuple[int, int] = (8, 8)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, (40, 50, 60)).save(buffer, format="JPEG")
    return buffer.getvalue()


def noisy_png_bytes(size: tuple[int, int]) -> bytes:
    width, height = size
    payload = random.Random(20261108).randbytes(width * height * 3)
    buffer = BytesIO()
    Image.frombytes("RGB", size, payload).save(buffer, format="PNG")
    return buffer.getvalue()


def animated_png_bytes() -> bytes:
    frames = [Image.new("RGB", (4, 4), (index, index, index)) for index in (1, 2)]
    buffer = BytesIO()
    frames[0].save(buffer, format="PNG", save_all=True, append_images=frames[1:])
    return buffer.getvalue()


def png_with_declared_size(width: int, height: int) -> bytes:
    data = bytearray(png_bytes((1, 1)))
    struct.pack_into(">II", data, 16, width, height)
    struct.pack_into(">I", data, 29, zlib.crc32(bytes(data[12:29])) & 0xFFFFFFFF)
    return bytes(data)


def image(
    media_type: str = "image/jpeg",
    *,
    payload: bytes | None = None,
    size: tuple[int, int] = (8, 8),
) -> ImageAttachment:
    if payload is None:
        payload = jpeg_bytes(size) if media_type == "image/jpeg" else png_bytes(size)
    return ImageAttachment(media_type=media_type, data_base64=b64(payload))


def input_bound(
    system: str = SYSTEM,
    user: str = USER,
    schema: Mapping[str, JsonValue] = SCHEMA,
    images: list[ImageAttachment] | None = None,
) -> int:
    schema_text = schema_json(schema)
    return text_request_tokens(system, user, schema_text) + len(images or []) * MIN_TOKENS_PER_IMAGE


def output_for(ceiling: int | None) -> int:
    return request_output_tokens(config(), input_bound(), ceiling)


def usage_obj(
    prompt: object = 1000,
    completion: object = 100,
    *,
    cached: object = 0,
    total: object = None,
    details: bool = True,
) -> object:
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=(
            (prompt + completion)
            if total is None and isinstance(prompt, int) and isinstance(completion, int)
            else total
        ),
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached) if details else None,
    )


def response(
    content: object = '{"citation_number": "12345"}',
    finish: str = "stop",
    *,
    usage: object | None = None,
    role: str = "assistant",
    tool_calls: object = None,
    refusal: object = None,
    choices: object = None,
) -> object:
    if choices is not None:
        return SimpleNamespace(usage=usage if usage is not None else usage_obj(), choices=choices)
    message = SimpleNamespace(role=role, content=content, tool_calls=tool_calls, refusal=refusal)
    return SimpleNamespace(
        usage=usage if usage is not None else usage_obj(),
        choices=[SimpleNamespace(message=message, finish_reason=finish)],
    )


class Transport:
    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self.result = response() if result is None else result
        self.error = error
        self.payloads: list[dict[str, object]] = []

    def create(self, payload: object) -> object:
        assert isinstance(payload, dict)
        self.payloads.append(cast(dict[str, object], payload))
        if len(self.payloads) > 1:
            raise AssertionError("adapter made more than one transport attempt")
        if self.error is not None:
            raise self.error
        return self.result


def api_error(status: int) -> openai.APIStatusError:
    request = httpx.Request(
        "POST", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    return openai.APIStatusError(
        "provider body", response=httpx.Response(status, request=request), body=None
    )


def test_payload_text_only() -> None:
    transport = Transport()
    client = GeminiLlmClient(config(), "offline-key", transport=transport)
    result, usage = client.complete_json("system ✓", "prompt 🙂", {"type": "object"})
    assert result == {"citation_number": "12345"}
    assert usage == Usage(1000, 100, usage_cost_cents(PRICING, 1000, 0, 100))
    (payload,) = transport.payloads
    assert payload["model"] == "gemini-3.8-flash"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["stream"] is False
    assert payload["max_completion_tokens"] == 8192
    messages = cast(list[dict[str, object]], payload["messages"])
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "prompt 🙂"


def test_tiny_text_succeeds_at_25_cent_ceiling() -> None:
    transport = Transport()
    client = GeminiLlmClient(config(), "offline-key", transport=transport)
    result, usage = client.complete_json(SYSTEM, USER, SCHEMA, max_cost_cents=25)
    assert result == {"citation_number": "12345"}
    assert usage.input_tokens == 1000
    (payload,) = transport.payloads
    sent = cast(int, payload["max_completion_tokens"])
    assert sent == output_for(25) == 8192
    assert sent >= 1024
    assert worst_case_cents(config(), input_bound(), sent) <= 25


def test_tighter_ceiling_reduces_output_and_stays_within_ceiling() -> None:
    loose = Transport()
    GeminiLlmClient(config(), "offline-key", transport=loose).complete_json(
        SYSTEM, USER, SCHEMA, max_cost_cents=25
    )
    tight = Transport()
    GeminiLlmClient(config(), "offline-key", transport=tight).complete_json(
        SYSTEM, USER, SCHEMA, max_cost_cents=1
    )
    loose_output = cast(int, loose.payloads[0]["max_completion_tokens"])
    tight_output = cast(int, tight.payloads[0]["max_completion_tokens"])
    assert tight_output < loose_output
    assert tight_output >= 1024
    assert tight_output == output_for(1)
    assert worst_case_cents(config(), input_bound(), tight_output) <= 1


def test_payload_multimodal_with_image_attachments() -> None:
    transport = Transport()
    client = GeminiLlmClient(config(), "offline-key", transport=transport)
    img = image()
    result, usage = client.complete_json(
        "extract fields", "ticket scan", {"type": "object"}, images=[img]
    )
    assert result == {"citation_number": "12345"}
    assert usage.cost_cents > 0
    (payload,) = transport.payloads
    messages = cast(list[dict[str, object]], payload["messages"])
    user_content = cast(list[dict[str, object]], messages[1]["content"])
    assert user_content[0] == {"type": "text", "text": "ticket scan"}
    assert user_content[1] == {
        "type": "image_url",
        "image_url": {"url": img.data_url()},
    }


@pytest.mark.parametrize("model", ["gemini-3.8-flash", "gemini-3.5-flash-lite"])
def test_original_ticket_image_fits_existing_ceiling(model: str) -> None:
    img = image(media_type="image/png", size=(1300, 1100))
    transport = Transport()
    cfg = config(model)
    GeminiLlmClient(cfg, "offline-key", transport=transport).complete_json(
        SYSTEM, USER, SCHEMA, images=[img], max_cost_cents=25
    )
    (payload,) = transport.payloads
    sent = cast(int, payload["max_completion_tokens"])
    assert sent >= 1024
    assert worst_case_cents(cfg, input_bound(images=[img]), sent) <= 25
    messages = cast(list[dict[str, object]], payload["messages"])
    content = cast(list[dict[str, object]], messages[1]["content"])
    assert content[1]["image_url"] == {"url": img.data_url()}


def test_each_image_is_reserved_before_transport() -> None:
    img = image()
    funded = Transport()
    GeminiLlmClient(config(), "offline-key", transport=funded).complete_json(
        SYSTEM, USER, SCHEMA, images=[img, img], max_cost_cents=2
    )
    sent = cast(int, funded.payloads[0]["max_completion_tokens"])
    assert sent == request_output_tokens(config(), input_bound(images=[img, img]), 2)
    assert sent < request_output_tokens(config(), input_bound(images=[img]), 2)
    transport = Transport()
    client = GeminiLlmClient(config(), "offline-key", transport=transport)
    with pytest.raises(LlmBudgetExceeded):
        client.complete_json(SYSTEM, USER, SCHEMA, images=[img, img], max_cost_cents=1)
    assert transport.payloads == []


def test_image_batch_that_exceeds_context_is_refused_before_transport() -> None:
    images = [image()] * (GEMINI_CONTEXT_TOKENS // MIN_TOKENS_PER_IMAGE)
    transport = Transport()
    client = GeminiLlmClient(config(), "offline-key", transport=transport)
    with pytest.raises(UnboundableRequestError, match="context"):
        client.complete_json(SYSTEM, USER, SCHEMA, images=images)
    assert transport.payloads == []


@pytest.mark.parametrize("include_json_overhead", [False, True])
def test_inline_size_limit_covers_wire_bytes_before_transport(
    monkeypatch: pytest.MonkeyPatch, *, include_json_overhead: bool
) -> None:
    img = image(media_type="image/png", payload=noisy_png_bytes((128, 128)))
    limit = len(img.data_base64) + (1 if include_json_overhead else -1)
    monkeypatch.setattr(llm_gemini, "MAX_INLINE_REQUEST_BYTES", limit)
    transport = Transport()
    client = GeminiLlmClient(config(), "offline-key", transport=transport)
    with pytest.raises(UnboundableRequestError, match="size limit"):
        client.complete_json(SYSTEM, USER, SCHEMA, images=[img])
    assert transport.payloads == []


def test_compressible_large_dimension_image_refused_before_transport() -> None:
    bomb = png_with_declared_size(20_000, 20_000)
    assert len(bomb) < 1_000
    transport = Transport()
    client = GeminiLlmClient(config(), "offline-key", transport=transport)
    with pytest.raises(UnboundableRequestError):
        client.complete_json(
            "s",
            "u",
            {},
            images=[ImageAttachment(media_type="image/png", data_base64=b64(bomb))],
        )
    assert transport.payloads == []


def test_multiframe_image_refused_before_transport() -> None:
    transport = Transport()
    client = GeminiLlmClient(config(), "offline-key", transport=transport)
    with pytest.raises(UnboundableRequestError, match="single frame"):
        client.complete_json(
            "s",
            "u",
            {},
            images=[ImageAttachment(media_type="image/png", data_base64=b64(animated_png_bytes()))],
        )
    assert transport.payloads == []


@pytest.mark.parametrize(
    "attachment",
    [
        ImageAttachment(media_type="application/pdf", data_base64=b64(b"pdf bytes")),
        ImageAttachment(media_type="image/webp", data_base64=b64(b"webp bytes")),
        ImageAttachment(media_type="", data_base64=b64(b"x")),
        ImageAttachment(media_type="image/png", data_base64=b64(b"%PDF-1.4 spoofed")),
        ImageAttachment(media_type="image/jpeg", data_base64=b64(b"offline ticket")),
        ImageAttachment(media_type="image/png", data_base64=""),
        ImageAttachment(media_type="image/png", data_base64="not base64 !!"),
        ImageAttachment(media_type="image/jpeg", data_base64=b64(png_bytes())),
        ImageAttachment(media_type="image/png", data_base64=b64(jpeg_bytes())),
    ],
)
def test_unqualified_attachments_refused_before_transport(attachment: ImageAttachment) -> None:
    transport = Transport()
    client = GeminiLlmClient(config(), "offline-key", transport=transport)
    with pytest.raises(UnboundableRequestError):
        client.complete_json("s", "u", {}, images=[attachment])
    assert transport.payloads == []


def test_gemini_cached_tokens() -> None:
    transport = Transport(response(usage=usage_obj(1_000_000, 100_000, cached=400_000)))
    _, usage = GeminiLlmClient(config(), "offline-key", transport=transport).complete_json(
        "s", "u", {}
    )
    assert usage.input_tokens == 1_000_000
    assert usage.output_tokens == 100_000
    expected = usage_cost_cents(PRICING, 600_000, 400_000, 100_000)
    assert usage.cost_cents == expected


def test_provider_error_mapping() -> None:
    transport = Transport(error=api_error(503))
    client = GeminiLlmClient(config(), "offline-key", transport=transport)
    with pytest.raises(LlmProviderError) as exc_info:
        client.complete_json("s", "u", {})
    assert exc_info.value.retryable is True


def test_zero_ceiling_refused_before_transport() -> None:
    transport = Transport()
    client = GeminiLlmClient(config(), "offline-key", transport=transport)
    with pytest.raises(LlmBudgetExceeded):
        client.complete_json("s", "u", {}, max_cost_cents=0)
    assert transport.payloads == []


def test_negative_ceiling_refused_before_transport() -> None:
    transport = Transport()
    client = GeminiLlmClient(config(), "offline-key", transport=transport)
    with pytest.raises(ValueError, match="max_cost_cents"):
        client.complete_json("s", "u", {}, max_cost_cents=-1)
    assert transport.payloads == []


def test_genuinely_unaffordable_request_refused_before_transport() -> None:
    transport = Transport()
    client = GeminiLlmClient(config(), "offline-key", transport=transport)
    with pytest.raises(LlmBudgetExceeded):
        client.complete_json("s", "x" * 20_000, {}, max_cost_cents=1)
    assert transport.payloads == []


def test_oversized_text_refused_before_transport() -> None:
    transport = Transport()
    client = GeminiLlmClient(config(), "offline-key", transport=transport)
    with pytest.raises(UnboundableRequestError, match="context"):
        client.complete_json("s", "x" * 2_000_000, {})
    assert transport.payloads == []
    assert input_bound("s", "x" * 2_000_000, {}) > GEMINI_CONTEXT_TOKENS
