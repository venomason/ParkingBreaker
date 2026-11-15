"""The per-appeal event log: code-recorded milestones read from the case history. Pure.

The restricted case history (pb_case_history) is written only by the transaction store,
in the same SQLite transaction as each letter/payment change, and it is append-only. This
module turns one case's history into a closed list of milestones, and those milestones into
privacy-safe telemetry. It is the ground truth for reports: every reported figure must be
reproducible from these events. AI journals never feed it and it never decides a
transaction.

Telemetry carries categories, counts and whole-hour durations only: no case token,
provider ids, exact times or amounts.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from parkingbreaker.case_state import CaseState, StopFamily, StopReason, stop_family
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.transaction_records import TransitionRecord


class AppealEventKind(StrEnum):
    CARD_AUTHORIZED = "CARD_AUTHORIZED"
    NEEDS_CUSTOMER = "NEEDS_CUSTOMER"
    RESEARCH_RESUMED = "RESEARCH_RESUMED"
    RESEARCH_VERIFIED = "RESEARCH_VERIFIED"
    DRAFT_READY = "DRAFT_READY"
    PACKET_FROZEN = "PACKET_FROZEN"
    MAIL_SUBMITTED = "MAIL_SUBMITTED"
    MAILED = "MAILED"
    DELIVERED = "DELIVERED"
    RETURNED = "RETURNED"
    RETURN_CLOSED = "RETURN_CLOSED"
    STOPPED = "STOPPED"
    CHARGE_REQUESTED = "CHARGE_REQUESTED"
    CHARGE_RETRYING = "CHARGE_RETRYING"
    CHARGED = "CHARGED"
    WRITTEN_OFF = "WRITTEN_OFF"
    HOLD_RELEASED = "HOLD_RELEASED"
    REFUNDED = "REFUNDED"


_LETTER: dict[CaseState, AppealEventKind] = {
    CaseState.NEEDS_CUSTOMER: AppealEventKind.NEEDS_CUSTOMER,
    CaseState.RESEARCHING: AppealEventKind.RESEARCH_RESUMED,
    CaseState.DRAFTING: AppealEventKind.RESEARCH_VERIFIED,
    CaseState.AWAITING_ADOPTION: AppealEventKind.DRAFT_READY,
    CaseState.FROZEN: AppealEventKind.PACKET_FROZEN,
    CaseState.SUBMITTING: AppealEventKind.MAIL_SUBMITTED,
    CaseState.IN_TRANSIT: AppealEventKind.MAILED,
    CaseState.DELIVERED: AppealEventKind.DELIVERED,
    CaseState.RETURNED: AppealEventKind.RETURNED,
    CaseState.CLOSED_RETURNED: AppealEventKind.RETURN_CLOSED,
    CaseState.STOPPED_NO_CHARGE: AppealEventKind.STOPPED,
}

_PAYMENT: dict[PaymentState, AppealEventKind] = {
    PaymentState.CAPTURE_PENDING: AppealEventKind.CHARGE_REQUESTED,
    PaymentState.CAPTURE_RETRYING: AppealEventKind.CHARGE_RETRYING,
    PaymentState.CAPTURED: AppealEventKind.CHARGED,
    PaymentState.WRITTEN_OFF: AppealEventKind.WRITTEN_OFF,
    PaymentState.RELEASED: AppealEventKind.HOLD_RELEASED,
    PaymentState.REFUNDED: AppealEventKind.REFUNDED,
}


@dataclass(frozen=True)
class AppealEvent:
    """One milestone, still inside the restricted boundary (it keeps the exact time)."""

    kind: AppealEventKind
    occurred_at: datetime
    stop_family: StopFamily | None = None


def _stop_family(event_json: str) -> StopFamily | None:
    try:
        reason = json.loads(event_json).get("reason")
    except (ValueError, AttributeError):
        return None
    if isinstance(reason, str) and reason in StopReason.__members__:
        return stop_family(StopReason(reason))
    return None


def appeal_events(
    authorized_at: datetime, history: Iterable[TransitionRecord]
) -> tuple[AppealEvent, ...]:
    """Milestones in recorded order; the card hold opens every case's log."""
    events = [AppealEvent(AppealEventKind.CARD_AUTHORIZED, authorized_at)]
    for record in history:
        if record.machine == "letter":
            kind = _LETTER.get(CaseState(record.target))
        elif record.machine == "payment":
            kind = _PAYMENT.get(PaymentState(record.target))
        else:
            raise ValueError("unknown history machine")
        if kind is None:
            raise ValueError("history target has no milestone")
        family = _stop_family(record.event_json) if kind is AppealEventKind.STOPPED else None
        events.append(AppealEvent(kind, record.occurred_at, family))
    return tuple(events)


class AppealOutcome(StrEnum):
    OPEN = "OPEN"
    CHARGED = "CHARGED"
    REFUNDED = "REFUNDED"
    RELEASED_NO_CHARGE = "RELEASED_NO_CHARGE"
    WRITTEN_OFF = "WRITTEN_OFF"


@dataclass(frozen=True)
class AppealTelemetry:
    """Personal-data-free summary of one appeal: categories and whole hours only."""

    outcome: AppealOutcome
    stop_family: StopFamily | None
    mail_attempts: int
    returned: bool
    hours_to_mailed: int | None
    hours_to_charged: int | None


def _hours(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() // 3600))


def appeal_telemetry(events: tuple[AppealEvent, ...]) -> AppealTelemetry:
    if not events or events[0].kind is not AppealEventKind.CARD_AUTHORIZED:
        raise ValueError("appeal log must open with the card hold")
    start = events[0].occurred_at
    kinds = [event.kind for event in events]
    first: dict[AppealEventKind, datetime] = {}
    for event in events:
        first.setdefault(event.kind, event.occurred_at)
    if AppealEventKind.REFUNDED in kinds:
        outcome = AppealOutcome.REFUNDED
    elif AppealEventKind.CHARGED in kinds:
        outcome = AppealOutcome.CHARGED
    elif AppealEventKind.WRITTEN_OFF in kinds:
        outcome = AppealOutcome.WRITTEN_OFF
    elif AppealEventKind.HOLD_RELEASED in kinds:
        outcome = AppealOutcome.RELEASED_NO_CHARGE
    else:
        outcome = AppealOutcome.OPEN
    stops = [event.stop_family for event in events if event.kind is AppealEventKind.STOPPED]
    mailed = first.get(AppealEventKind.MAILED)
    charged = first.get(AppealEventKind.CHARGED)
    return AppealTelemetry(
        outcome=outcome,
        stop_family=stops[-1] if stops else None,
        mail_attempts=kinds.count(AppealEventKind.MAIL_SUBMITTED),
        returned=AppealEventKind.RETURNED in kinds,
        hours_to_mailed=None if mailed is None else _hours(start, mailed),
        hours_to_charged=None if charged is None else _hours(start, charged),
    )
