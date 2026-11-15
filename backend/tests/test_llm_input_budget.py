from __future__ import annotations

import base64
import random
import struct
import zlib
from io import BytesIO

import pytest
from PIL import Image

from parkingbreaker.llm import ImageAttachment, neutralise
from parkingbreaker.llm_input_budget import (
    FRAMING_TOKEN_OVERHEAD,
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_PIXELS,
    MIN_TOKENS_PER_IMAGE,
    SCHEMA_INSTRUCTION,
    UnboundableRequestError,
    ensure_request_fits_context,
    image_attachment_tokens,
    request_input_tokens,
    text_request_tokens,
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
    """A tiny PNG whose IHDR claims the given dimensions (compressible large size)."""
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


def test_text_bound_counts_framing_and_exact_parts() -> None:
    base = text_request_tokens("", "", "")
    assert base == len(("\n" + SCHEMA_INSTRUCTION).encode("utf-8")) + FRAMING_TOKEN_OVERHEAD
    assert FRAMING_TOKEN_OVERHEAD > 0


def test_text_bound_counts_system_schema_and_unicode_bytes() -> None:
    base = text_request_tokens("", "", "")
    assert text_request_tokens("s", "", "") - base == 1
    assert text_request_tokens("", "é", "") - base == 2
    assert text_request_tokens("", "🙂", "") - base == 4
    assert text_request_tokens("", "", "{}") - base == 2
    assert text_request_tokens("", "🙂", "{}") > text_request_tokens("", "a", "{}")


def test_text_bound_counts_neutralisation_expansion() -> None:
    base = text_request_tokens("", "", "")
    neutralised = neutralise("<<<")
    assert neutralised != "<<<"
    assert len(neutralised.encode("utf-8")) == 6
    assert text_request_tokens("", "<<<", "") - base == 6
    assert text_request_tokens("", neutralised, "") == text_request_tokens("", "<<<", "")


def test_text_bound_rejects_non_utf8_encodable_text() -> None:
    with pytest.raises(UnboundableRequestError, match="UTF-8"):
        text_request_tokens("\ud800", "", "")
    with pytest.raises(UnboundableRequestError, match="UTF-8"):
        text_request_tokens("", "\ud800", "")


def test_image_bound_is_max_of_encoded_bytes_pixels_and_floor() -> None:
    tiny = image(media_type="image/png", size=(8, 8))
    assert image_attachment_tokens([tiny]) == MIN_TOKENS_PER_IMAGE
    assert len(tiny.data_url()) < MIN_TOKENS_PER_IMAGE

    pixel_dominant = image(media_type="image/png", size=(512, 512))
    pixels = 512 * 512
    assert len(pixel_dominant.data_url()) < pixels
    assert image_attachment_tokens([pixel_dominant]) == max(pixels, MIN_TOKENS_PER_IMAGE)

    encoded_dominant = image(media_type="image/png", payload=noisy_png_bytes((256, 256)))
    assert len(encoded_dominant.data_url()) > 256 * 256
    assert image_attachment_tokens([encoded_dominant]) == len(encoded_dominant.data_url())


def test_image_bound_uses_jpeg_content_bytes() -> None:
    attachment = image(media_type="image/jpeg", payload=noisy_png_bytes((256, 256)))
    # The payload is PNG but declared JPEG: it must be rejected, not charged.
    with pytest.raises(UnboundableRequestError):
        image_attachment_tokens([attachment])

    jpeg = image(media_type="image/jpeg", payload=jpeg_bytes((64, 64)))
    assert image_attachment_tokens([jpeg]) == max(
        len(jpeg.data_url()), 64 * 64, MIN_TOKENS_PER_IMAGE
    )


def test_image_bound_sums_multiple_attachments() -> None:
    first = image(media_type="image/png", size=(8, 8))
    second = image(media_type="image/jpeg", size=(8, 8))
    assert image_attachment_tokens([first, second]) == 2 * MIN_TOKENS_PER_IMAGE


def test_image_bound_is_zero_for_no_attachments() -> None:
    assert image_attachment_tokens(None) == 0
    assert image_attachment_tokens([]) == 0
    assert request_input_tokens("s", "u", "{}") == text_request_tokens("s", "u", "{}")


@pytest.mark.parametrize(
    "attachment",
    [
        ImageAttachment(media_type="application/pdf", data_base64=b64(b"%PDF-1.4 ticket")),
        ImageAttachment(media_type="image/webp", data_base64=b64(b"webp bytes")),
        ImageAttachment(media_type="", data_base64=b64(b"x")),
        ImageAttachment(media_type="image/png", data_base64=b64(b"%PDF-1.4 spoofed")),
        ImageAttachment(media_type="image/jpeg", data_base64=b64(b"offline ticket")),
        ImageAttachment(media_type="image/png", data_base64=""),
        ImageAttachment(media_type="image/png", data_base64="not base64 !!"),
        ImageAttachment(media_type="image/png", data_base64="AAA"),
        ImageAttachment(media_type="image/jpeg", data_base64=b64(png_bytes())),
        ImageAttachment(media_type="image/png", data_base64=b64(jpeg_bytes())),
    ],
)
def test_unqualified_or_mismatched_attachments_refused(attachment: ImageAttachment) -> None:
    with pytest.raises(UnboundableRequestError):
        image_attachment_tokens([attachment])


def test_truncated_image_refused() -> None:
    truncated = png_bytes()[: len(png_bytes()) // 2]
    with pytest.raises(UnboundableRequestError):
        image_attachment_tokens(
            [ImageAttachment(media_type="image/png", data_base64=b64(truncated))]
        )
    signature_only = b"\x89PNG\r\n\x1a\n"
    with pytest.raises(UnboundableRequestError):
        image_attachment_tokens(
            [ImageAttachment(media_type="image/png", data_base64=b64(signature_only))]
        )


def test_multiframe_image_refused() -> None:
    with pytest.raises(UnboundableRequestError, match="single frame"):
        image_attachment_tokens(
            [ImageAttachment(media_type="image/png", data_base64=b64(animated_png_bytes()))]
        )


def test_zero_dimension_image_refused() -> None:
    with pytest.raises(UnboundableRequestError):
        image_attachment_tokens(
            [ImageAttachment(media_type="image/png", data_base64=b64(png_with_declared_size(0, 0)))]
        )


def test_compressible_large_dimensions_refused_before_expansion() -> None:
    # Tiny encoded bodies that a naive byte count would happily accept.
    for width, height in (
        (MAX_IMAGE_DIMENSION + 1, 16),
        (16, MAX_IMAGE_DIMENSION + 1),
        (10_000, 10_000),
        (20_000, 20_000),
    ):
        bomb = png_with_declared_size(width, height)
        assert len(bomb) < 1_000
        with pytest.raises(UnboundableRequestError):
            image_attachment_tokens(
                [ImageAttachment(media_type="image/png", data_base64=b64(bomb))]
            )


def test_pixel_limit_refused_within_dimension_bound() -> None:
    side = 5_000
    assert side <= MAX_IMAGE_DIMENSION
    assert side * side > MAX_IMAGE_PIXELS
    bomb = png_with_declared_size(side, side)
    assert len(bomb) < 1_000
    with pytest.raises(UnboundableRequestError, match="pixel"):
        image_attachment_tokens([ImageAttachment(media_type="image/png", data_base64=b64(bomb))])


def test_non_attachment_value_refused() -> None:
    with pytest.raises(UnboundableRequestError):
        image_attachment_tokens([object()])  # type: ignore[list-item]


def test_context_limit_accepts_exact_fit_and_refuses_overflow() -> None:
    ensure_request_fits_context(1000, 8192, 9192)
    with pytest.raises(UnboundableRequestError, match="context"):
        ensure_request_fits_context(1001, 8192, 9192)


@pytest.mark.parametrize("value", [-1, True, 1.0])
def test_context_limit_rejects_invalid_counts(value: object) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        ensure_request_fits_context(value, 0, 100)  # type: ignore[arg-type]
