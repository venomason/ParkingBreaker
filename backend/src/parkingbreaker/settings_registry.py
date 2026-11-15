"""Learned settings, each on a ladder of allowed values that moves one step at a time. Pure.

Every setting an optimizer or AI worker may tune lives here: a fixed ladder of allowed
values, a current value and the direction that is safer. A change moves exactly one rung
and only after the setting's minimum hold time. A step toward safety or lower spend is
applied at once (a hold never delays tightening); a step toward risk or more spend must
wait out the setting's minimum hold time and then approval in the owner review queue
(any spend increase needs the owner's explicit yes). Every change is recorded with
its old and new value, proposer, approver and evidence by the caller's store.

The owner's fixed rules (policy_record) are not settings and nothing here can move them.
The research reuse window's ladder tops out at the 24-hour owner rule, so the registry can
only shorten it. The ladders and starting values below are proposed (2026-10-31) and
pending board review; only the ladder shape is enforced here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from parkingbreaker.policy_record import PolicyRecord


class Safer(StrEnum):
    LOWER = "LOWER"  # a smaller value is safer or spends less
    HIGHER = "HIGHER"  # a larger value is safer


class Verdict(StrEnum):
    APPLY = "APPLY"  # one step toward safety: applied without anyone's approval
    NEEDS_OWNER = "NEEDS_OWNER"  # one step toward risk or spend: queued for the owner
    REFUSED = "REFUSED"


@dataclass(frozen=True)
class Setting:
    name: str
    unit: str
    ladder: tuple[int, ...]
    safer: Safer
    start: int
    min_hold: timedelta

    def __post_init__(self) -> None:
        if len(self.ladder) < 2 or list(self.ladder) != sorted(set(self.ladder)):
            raise ValueError(f"{self.name}: the ladder must be strictly increasing")
        if self.start not in self.ladder or self.min_hold <= timedelta(0):
            raise ValueError(f"{self.name}: bad start or hold time")


_DAY = timedelta(days=1)
# Proposed 2026-10-31, pending board review. Spend and ad-guard settings start at their
# safest rung, so nothing is loosened before the board and owner have looked.
SETTINGS: dict[str, Setting] = {
    s.name: s
    for s in (
        # Never above the owner's 24-hour reuse rule, so the registry can only shorten it.
        Setting(
            "research_reuse_hours", "hours", (0, 1, 2, 4, 8, 12, 24), Safer.LOWER, 24, 7 * _DAY
        ),
        # Business days added to the mail timing upper bounds when checking a deadline. It
        # starts at 0, today's behavior, not its safest rung; the board sets the real start.
        Setting("postal_buffer_days", "business days", (0, 1, 2, 3, 5), Safer.HIGHER, 0, 7 * _DAY),
        Setting(
            "photo_reading_confidence_pct",
            "percent",
            (80, 85, 90, 95, 99),
            Safer.HIGHER,
            95,
            7 * _DAY,
        ),
        Setting(
            "ad_daily_cap_cents",
            "cents per day",
            (0, 500, 1000, 2000, 5000, 10000),
            Safer.LOWER,
            0,
            3 * _DAY,
        ),
        Setting("ad_click_minimum", "clicks", (50, 100, 200, 400), Safer.HIGHER, 400, 3 * _DAY),
        Setting("ad_cooldown_hours", "hours", (24, 48, 72, 168), Safer.HIGHER, 168, 3 * _DAY),
        Setting(
            "ad_stop_loss_cents",
            "cents per day",
            (0, 500, 1000, 2500, 5000),
            Safer.LOWER,
            0,
            3 * _DAY,
        ),
        Setting(
            "ai_cost_limit_cents",
            "cents per appeal",
            (25, 50, 100, 200),
            Safer.LOWER,
            25,
            7 * _DAY,
        ),
        Setting(
            "price_test_min_orders",
            "charged orders per price",
            (10, 20, 30, 50, 100),
            Safer.HIGHER,
            30,
            7 * _DAY,
        ),
    )
}

# The owner's 24-hour research reuse rule (policy decisions in force; ROADMAP.md).
OWNER_REUSE_HOURS = 24


@dataclass(frozen=True)
class Current:
    value: int
    changed_at: datetime | None  # None until the first recorded change


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str


def decide(setting: Setting, current: Current, proposed: int, now: datetime) -> Decision:
    """Whether a proposed move is applied now, needs the owner, or is refused."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("decision time must be timezone-aware")
    if current.changed_at is not None:
        if current.changed_at.tzinfo is None or current.changed_at.utcoffset() is None:
            raise ValueError("current change time must be timezone-aware")
        if now < current.changed_at:
            return Decision(Verdict.REFUSED, "decision time precedes the current change")
    if type(proposed) is not int or proposed not in setting.ladder:
        return Decision(Verdict.REFUSED, "not a rung of this setting's ladder")
    if current.value not in setting.ladder:
        return Decision(Verdict.REFUSED, "current value is off the ladder")
    step = setting.ladder.index(proposed) - setting.ladder.index(current.value)
    if abs(step) != 1:
        return Decision(Verdict.REFUSED, "a change moves exactly one step")
    safer = (step < 0) == (setting.safer is Safer.LOWER)
    if safer:
        # Tightening never waits: a bad loosening can be walked back at once.
        return Decision(Verdict.APPLY, "one step toward safety")
    if current.changed_at is not None and now - current.changed_at < setting.min_hold:
        return Decision(Verdict.REFUSED, "the minimum hold time has not passed")
    return Decision(Verdict.NEEDS_OWNER, "one step toward risk or spend needs the owner")


def outside_fixed_rules(policy: PolicyRecord) -> bool:
    """True when no setting can move a fixed owner rule (checked by the tests)."""
    fixed = set(vars(policy))
    reuse = SETTINGS["research_reuse_hours"]
    return not (fixed & set(SETTINGS)) and max(reuse.ladder) == OWNER_REUSE_HOURS
