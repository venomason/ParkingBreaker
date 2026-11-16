"""Bounded OCR routing using explicitly uncalibrated, per-field model estimates.

An independent Gemini reading may corroborate a borderline transcription. Its own
estimate is retained, never averaged or converted into a claimed accuracy probability.
Conflicting characters and low/unknown estimates require customer entry. No model
sees the other's answer, customer reasons or customer corrections during extraction.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from parkingbreaker.llm import ImageAttachment, LlmBudgetExceeded, LlmProviderError, Usage
from parkingbreaker.llm_input_budget import UnboundableRequestError
from parkingbreaker.ticket_vision import (
    FieldReading,
    ReadingState,
    TicketReading,
    TicketReview,
    TicketVisionInputError,
    TicketVisionReader,
)


@dataclass(frozen=True)
class OcrPolicy:
    manual_below: float = 0.80
    escalate_below: float = 0.90

    def __post_init__(self) -> None:
        if (
            any(
                type(v) not in (int, float) or not math.isfinite(v)
                for v in (self.manual_below, self.escalate_below)
            )
            or not 0 <= self.manual_below < self.escalate_below <= 1
        ):
            raise ValueError("OCR thresholds must satisfy 0 <= manual < escalation <= 1")


class RoutedTicketReader(TicketVisionReader):
    """One primary extraction and at most one independent secondary extraction."""

    def __init__(
        self,
        primary: TicketVisionReader,
        primary_model: str,
        secondary: TicketVisionReader | None = None,
        policy: OcrPolicy | None = None,
    ) -> None:
        self._primary = primary
        self._primary_model = primary_model
        self._secondary = secondary
        self._policy = policy if policy is not None else OcrPolicy()

    def review(
        self,
        images: Sequence[ImageAttachment],
        confirmed: Mapping[str, str],
        *,
        max_cost_cents: int,
    ) -> TicketReview:
        return self._primary.review(images, confirmed, max_cost_cents=max_cost_cents)

    def extract(self, images: Sequence[ImageAttachment], *, max_cost_cents: int) -> TicketReading:
        if type(max_cost_cents) is not int or max_cost_cents <= 0:
            raise TicketVisionInputError("max_cost_cents must be positive")
        primary_limit = (
            max(1, max_cost_cents // 2) if self._secondary is not None else max_cost_cents
        )
        first = self._primary.extract(images, max_cost_cents=primary_limit)
        if first.refused:
            return replace(first, primary_model=self._primary_model, escalation="manual")
        borderline = {
            f.field
            for f in first.fields
            if f.state is ReadingState.SHOWN
            and f.confidence is not None
            and self._policy.manual_below <= f.confidence < self._policy.escalate_below
        }
        second = None
        status = "not_needed"
        remaining = max_cost_cents - max(primary_limit, first.usage.cost_cents)
        if borderline:
            status = "unavailable" if self._secondary is None else "budget_exhausted"
            if self._secondary is not None and remaining > 0:
                try:
                    second = self._secondary.extract(images, max_cost_cents=remaining)
                    status = "refused" if second.refused else "completed"
                except (
                    LlmProviderError,
                    LlmBudgetExceeded,
                    TicketVisionInputError,
                    UnboundableRequestError,
                ):
                    status = "failed"
        corroboration = {f.field: f for f in second.fields} if second and not second.refused else {}
        fields: list[FieldReading] = []
        for field in first.fields:
            if field.state is not ReadingState.SHOWN:
                fields.append(field)
                continue
            independent = corroboration.get(field.field)
            if independent is not None and (
                independent.state is not ReadingState.SHOWN or independent.value != field.value
            ):
                fields.append(FieldReading(field.field, ReadingState.UNREADABLE, None))
                continue
            if field.confidence is not None and field.confidence >= self._policy.escalate_below:
                fields.append(field)
                continue
            other = corroboration.get(field.field) if field.field in borderline else None
            if (
                other is not None
                and other.state is ReadingState.SHOWN
                and other.value == field.value
                and other.confidence is not None
                and other.confidence >= self._policy.escalate_below
            ):
                # This is the secondary estimate with exact agreement, not a pooled probability.
                fields.append(other)
            else:
                fields.append(FieldReading(field.field, ReadingState.UNREADABLE, None))
        usage = first.usage
        if second is not None:
            usage = Usage(
                first.usage.input_tokens + second.usage.input_tokens,
                first.usage.output_tokens + second.usage.output_tokens,
                first.usage.cost_cents + second.usage.cost_cents,
            )
        return replace(
            first,
            fields=tuple(fields),
            usage=usage,
            primary_model=self._primary_model,
            escalation=status,
        )
