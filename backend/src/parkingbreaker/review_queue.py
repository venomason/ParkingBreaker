"""Owner review queue: exceptions to look at, derived from recorded facts. Pure.

This is not a gate. Automated case processing never waits on it (T24: no routine
operator queue replaces automated processing). Items are derived from facts the code
already recorded (dispute and fraud-warning webhooks, written-off charges), so an item can
never be lost or invented; only the owner's resolution is stored. Item ids are
deterministic, so a replayed fact maps to the same item. Nothing resolves itself:
owner-away auto-resolution limits are not set, so every item waits for the owner.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from parkingbreaker.disputes import DisputeFact, DisputeStatus, latest_disputes
from parkingbreaker.oplog.appeal_events import AppealEvent, AppealEventKind

OWNER = "owner"


class ReviewKind(StrEnum):
    DISPUTE = "DISPUTE"
    FRAUD_WARNING = "FRAUD_WARNING"
    CAPTURE_WRITTEN_OFF = "CAPTURE_WRITTEN_OFF"
    # A learned-settings step toward risk or spend (T49); not tied to one case.
    SETTING_CHANGE = "SETTING_CHANGE"


class Resolution(StrEnum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    HANDLED_OUTSIDE_SYSTEM = "HANDLED_OUTSIDE_SYSTEM"
    DISMISSED = "DISMISSED"


class ReviewAuthority(StrEnum):
    INFORMATIONAL = "INFORMATIONAL"
    SETTING_REQUEST = "SETTING_REQUEST"


@dataclass(frozen=True)
class ReviewItem:
    item_id: str
    kind: ReviewKind
    case_token: str
    dispute_status: DisputeStatus | None = None
    # Hours left before the filing deadline of an unmailed order this item affects;
    # None when no unmailed order is affected (every current kind is post-charge).
    deadline_margin_hours: int | None = None
    expires_at: datetime | None = None

    @property
    def authority(self) -> ReviewAuthority:
        """Queue entries never grant permission to act."""
        return (
            ReviewAuthority.SETTING_REQUEST
            if self.kind is ReviewKind.SETTING_CHANGE
            else ReviewAuthority.INFORMATIONAL
        )


def review_items(
    case_token: str, events: Iterable[AppealEvent], facts: Iterable[DisputeFact]
) -> tuple[ReviewItem, ...]:
    """Every exception one case has produced; a dispute appears once, at its latest status."""
    facts = tuple(facts)
    items: dict[str, ReviewItem] = {}
    for ref, fact in latest_disputes(facts).items():
        item_id = f"dispute:{ref}"
        items[item_id] = ReviewItem(
            item_id, ReviewKind.DISPUTE, case_token, fact.status if fact is not None else None
        )
    for fact in facts:
        if fact.kind == "fraud_warning":
            item_id = f"fraud_warning:{fact.object_ref}"
            items[item_id] = ReviewItem(item_id, ReviewKind.FRAUD_WARNING, case_token)
    if any(event.kind is AppealEventKind.WRITTEN_OFF for event in events):
        item_id = f"written_off:{case_token}"
        items[item_id] = ReviewItem(item_id, ReviewKind.CAPTURE_WRITTEN_OFF, case_token)
    return tuple(items[key] for key in sorted(items))


def can_resolve(actor: str) -> bool:
    """Decision rights: only the owner resolves review items for now."""
    return actor == OWNER
