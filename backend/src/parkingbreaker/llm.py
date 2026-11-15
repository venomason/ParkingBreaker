"""The one LLM interface. Research extraction and drafting both go through LlmClient.

Provider adapters and tests share this contract. A real client must return parsed JSON that the
caller then validates strictly, and must report usage so cost can be recorded per case.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple, Protocol, cast

from parkingbreaker.json_types import JsonValue


@dataclass(frozen=True)
class ImageAttachment:
    media_type: str  # "image/png", "image/jpeg", "image/webp", "application/pdf"
    data_base64: str
    description: str | None = None

    def data_url(self) -> str:
        return f"data:{self.media_type};base64,{self.data_base64}"


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_cents: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cost_cents + other.cost_cents,
        )


class LlmBudgetExceeded(Exception):
    """The request cannot fit its spending ceiling; no provider request was made."""


class LlmProviderError(Exception):
    """A sanitized provider failure. Retryability is advice, never an automatic retry."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def schema_json(schema: Mapping[str, JsonValue]) -> str:
    return json.dumps(dict(schema), ensure_ascii=False, allow_nan=False)


def conservative_input_tokens(system: str, user: str, schema_text: str) -> int:
    """Ceil UTF-8 bytes / 2 plus 500 tokens for provider framing/schema overhead."""
    size = len((system + user + schema_text).encode("utf-8"))
    return (size + 1) // 2 + 500


def parse_json_text(text: str) -> JsonValue:
    """Strip a whole JSON code fence; malformed JSON is dropped, never repaired."""
    text = text.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[len("```json") : -3].strip()
    try:
        return cast(JsonValue, json.loads(text, parse_constant=_invalid_json_constant))
    except (ValueError, RecursionError):
        return {}


def _invalid_json_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


class BudgetExhaustedResult[T](NamedTuple):
    """An empty assessment and zero usage, marked for callers without changing unpacking."""

    result: T
    usage: Usage = Usage()


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str
    schema: Mapping[str, JsonValue]


class LlmClient(Protocol):
    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
    ) -> tuple[JsonValue, Usage]:
        """Return parsed JSON and usage, subject to a hard per-call spending ceiling.

        None preserves uncapped behaviour. A negative ceiling raises ValueError;
        zero always raises LlmBudgetExceeded without a provider request.

        With a ceiling, the client must never spend more than max_cost_cents. It
        must conservatively estimate all billable input (including system, user,
        schema and provider overhead), and cap the output tokens it sends so the
        worst-case input plus capped output cost fits the ceiling. If that request
        could exceed the ceiling, raise LlmBudgetExceeded BEFORE any provider
        request. Rounding and any retries must also fit this per-call ceiling.
        """
        ...


class MultimodalLlmClient(LlmClient, Protocol):
    """Extended interface for vision-capable models (e.g. Gemini 3.8 Flash)."""

    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
        images: Sequence[ImageAttachment] | None = None,
    ) -> tuple[JsonValue, Usage]: ...


DATA_DELIMITER = "<<<"
_NEUTRALISED = "<\u200b<<"


def neutralise(text: str) -> str:
    """Break up the data-block delimiter inside untrusted text so it cannot close a block."""
    return text.replace(DATA_DELIMITER, _NEUTRALISED)
