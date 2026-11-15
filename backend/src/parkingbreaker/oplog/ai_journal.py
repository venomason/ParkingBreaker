"""The AI journal: what a model worker noticed on one appeal, as closed categories. Pure.

The journal is context for choosing what to test next. It has zero financial or mailing
authority: no payment, mail, case-state or report code reads it, and no figure in a
report may come from it (reports reconcile to oplog.appeal_events). Entries hold enum
categories and bounded counts only, never free text, quotes, identifiers or exact
amounts, so nothing a customer wrote or a page said can reach it. A failed or skipped
model call is still journaled, with its fallback status.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum

MAX_COUNT = 1_000_000


class AiWorker(StrEnum):
    INTAKE_READING = "INTAKE_READING"
    RESEARCH = "RESEARCH"
    DRAFTING = "DRAFTING"
    PACKET_CHECK = "PACKET_CHECK"
    SUPPORT_DRAFT = "SUPPORT_DRAFT"
    OPS_DIGEST = "OPS_DIGEST"


class AiNote(StrEnum):
    SOURCE_FOUND = "SOURCE_FOUND"
    SOURCE_MISSING = "SOURCE_MISSING"
    QUOTE_REJECTED = "QUOTE_REJECTED"
    ADDRESS_CONFLICT = "ADDRESS_CONFLICT"
    DEADLINE_UNCLEAR = "DEADLINE_UNCLEAR"
    REQUIREMENT_UNCLEAR = "REQUIREMENT_UNCLEAR"
    CUSTOMER_QUESTION = "CUSTOMER_QUESTION"
    PACKET_MISMATCH = "PACKET_MISMATCH"
    NOTHING_NOTABLE = "NOTHING_NOTABLE"


class AiCallStatus(StrEnum):
    OK = "OK"
    FALLBACK_AFTER_FAILURE = "FALLBACK_AFTER_FAILURE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    SKIPPED = "SKIPPED"


_FIELDS = ("worker", "note", "status", "calls", "input_tokens", "output_tokens")


@dataclass(frozen=True)
class AiJournalEntry:
    worker: AiWorker
    note: AiNote
    status: AiCallStatus
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        for name, kind in (("worker", AiWorker), ("note", AiNote), ("status", AiCallStatus)):
            if type(getattr(self, name)) is not kind:
                raise ValueError(f"{name} must be a {kind.__name__}")
        for name in ("calls", "input_tokens", "output_tokens"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= MAX_COUNT:
                raise ValueError(f"{name} must be a bounded count")

    def to_json(self) -> str:
        return json.dumps(
            {
                name: str(getattr(self, name)) if name in _FIELDS[:3] else getattr(self, name)
                for name in _FIELDS
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> AiJournalEntry:
        data = json.loads(text)
        if not isinstance(data, dict) or set(data) != set(_FIELDS):
            raise ValueError("journal entry has unknown or missing fields")
        return cls(
            AiWorker(data["worker"]),
            AiNote(data["note"]),
            AiCallStatus(data["status"]),
            data["calls"],
            data["input_tokens"],
            data["output_tokens"],
        )


_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}")
MAX_CALL_CENTS = 100_000


@dataclass(frozen=True)
class AiCall:
    """One worker run's model usage on one appeal: what it cost and how it ended."""

    worker: AiWorker
    model: str  # a configured preset or model id label, never free text
    status: AiCallStatus
    input_tokens: int | None = 0
    output_tokens: int | None = 0
    cost_cents: int | None = 0

    def __post_init__(self) -> None:
        if type(self.worker) is not AiWorker or type(self.status) is not AiCallStatus:
            raise ValueError("worker and status must be closed categories")
        if not isinstance(self.model, str) or _LABEL.fullmatch(self.model) is None:
            raise ValueError("model must be a configuration label")
        values = (self.input_tokens, self.output_tokens, self.cost_cents)
        if any(value is None for value in values):
            if self.status is not AiCallStatus.FALLBACK_AFTER_FAILURE or not all(
                value is None for value in values
            ):
                raise ValueError("unknown usage requires a failed call with all values unknown")
            return
        for name, limit in (
            ("input_tokens", MAX_COUNT),
            ("output_tokens", MAX_COUNT),
            ("cost_cents", MAX_CALL_CENTS),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= limit:
                raise ValueError(f"{name} must be a bounded count")

    def to_json(self) -> str:
        return json.dumps(
            {
                "worker": str(self.worker),
                "model": self.model,
                "status": str(self.status),
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cost_cents": self.cost_cents,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> AiCall:
        data = json.loads(text)
        fields = {"worker", "model", "status", "input_tokens", "output_tokens", "cost_cents"}
        if not isinstance(data, dict) or set(data) != fields:
            raise ValueError("AI call has unknown or missing fields")
        return cls(
            AiWorker(data["worker"]),
            data["model"],
            AiCallStatus(data["status"]),
            data["input_tokens"],
            data["output_tokens"],
            data["cost_cents"],
        )
