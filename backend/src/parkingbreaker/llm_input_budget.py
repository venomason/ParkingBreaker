"""Conservative offline allocation bounds for one chat request's input tokens.

Why this exists
    A capped adapter must *prove* that a single request cannot exceed its cent
    ceiling.  Charging the whole model context window (1,048,576 tokens) as every
    request's input made all capped requests unaffordable, including tiny
    extraction prompts.  This module bounds only the tokens attributable to the
    request being sent, while staying deliberately conservative.

Qualification status (read this before claiming anything)
    Everything in this module is an offline, conservative *allocation* bound used
    for spend-ceiling arithmetic.  It is not a measured token count, not the
    provider's billing unit, and not a provider-proved hard guarantee.  Provider
    qualification -- tokenizer behaviour, image-token accounting, framing,
    truncation and request limits -- is still required before any live beta
    relies on these figures.

Assumptions, not established facts
    * The text bound treats one UTF-8 byte as at most one token.  That holds by
      construction for byte-level BPE tokenizers, but the Gemini tokenizer's
      exact behaviour is *not* established here and is not claimed.  The
      assumption is deliberately one-sided (it over-reserves) and exists only to
      stop a ceiling being exceeded; if a provider ever emitted more tokens than
      bytes, this bound would be wrong.
    * ``FRAMING_TOKEN_OVERHEAD`` is a fixed allowance for provider framing that
      is not attributable to the caller's strings: role markers, message
      separators, the ``json_object`` response-format envelope and the schema
      instruction appended to the system message.  It is an allowance, not a
      measured value.
    * Media is *structurally* checked, not provider-qualified.  JPEG and PNG
      payloads must decode with Pillow to the declared format, contain exactly
      one frame and have positive dimensions inside fixed limits.  This rejects
      obvious spoofing, empty, truncated and decompression-bomb inputs before
      transport; it does not prove the provider accepts the file, and it must not
      be described as "qualified" bounded media.
    * A per-image reservation is the maximum of the encoded data-URL byte count,
      the decoded pixel count, and ``MIN_TOKENS_PER_IMAGE``.  It is intentionally
      far above any documented image-token accounting, so some batches that a
      provider would accept are refused.
    * Only UTF-8-encodable text and structurally valid JPEG/PNG data are bounded.
      PDF, webp, formatless, malformed-base64 or otherwise unbounded media have
      no defensible token bound and are refused before transport.  Text that
      cannot be UTF-8 encoded (lone surrogates) is refused the same way.
    * Media inspection uses the Pillow library already installed in this
      environment.  This module adds no dependency of its own, but importing it
      requires Pillow at runtime.
    * A request whose bounded input plus requested output exceeds the model
      context window is refused before transport.

The bound must not be described as the provider's token count.  Providers bill
what their tokenizers count; callers reconcile that against reported usage.
"""

from __future__ import annotations

import base64
import binascii
import io
import warnings
from collections.abc import Sequence

from PIL import Image, UnidentifiedImageError

from parkingbreaker.llm import ImageAttachment, neutralise

# Sent to capped calls so provider framing cannot be charged to no budget line.
FRAMING_TOKEN_OVERHEAD = 1024

# A generous per-attachment floor: tiny images still cost a fixed number of
# image tokens, and the floor keeps the reservation conservative.
MIN_TOKENS_PER_IMAGE = 8192

# Structural media limits.  Dimensions are read from the header before any pixel
# data is expanded, so a small compressed file cannot force a huge allocation.
MAX_IMAGE_DIMENSION = 8_000
MAX_IMAGE_PIXELS = 16_000_000

# Only formats whose content this module can structurally verify.  PDFs and webp
# are deliberately absent and must be refused.
BOUNDED_IMAGE_MEDIA_TYPES = {"image/jpeg": "JPEG", "image/png": "PNG"}

SCHEMA_INSTRUCTION = "Reply with JSON only matching this schema: "


class UnboundableRequestError(ValueError):
    """No defensible token bound exists for this request; nothing was transported."""


def text_request_tokens(system: str, user: str, schema_text: str) -> int:
    """Upper-bound tokens for the text parts of one chat request.

    Counts the exact system message the adapters send (``system``, the schema
    instruction and the serialized schema), the neutralized user text, all as
    UTF-8 bytes, plus fixed framing overhead.
    """
    for name, value in (
        ("system", system),
        ("user", user),
        ("schema_text", schema_text),
    ):
        if not isinstance(value, str):
            raise UnboundableRequestError(f"{name} must be text")
    system_message = system + "\n" + SCHEMA_INSTRUCTION + schema_text
    return (
        _utf8_bytes(system_message, "system message")
        + _utf8_bytes(neutralise(user), "user text")
        + FRAMING_TOKEN_OVERHEAD
    )


def image_attachment_tokens(images: Sequence[ImageAttachment] | None) -> int:
    """Reserve a conservative bound for every structurally checked image.

    Each JPEG/PNG attachment reserves the maximum of its encoded data-URL byte
    count, its decoded pixel count and ``MIN_TOKENS_PER_IMAGE``.  Anything that
    is not a single-frame, positive-dimension, in-limit JPEG/PNG refuses before
    transport.
    """
    if images is None:
        return 0
    total = 0
    for image in images:
        if not isinstance(image, ImageAttachment):
            raise UnboundableRequestError("image attachments must be ImageAttachment values")
        expected_format = BOUNDED_IMAGE_MEDIA_TYPES.get(image.media_type)
        if expected_format is None:
            raise UnboundableRequestError(
                "only structurally checkable JPEG/PNG image attachments are bounded; "
                "refusing unsupported media"
            )
        raw = _decode_attachment(image)
        pixels = _validated_pixel_count(raw, expected_format)
        encoded_bytes = len(image.data_url().encode("utf-8"))
        total += max(encoded_bytes, pixels, MIN_TOKENS_PER_IMAGE)
    return total


def request_input_tokens(
    system: str,
    user: str,
    schema_text: str,
    images: Sequence[ImageAttachment] | None = None,
) -> int:
    """Bounded input tokens for a text request plus its structurally checked images."""
    return text_request_tokens(system, user, schema_text) + image_attachment_tokens(images)


def ensure_request_fits_context(input_tokens: int, output_tokens: int, context_tokens: int) -> None:
    """Refuse a request whose bounded input plus requested output exceeds context."""
    for name, value in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("context_tokens", context_tokens),
    ):
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if input_tokens + output_tokens > context_tokens:
        raise UnboundableRequestError(
            "request exceeds the model context limit; refusing before transport"
        )


def _decode_attachment(image: ImageAttachment) -> bytes:
    data = image.data_base64
    if not isinstance(data, str) or not data:
        raise UnboundableRequestError("image attachment has no bounded encoded body")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise UnboundableRequestError("image attachment body is not valid base64") from None
    if not raw:
        raise UnboundableRequestError("image attachment has no bounded encoded body")
    return raw


def _validated_pixel_count(raw: bytes, expected_format: str) -> int:
    """Check format, frame count and dimensions before expanding any pixels."""
    try:
        with warnings.catch_warnings():
            # A dimension check stricter than Pillow's own runs below; its warning
            # would otherwise leak into callers for merely large images.
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as decoded:
                if decoded.format != expected_format:
                    raise UnboundableRequestError(
                        "image attachment content does not match its declared media type"
                    )
                width, height = decoded.size
                if width <= 0 or height <= 0:
                    raise UnboundableRequestError(
                        "image attachment dimensions must be positive and bounded"
                    )
                if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
                    raise UnboundableRequestError(
                        "image attachment dimensions exceed the bounded limit"
                    )
                pixels = width * height
                if pixels > MAX_IMAGE_PIXELS:
                    raise UnboundableRequestError(
                        "image attachment pixel count exceeds the bounded limit"
                    )
                frames = getattr(decoded, "n_frames", 1)
                if type(frames) is not int or frames != 1:
                    raise UnboundableRequestError("image attachment must be a single frame")
                # Dimensions are known to be bounded, so this expansion is bounded.
                decoded.load()
                return pixels
    except UnboundableRequestError:
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        SyntaxError,
        ValueError,
    ):
        raise UnboundableRequestError(
            "image attachment is not a structurally valid bounded JPEG/PNG image"
        ) from None


def _utf8_bytes(text: str, name: str) -> int:
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise UnboundableRequestError(f"{name} is not valid UTF-8 text") from None
