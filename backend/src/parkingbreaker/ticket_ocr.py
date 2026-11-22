"""Bounded OCR routing using explicitly uncalibrated, per-field model estimates.

An independent Gemini reading may corroborate a borderline transcription. Its own
estimate is retained, never averaged or converted into a claimed accuracy probability.
Conflicting characters and low/unknown estimates require customer entry. No model
sees the other's answer, customer reasons or customer corrections during extraction.
"""

import math
import time
from collections.abc import Callable, Mapping, Sequence
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


@dataclass(frozen=True)
class RoutedExtractTrace:
    """Separated primary/escalation monotonic spans and readings for one routed extract."""

    primary_model: str
    secondary_model: str | None
    policy: OcrPolicy
    primary_status: str
    primary_started_monotonic: float
    primary_ended_monotonic: float
    escalation_status: str
    escalation_started_monotonic: float | None
    escalation_ended_monotonic: float | None
    escalation_exception_kind: str | None
    first_reading: TicketReading | None
    second_reading: TicketReading | None
    final_reading: TicketReading | None


class RoutedTicketReader(TicketVisionReader):
    """One primary extraction and at most one independent secondary extraction."""

    def __init__(
        self,
        primary: TicketVisionReader,
        primary_model: str,
        secondary: TicketVisionReader | None = None,
        policy: OcrPolicy | None = None,
        *,
        secondary_model: str | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._primary = primary
        self._primary_model = primary_model
        self._secondary = secondary
        self._secondary_model = secondary_model
        self._policy = policy if policy is not None else OcrPolicy()
        self._monotonic = time.monotonic if monotonic is None else monotonic
        self._last_routed_trace: RoutedExtractTrace | None = None

    @property
    def policy(self) -> OcrPolicy:
        return self._policy

    @property
    def last_routed_trace(self) -> RoutedExtractTrace | None:
        return self._last_routed_trace

    def review(
        self,
        images: Sequence[ImageAttachment],
        confirmed: Mapping[str, str],
        *,
        max_cost_cents: int,
    ) -> TicketReview:
        return self._primary.review(images, confirmed, max_cost_cents=max_cost_cents)

    def extract(self, images: Sequence[ImageAttachment], *, max_cost_cents: int) -> TicketReading:
        self._last_routed_trace = None
        p_start = self._monotonic()
        if type(max_cost_cents) is not int or max_cost_cents <= 0:
            p_end = self._monotonic()
            self._last_routed_trace = RoutedExtractTrace(
                primary_model=self._primary_model,
                secondary_model=self._secondary_model,
                policy=self._policy,
                primary_status="input_error",
                primary_started_monotonic=p_start,
                primary_ended_monotonic=p_end,
                escalation_status="not_applicable",
                escalation_started_monotonic=None,
                escalation_ended_monotonic=None,
                escalation_exception_kind=None,
                first_reading=None,
                second_reading=None,
                final_reading=None,
            )
            raise TicketVisionInputError("max_cost_cents must be positive")
        primary_limit = (
            max(1, max_cost_cents // 2) if self._secondary is not None else max_cost_cents
        )
        try:
            first = self._primary.extract(images, max_cost_cents=primary_limit)
        except Exception as exc:
            p_end = self._monotonic()
            if isinstance(exc, LlmBudgetExceeded):
                p_status = "budget_refused"
            elif isinstance(exc, (TicketVisionInputError, UnboundableRequestError)):
                p_status = "input_error"
            else:
                p_status = "provider_error"
            self._last_routed_trace = RoutedExtractTrace(
                primary_model=self._primary_model,
                secondary_model=self._secondary_model,
                policy=self._policy,
                primary_status=p_status,
                primary_started_monotonic=p_start,
                primary_ended_monotonic=p_end,
                escalation_status="not_applicable",
                escalation_started_monotonic=None,
                escalation_ended_monotonic=None,
                escalation_exception_kind=None,
                first_reading=None,
                second_reading=None,
                final_reading=None,
            )
            raise
        p_end = self._monotonic()
        if first.refused:
            final_refused = replace(first, primary_model=self._primary_model, escalation="manual")
            self._last_routed_trace = RoutedExtractTrace(
                primary_model=self._primary_model,
                secondary_model=self._secondary_model,
                policy=self._policy,
                primary_status="refused",
                primary_started_monotonic=p_start,
                primary_ended_monotonic=p_end,
                escalation_status="not_applicable",
                escalation_started_monotonic=None,
                escalation_ended_monotonic=None,
                escalation_exception_kind=None,
                first_reading=first,
                second_reading=None,
                final_reading=final_refused,
            )
            return final_refused
        borderline = {
            f.field
            for f in first.fields
            if f.state is ReadingState.SHOWN
            and f.confidence is not None
            and self._policy.manual_below <= f.confidence < self._policy.escalate_below
        }
        second = None
        status = "not_needed"
        s_start: float | None = None
        s_end: float | None = None
        escalation_exc_kind: str | None = None
        remaining = max_cost_cents - max(primary_limit, first.usage.cost_cents)
        if borderline:
            status = "unavailable" if self._secondary is None else "budget_exhausted"
            if self._secondary is not None and remaining > 0:
                s_start = self._monotonic()
                try:
                    second = self._secondary.extract(images, max_cost_cents=remaining)
                    status = "refused" if second.refused else "completed"
                except (
                    LlmProviderError,
                    LlmBudgetExceeded,
                    TicketVisionInputError,
                    UnboundableRequestError,
                ) as exc:
                    status = "failed"
                    if isinstance(exc, LlmBudgetExceeded):
                        escalation_exc_kind = "budget_exhausted"
                    elif isinstance(exc, (TicketVisionInputError, UnboundableRequestError)):
                        escalation_exc_kind = "input_error"
                    else:
                        escalation_exc_kind = "provider_error"
                except Exception:
                    s_end = self._monotonic()
                    self._last_routed_trace = RoutedExtractTrace(
                        primary_model=self._primary_model,
                        secondary_model=self._secondary_model,
                        policy=self._policy,
                        primary_status="completed",
                        primary_started_monotonic=p_start,
                        primary_ended_monotonic=p_end,
                        escalation_status="failed",
                        escalation_started_monotonic=s_start,
                        escalation_ended_monotonic=s_end,
                        escalation_exception_kind="provider_error",
                        first_reading=first,
                        second_reading=None,
                        final_reading=None,
                    )
                    raise
                finally:
                    if s_end is None:
                        s_end = self._monotonic()
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
        final_reading = replace(
            first,
            fields=tuple(fields),
            usage=usage,
            primary_model=self._primary_model,
            escalation=status,
        )
        self._last_routed_trace = RoutedExtractTrace(
            primary_model=self._primary_model,
            secondary_model=self._secondary_model,
            policy=self._policy,
            primary_status="completed",
            primary_started_monotonic=p_start,
            primary_ended_monotonic=p_end,
            escalation_status=status,
            escalation_started_monotonic=s_start,
            escalation_ended_monotonic=s_end,
            escalation_exception_kind=escalation_exc_kind,
            first_reading=first,
            second_reading=second,
            final_reading=final_reading,
        )
        return final_reading
