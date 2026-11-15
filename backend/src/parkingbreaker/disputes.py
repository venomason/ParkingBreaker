"""Card disputes and early fraud warnings, reduced to validated facts. Pure: no I/O.

Stripe sends ``charge.dispute.*`` and ``radar.early_fraud_warning.*`` webhooks for a charge
we captured. They are bookkeeping and review signals only: Stripe moves the disputed funds
itself, so recording a fact never captures, releases, refunds or changes a case state. The
fact keeps the original PaymentIntent (to bind the case), the Stripe event time and closed
categories; dispute evidence, customer text and card details are never kept. Reasons and
fraud types come from Stripe's published enums (docs.stripe.com API reference for the
Dispute and Early Fraud Warning objects, read 2026-10-29); anything else is "unknown".
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from parkingbreaker.stripe_payment import _reference

DISPUTE_EVENTS = frozenset(
    {
        "charge.dispute.created",
        "charge.dispute.updated",
        "charge.dispute.closed",
        "charge.dispute.funds_withdrawn",
        "charge.dispute.funds_reinstated",
    }
)
FRAUD_WARNING_EVENTS = frozenset(
    {"radar.early_fraud_warning.created", "radar.early_fraud_warning.updated"}
)
_DISPUTE_ID = re.compile(r"(?:du|dp)_[A-Za-z0-9]+")
DISPUTE_REASONS = frozenset(
    {
        "bank_cannot_process",
        "check_returned",
        "credit_not_processed",
        "customer_initiated",
        "debit_not_authorized",
        "duplicate",
        "fraudulent",
        "general",
        "incorrect_account_details",
        "insufficient_funds",
        "noncompliant",
        "product_not_received",
        "product_unacceptable",
        "subscription_canceled",
        "unrecognized",
    }
)
FRAUD_TYPES = frozenset(
    {
        "card_never_received",
        "fraudulent_card_application",
        "made_with_counterfeit_card",
        "made_with_lost_card",
        "made_with_stolen_card",
        "misc",
        "unauthorized_use_of_card",
    }
)


class DisputeStatus(StrEnum):
    WARNING_NEEDS_RESPONSE = "warning_needs_response"
    WARNING_UNDER_REVIEW = "warning_under_review"
    WARNING_CLOSED = "warning_closed"
    NEEDS_RESPONSE = "needs_response"
    UNDER_REVIEW = "under_review"
    WON = "won"
    LOST = "lost"
    PREVENTED = "prevented"


# Inquiries (warning_*) withdraw no funds; formal disputes do until won.
FUNDS_HELD = frozenset({DisputeStatus.NEEDS_RESPONSE, DisputeStatus.UNDER_REVIEW})
# A closed outcome outranks an open status recorded in the same second.
FINAL_STATUSES = frozenset(
    {DisputeStatus.WON, DisputeStatus.LOST, DisputeStatus.WARNING_CLOSED, DisputeStatus.PREVENTED}
)


@dataclass(frozen=True)
class DisputeFact:
    kind: str  # "dispute" or "fraud_warning"
    event_type: str
    object_ref: str
    payment_intent: str
    status: DisputeStatus | None
    amount_cents: int | None
    category: str
    # Stripe event time in Unix seconds; None for facts stored before it was recorded.
    event_created: int | None

    def to_json(self) -> str:
        return json.dumps(
            {
                "kind": self.kind,
                "event": self.event_type,
                "object_ref": self.object_ref,
                "payment_intent": self.payment_intent,
                "status": None if self.status is None else str(self.status),
                "amount_cents": self.amount_cents,
                "category": self.category,
                "event_created": self.event_created,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> DisputeFact:
        data = json.loads(text)
        if isinstance(data, dict) and "event_created" not in data:
            data["event_created"] = None  # recorded before event times were kept
        if not isinstance(data, dict) or set(data) != {
            "kind",
            "event",
            "object_ref",
            "payment_intent",
            "status",
            "amount_cents",
            "category",
            "event_created",
        }:
            raise ValueError("dispute fact has unknown or missing fields")
        status = data["status"]
        return _checked(
            cls(
                data["kind"],
                data["event"],
                data["object_ref"],
                data["payment_intent"],
                None if status is None else DisputeStatus(status),
                data["amount_cents"],
                data["category"],
                data["event_created"],
            )
        )


def _category(value: object, allowed: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "unknown"


def _checked(fact: DisputeFact) -> DisputeFact:
    if fact.kind == "dispute":
        if fact.event_type not in DISPUTE_EVENTS or fact.status is None:
            raise ValueError("invalid dispute fact")
        if not isinstance(fact.object_ref, str) or not _DISPUTE_ID.fullmatch(fact.object_ref):
            raise ValueError("invalid dispute id")
        allowed = DISPUTE_REASONS
        if type(fact.amount_cents) is not int or fact.amount_cents <= 0:
            raise ValueError("dispute amount must be positive cents")
    elif fact.kind == "fraud_warning":
        if fact.event_type not in FRAUD_WARNING_EVENTS or fact.status is not None:
            raise ValueError("invalid fraud warning fact")
        _reference(fact.object_ref, "issfr_")
        if fact.amount_cents is not None:
            raise ValueError("fraud warnings carry no amount")
        allowed = FRAUD_TYPES
    else:
        raise ValueError("unknown dispute fact kind")
    _reference(fact.payment_intent, "pi_")
    if fact.category != "unknown" and fact.category not in allowed:
        raise ValueError("invalid dispute category")
    if fact.event_created is not None and (
        type(fact.event_created) is not int or fact.event_created < 0
    ):
        raise ValueError("invalid event time")
    return fact


def dispute_fact(event_type: str, obj: dict[str, object], event_created: object) -> DisputeFact:
    """Reduce one verified Stripe dispute or fraud-warning event; ValueError if malformed."""
    if type(event_created) is not int:
        raise ValueError("a live event must carry its Stripe event time")
    payment_intent = obj.get("payment_intent")
    if isinstance(payment_intent, dict):
        payment_intent = payment_intent.get("id")
    if event_type in DISPUTE_EVENTS:
        if obj.get("object") != "dispute" or obj.get("currency") != "usd":
            raise ValueError("not a USD dispute")
        status = obj.get("status")
        if not isinstance(status, str) or status not in {s.value for s in DisputeStatus}:
            raise ValueError("unknown dispute status")
        return _checked(
            DisputeFact(
                "dispute",
                event_type,
                str(obj.get("id")),
                str(payment_intent),
                DisputeStatus(status),
                obj.get("amount"),  # type: ignore[arg-type]
                _category(obj.get("reason"), DISPUTE_REASONS),
                event_created,
            )
        )
    if event_type in FRAUD_WARNING_EVENTS:
        if obj.get("object") != "radar.early_fraud_warning":
            raise ValueError("not an early fraud warning")
        return _checked(
            DisputeFact(
                "fraud_warning",
                event_type,
                str(obj.get("id")),
                str(payment_intent),
                None,
                None,
                _category(obj.get("fraud_type"), FRAUD_TYPES),
                event_created,
            )
        )
    raise ValueError("not a dispute or fraud-warning event")


def _time(fact: DisputeFact) -> int:
    return -1 if fact.event_created is None else fact.event_created


def latest_disputes(facts: Iterable[DisputeFact]) -> dict[str, DisputeFact | None]:
    """Newest ranked facts per dispute; contradictory equal-rank payloads stay unresolved.

    Event types describe delivery, not authority. Only equivalent substantive payloads
    may use their event type to select a deterministic representative. Source facts are
    retained by the caller; a later Stripe timestamp can supersede an older conflict.
    """
    candidates: dict[str, list[DisputeFact]] = {}
    ranks: dict[str, tuple[int, bool]] = {}
    for fact in facts:
        if fact.kind != "dispute":
            continue
        rank = (_time(fact), fact.status in FINAL_STATUSES)
        current_rank = ranks.get(fact.object_ref)
        if current_rank is None or rank > current_rank:
            ranks[fact.object_ref] = rank
            candidates[fact.object_ref] = [fact]
        elif rank == current_rank:
            candidates[fact.object_ref].append(fact)
    latest: dict[str, DisputeFact | None] = {}
    for ref in sorted(candidates):
        group = candidates[ref]
        payloads = {
            (fact.payment_intent, fact.status, fact.amount_cents, fact.category) for fact in group
        }
        latest[ref] = min(group, key=lambda fact: fact.event_type) if len(payloads) == 1 else None
    return latest
