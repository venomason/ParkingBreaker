"""Per-subject LLM call logging and spending caps without changing the provider boundary."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Protocol

from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import ImageAttachment, LlmBudgetExceeded, LlmClient, Usage
from parkingbreaker.oplog.ai_journal import AiCall, AiCallStatus, AiWorker

AppendCall = Callable[[str, AiCall, datetime], object]
Clock = Callable[[], datetime]


class SpendLedger(Protocol):
    def reserve_ai_spend(
        self,
        subject: str,
        worker: AiWorker,
        cap_cents: int,
        at: datetime,
        ceiling_cents: int | None = None,
    ) -> tuple[int, int] | None: ...

    def settle_ai_spend(
        self, subject: str, worker: AiWorker, seq: int, cost_cents: int
    ) -> None: ...


class CallLoggingLlmClient:
    """Log each result for one fixed opaque subject; never log prompts or provider errors."""

    def __init__(
        self,
        inner: LlmClient,
        subject_token: str,
        worker: AiWorker,
        model_label: str,
        append_call: AppendCall,
        clock: Clock,
        *,
        spend: SpendLedger | None = None,
        cap_cents: int | None = None,
    ) -> None:
        if (
            not isinstance(subject_token, str)
            or not subject_token.strip()
            or len(subject_token) > 128
        ):
            raise ValueError("subject_token must be an opaque non-blank token")
        if type(worker) is not AiWorker:
            raise ValueError("worker must be an AiWorker")
        self._inner = inner
        self._subject_token = subject_token
        self._worker = worker
        self._model_label = model_label
        self._append_call = append_call
        self._clock = clock
        if (spend is None) != (cap_cents is None):
            raise ValueError("a spending cap needs its ledger and a ledger needs its cap")
        self._spend = spend
        self._cap_cents = cap_cents
        # Validate label before any provider call; construction writes no journal rows.
        AiCall(worker, model_label, AiCallStatus.OK)

    def _record(
        self,
        status: AiCallStatus,
        usage: Usage | None,
    ) -> None:
        try:
            call = AiCall(
                self._worker,
                self._model_label,
                status,
                None if usage is None else usage.input_tokens,
                None if usage is None else usage.output_tokens,
                None if usage is None else usage.cost_cents,
            )
            self._append_call(self._subject_token, call, self._clock())
        except Exception:
            # Observability is best-effort and never changes provider behavior.
            return

    def complete_json(
        self,
        system: str,
        user: str,
        schema: Mapping[str, JsonValue],
        *,
        max_cost_cents: int | None = None,
        images: Sequence[ImageAttachment] | None = None,
    ) -> tuple[JsonValue, Usage]:
        reservation = self._reserve(max_cost_cents)
        ceiling = max_cost_cents if reservation is None else reservation[1]
        try:
            if images is not None:
                try:
                    result = self._inner.complete_json(  # type: ignore[call-arg]
                        system, user, schema, max_cost_cents=ceiling, images=images
                    )
                except TypeError:
                    raise ValueError(
                        "Inner LLM client does not support image attachments"
                    ) from None
            else:
                result = self._inner.complete_json(system, user, schema, max_cost_cents=ceiling)
        except LlmBudgetExceeded:
            # Refused before any provider request (LlmClient contract): nothing was spent.
            self._settle(reservation, 0)
            self._record(AiCallStatus.BUDGET_EXCEEDED, Usage())
            raise
        except Exception:
            # Cost unknown: the reservation stays counted in full.
            self._record(AiCallStatus.FALLBACK_AFTER_FAILURE, None)
            raise
        self._settle(reservation, result[1].cost_cents)
        self._record(AiCallStatus.OK, result[1])
        return result

    def _reserve(self, ceiling: int | None) -> tuple[int, int] | None:
        """Reserve this call's ceiling from the appeal's allowance, or stop the worker.

        Without a caller ceiling one call may use at most half the whole allowance, so a
        single failed call whose cost stays unknown cannot use it all up.
        """
        if self._spend is None or self._cap_cents is None:
            return None
        if ceiling is None:
            ceiling = max(1, self._cap_cents // 2)
        try:
            reservation = self._spend.reserve_ai_spend(
                self._subject_token, self._worker, self._cap_cents, self._clock(), ceiling
            )
        except sqlite3.OperationalError:
            raise  # a busy database is retried by the next sweep, never a budget stop
        except Exception:
            reservation = None  # an unreadable ledger fails closed
        if reservation is None:
            self._record(AiCallStatus.BUDGET_EXCEEDED, Usage())
            raise LlmBudgetExceeded("appeal spending cap reached for this worker")
        return reservation

    def _settle(self, reservation: tuple[int, int] | None, cost_cents: int) -> None:
        if reservation is None or self._spend is None:
            return
        try:
            self._spend.settle_ai_spend(
                self._subject_token, self._worker, reservation[0], max(0, cost_cents)
            )
        except Exception:
            return  # an unsettled reservation stays counted in full
