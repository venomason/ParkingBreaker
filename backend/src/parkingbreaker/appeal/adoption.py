"""The customer adopts the typed draft as their own statement (docs/architecture-v6.md 5.5).

An Adoption records who adopted which exact text, and when. It is bound to the text by
SHA-256 over its UTF-8 bytes, so any edit after adoption (even one character or one
space) makes the adoption invalid for the edited text and the customer must adopt again.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

FINAL_SCREEN_TEXT = (
    "Here is your typed draft based on your notes. Read it, edit any details, and approve "
    "if you adopt it as your own statement."
)


class AdoptionError(ValueError):
    pass


@dataclass(frozen=True)
class Adoption:
    customer_id: str
    adopted_at: datetime
    sha256: str
    screen_text: str = FINAL_SCREEN_TEXT


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def adopt(letter_text: str, customer_id: str, clock: Callable[[], datetime]) -> Adoption:
    """Record that `customer_id` adopted exactly `letter_text`, shown FINAL_SCREEN_TEXT."""
    if not letter_text.strip():
        raise AdoptionError("cannot adopt an empty letter")
    if not customer_id.strip():
        raise AdoptionError("customer_id is required")
    return Adoption(customer_id=customer_id, adopted_at=clock(), sha256=text_sha256(letter_text))


def is_adopted(adoption: Adoption, letter_text: str) -> bool:
    """True only if `letter_text` is byte-for-byte the text that was adopted."""
    return adoption.sha256 == text_sha256(letter_text)
