"""Pure binding of literal ticket review inputs and explicitly adopted facts.

This module is a validated immutable envelope plus stable hashes; it is not a
store, a route, a UI, a provider or a verifier. It reads no environment, file,
network or clock, makes no model call and never edits supplied facts.

Two projections are bound together:

* the literal 11-field reading (exactly :class:`TicketField`), where every key is
  present and each value is bounded printable single-line text or an explicit
  ``None`` for not-shown/unreadable. Dates stay the literal visible strings.
* the adopted 13 ``TicketFacts`` properties, every key present, with dates only
  as explicit ISO calendar dates or ``date`` values. No default issuer kind,
  stage or violation code is ever supplied.

Hashes are binding only. A matching digest or a :meth:`TicketFactBinding.matches`
result is not adoption, verification or truth.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, cast

from pydantic import ValidationError

from parkingbreaker.api.schemas import TicketFacts
from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import ImageAttachment
from parkingbreaker.llm_input_budget import UnboundableRequestError, image_attachment_tokens
from parkingbreaker.ticket_vision import TicketField

MAX_FIELD_CHARS: Final[int] = 200
MIN_IMAGES: Final[int] = 1
MAX_IMAGES: Final[int] = 2
CLOSED_FIELD_VERSION: Final[str] = "ticket-fact-binding-v1"
IMAGES_BINDING_VERSION: Final[str] = "ticket-images-binding-v1"
REVIEW_BINDING_VERSION: Final[str] = "ticket-review-binding-v1"

LITERAL_FIELD_ORDER: Final[tuple[str, ...]] = tuple(field.value for field in TicketField)
ADOPTED_FIELD_ORDER: Final[tuple[str, ...]] = (
    "citation_number",
    "license_plate",
    "license_plate_state",
    "issuer_hint",
    "issued_city",
    "issued_state",
    "issuer_kind",
    "violation_code",
    "violation_description",
    "issue_date",
    "stage",
    "notice_date",
    "printed_deadline",
)
DATE_FIELDS: Final[frozenset[str]] = frozenset({"issue_date", "notice_date", "printed_deadline"})
OPTIONAL_DATE_FIELDS: Final[frozenset[str]] = frozenset({"notice_date", "printed_deadline"})
ISSUER_KINDS: Final[frozenset[str]] = frozenset({"GOVERNMENT", "PRIVATE"})
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class TicketFactBindingError(ValueError):
    """Supplied facts or media are not a bounded, explicitly closed binding."""


def _closed_mapping(
    value: Mapping[str, object], expected: tuple[str, ...], what: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TicketFactBindingError(f"{what} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise TicketFactBindingError(f"{what} keys must be text")
    if len(value) != len(expected) or set(value) != set(expected):
        raise TicketFactBindingError(f"{what} must carry exactly the closed field set")
    return value


def _bounded_line(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TicketFactBindingError(f"{what} must be non-blank text")
    if len(value) > MAX_FIELD_CHARS:
        raise TicketFactBindingError(f"{what} exceeds {MAX_FIELD_CHARS} characters")
    if not value.isprintable():
        raise TicketFactBindingError(f"{what} must be a single visible line")
    return value


def _normalise_literal(literal: Mapping[str, object]) -> tuple[tuple[str, str | None], ...]:
    supplied = _closed_mapping(literal, LITERAL_FIELD_ORDER, "literal projection")
    values: list[tuple[str, str | None]] = []
    for key in LITERAL_FIELD_ORDER:
        value = supplied[key]
        values.append((key, None if value is None else _bounded_line(value, f"literal {key}")))
    return tuple(values)


def _adopted_date(value: object, key: str) -> str | None:
    if value is None:
        if key in OPTIONAL_DATE_FIELDS:
            return None
        raise TicketFactBindingError(f"adopted {key} must be an explicit calendar date")
    if isinstance(value, datetime):
        raise TicketFactBindingError(f"adopted {key} must be a calendar date, never a datetime")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        if _ISO_DATE.fullmatch(value) is None:
            raise TicketFactBindingError(f"adopted {key} must be an ISO calendar date")
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            raise TicketFactBindingError(f"adopted {key} is not a real calendar date") from None
    raise TicketFactBindingError(f"adopted {key} must be an ISO date string, a date or null")


def _normalise_adopted(adopted: Mapping[str, object]) -> tuple[tuple[str, str | None], ...]:
    supplied = _closed_mapping(adopted, ADOPTED_FIELD_ORDER, "adopted projection")
    check: dict[str, object] = {}
    for key in ADOPTED_FIELD_ORDER:
        value = supplied[key]
        if key in DATE_FIELDS:
            check[key] = _adopted_date(value, key)
        elif not isinstance(value, str):
            raise TicketFactBindingError(f"adopted {key} must be text")
        elif key == "issuer_kind" and value not in ISSUER_KINDS:
            raise TicketFactBindingError("adopted issuer_kind must be GOVERNMENT or PRIVATE")
        else:
            check[key] = value
    try:
        TicketFacts.model_validate(check)
    except ValidationError:
        raise TicketFactBindingError(
            "adopted facts do not satisfy the TicketFacts schema"
        ) from None
    return tuple((key, cast(str | None, check[key])) for key in ADOPTED_FIELD_ORDER)


def _digest(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_digest(value: object, what: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise TicketFactBindingError(f"{what} must be a lowercase sha256 digest")
    return value


@dataclass(frozen=True, slots=True)
class TicketFactBinding:
    """An immutable, fully validated literal/adopted projection pair."""

    literal: tuple[tuple[str, str | None], ...]
    adopted: tuple[tuple[str, str | None], ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.literal, tuple)
            or not isinstance(self.adopted, tuple)
            or tuple(key for key, _ in self.literal) != LITERAL_FIELD_ORDER
            or tuple(key for key, _ in self.adopted) != ADOPTED_FIELD_ORDER
            or self.literal != _normalise_literal(dict(self.literal))
            or self.adopted != _normalise_adopted(dict(self.adopted))
        ):
            raise TicketFactBindingError("the envelope is not a validated closed projection")

    @classmethod
    def bind(
        cls, literal: Mapping[str, object], adopted: Mapping[str, object]
    ) -> TicketFactBinding:
        """Validate both explicit projections; nothing is defaulted or inferred."""
        return cls(_normalise_literal(literal), _normalise_adopted(adopted))

    @property
    def closed_field_digest(self) -> str:
        """Stable versioned digest over both ordered, JSON-safe projections."""
        return _digest(
            {
                "version": CLOSED_FIELD_VERSION,
                "literal": [[key, value] for key, value in self.literal],
                "adopted": [[key, value] for key, value in self.adopted],
            }
        )

    def matches(self, submitted: TicketFacts) -> bool:
        """Exact 13-property equality with the persisted adopted projection."""
        if not isinstance(submitted, TicketFacts):
            raise TicketFactBindingError("submitted facts must be TicketFacts")
        submitted_values: list[tuple[str, str | None]] = []
        for key in ADOPTED_FIELD_ORDER:
            value = getattr(submitted, key)
            if key in DATE_FIELDS:
                submitted_values.append((key, _adopted_date(value, key)))
            elif not isinstance(value, str):
                raise TicketFactBindingError(f"submitted {key} must be text")
            else:
                submitted_values.append((key, value))
        return tuple(submitted_values) == self.adopted

    def review_fields(self) -> dict[str, str]:
        """Only the non-null literal closed values; never adopted-only fields."""
        return {key: value for key, value in self.literal if value is not None}


def ordered_images_digest(images: Sequence[ImageAttachment]) -> str:
    """Stable digest over 1-2 ordered, structurally preflighted JPEG/PNG images."""
    if not isinstance(images, (list, tuple)):
        raise TicketFactBindingError("images must be a list or tuple of attachments")
    if not MIN_IMAGES <= len(images) <= MAX_IMAGES:
        raise TicketFactBindingError(
            f"exactly {MIN_IMAGES}-{MAX_IMAGES} ticket images are required"
        )
    try:
        image_attachment_tokens(images)
    except UnboundableRequestError:
        raise TicketFactBindingError(
            "only structurally valid single-frame JPEG/PNG images are accepted"
        ) from None
    entries: list[JsonValue] = []
    for image in images:
        try:
            raw = base64.b64decode(image.data_base64, validate=True)
        except (binascii.Error, ValueError):
            raise TicketFactBindingError("image attachment body is not valid base64") from None
        entries.append([image.media_type, hashlib.sha256(raw).hexdigest()])
    return _digest({"version": IMAGES_BINDING_VERSION, "images": entries})


def review_binding_digest(images_digest: str, closed_field_digest: str) -> str:
    """Combine both digests using the durable store's agreed array protocol."""
    payload = [
        REVIEW_BINDING_VERSION,
        _require_digest(images_digest, "images_digest"),
        _require_digest(closed_field_digest, "closed_field_digest"),
    ]
    canonical = json.dumps(payload, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()
