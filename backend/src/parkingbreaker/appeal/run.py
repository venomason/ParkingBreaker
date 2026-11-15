"""Drafting orchestration through the sole correction policy (v6 sections 4.2/5.4/6.1)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from parkingbreaker.agencies import ResolvedAgency
from parkingbreaker.appeal.correction import (
    CustomerReview,
    Flag,
    Redraft,
    SpanAuthor,
    TextSpan,
    check_spans,
    flags_from_problems,
    next_correction_step,
)
from parkingbreaker.appeal.drafting import Drafter, TicketFacts, with_notice_sentence
from parkingbreaker.appeal.references import Reference
from parkingbreaker.llm import Usage
from parkingbreaker.mode_gate import Operation, mode_permission
from parkingbreaker.service_policy import (
    SERVICE_CONTROL,
    STATE_BASIS,
    ServiceControlEntry,
    StateBasis,
)
from parkingbreaker.session_window import WindowStatus, window_status


class DraftingStatus(StrEnum):
    READY = "ready"
    CUSTOMER_REVIEW = "customer_review"
    WINDOW_EXPIRED = "window_expired"
    MODE_BLOCKED = "mode_blocked"


@dataclass(frozen=True)
class DraftingResult:
    status: DraftingStatus
    spans: tuple[TextSpan, ...]
    flags: tuple[Flag, ...]
    usage: Usage
    attempts: int
    reason: str | None = None


def run_drafting(
    notes: str,
    references: Sequence[Reference],
    facts: TicketFacts,
    drafter: Drafter,
    clock: Callable[[], datetime],
    authorized_at: datetime,
    get_agency: Callable[[], ResolvedAgency],
    *,
    new_revision: Callable[[], str],
    get_state_basis: Callable[[], Mapping[str, StateBasis]] = lambda: STATE_BASIS,
    get_service_control: Callable[[], Sequence[ServiceControlEntry]] = lambda: SERVICE_CONTROL,
) -> DraftingResult:
    """Draft, validate, and apply the bounded correction policy inside one window.

    Clock and policy access are injected. The Drafter is the only external call;
    permission and window checks are repeated before each attempt. Returned work is
    retained if the window expires while that call is in flight.
    The caller supplies a fresh revision id for each run, distinct from workspace history.
    """
    spans: tuple[TextSpan, ...] = ()
    flags: tuple[Flag, ...] = ()
    usage = Usage()
    attempts = 0
    feedback: tuple[str, ...] = ()
    # Separate invocations can coexist in workspace history; never recycle span ids.
    revision = new_revision()

    while True:
        if window_status(authorized_at, clock()) is WindowStatus.EXPIRED:
            return DraftingResult(
                DraftingStatus.WINDOW_EXPIRED,
                spans,
                flags,
                usage,
                attempts,
                "authorization window expired",
            )

        permission = mode_permission(
            get_agency(),
            Operation.DRAFT,
            state_basis=get_state_basis(),
            controls=get_service_control(),
        )
        if not permission.allowed:
            return DraftingResult(
                DraftingStatus.MODE_BLOCKED,
                spans,
                flags,
                usage,
                attempts,
                permission.reason,
            )

        # Policy lookup can consume the last seconds of the fixed window.
        if window_status(authorized_at, clock()) is WindowStatus.EXPIRED:
            return DraftingResult(
                DraftingStatus.WINDOW_EXPIRED,
                spans,
                flags,
                usage,
                attempts,
                "authorization window expired",
            )

        result = drafter.draft(notes, references, facts, feedback)
        attempts += 1
        usage += result.usage
        deterministic = bool(getattr(drafter, "deterministic", False))
        spans = tuple(
            TextSpan(
                # A late flag action for an earlier draft must not target a replacement.
                id=(
                    f"{revision}:customer-{index + 1}"
                    if deterministic
                    else f"{revision}:ai-{attempts}-{index + 1}"
                ),
                text=sentence.text,
                author=SpanAuthor.CUSTOMER if deterministic else SpanAuthor.AI,
                support=sentence.support,
                paragraph=sentence.paragraph,
            )
            for index, sentence in enumerate(with_notice_sentence(result.draft, facts).sentences)
        )
        problems = check_spans(spans, notes, references, facts)
        flags = flags_from_problems(spans, problems)

        if window_status(authorized_at, clock()) is WindowStatus.EXPIRED:
            return DraftingResult(
                DraftingStatus.WINDOW_EXPIRED,
                spans,
                flags,
                usage,
                attempts,
                "authorization window expired",
            )

        if not flags:
            return DraftingResult(DraftingStatus.READY, spans, (), usage, attempts)

        if deterministic:
            # The customer's own text is the draft: there is nothing to redraft and nothing
            # to adopt around. Surface every finding for the customer to resolve directly.
            return DraftingResult(
                DraftingStatus.CUSTOMER_REVIEW,
                spans,
                flags,
                usage,
                attempts,
                "customer text needs a change before approval",
            )

        step = next_correction_step(attempts, flags)
        if isinstance(step, CustomerReview):
            return DraftingResult(
                DraftingStatus.CUSTOMER_REVIEW,
                spans,
                step.flags,
                usage,
                attempts,
            )
        assert isinstance(step, Redraft)
        feedback = step.feedback
