"""The owner's fixed business rules, read from the code into one fingerprinted record. Pure.

These are the rules no optimizer, experiment or AI worker may change (the plan's safety
wall): the certified price test (three prices, an even per-customer split), certified-only
mail, the ticket age cutoff, nationwide intake with only the recorded state holds, and
charging only on the existing T17 path.
The price, mail class, age cutoff and held states are read from the live constants. The
charge trigger is a recorded label only: the capture path itself is guarded by the T17
capture tests, not by this fingerprint. The fingerprint is pinned below. Changing any of
these rules changes the fingerprint and fails the policy test until the new value is
recorded with the owner's decision.
Optimizers may read the record; nothing here can set it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from parkingbreaker.prices import PRICE_ARMS
from parkingbreaker.service_policy import (
    LAUNCH_STANDARD_OFFERED,
    MAX_TICKET_AGE_DAYS,
    STATE_BASIS,
)

_STATE_CODES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV "
    "NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY"
)
US_STATES_AND_DC = frozenset(_STATE_CODES.split())


@dataclass(frozen=True)
class PolicyRecord:
    prices_cents: tuple[int, ...]
    price_split: str
    certified_only: bool
    max_ticket_age_days: int
    held_states: tuple[str, ...]
    nationwide_intake: bool
    charge_trigger: str


def current_policy() -> PolicyRecord:
    return PolicyRecord(
        prices_cents=tuple(sorted(PRICE_ARMS.values())),
        price_split="equal_thirds_per_customer",
        certified_only=not LAUNCH_STANDARD_OFFERED,
        max_ticket_age_days=MAX_TICKET_AGE_DAYS,
        held_states=tuple(sorted(US_STATES_AND_DC - set(STATE_BASIS))),
        # Production composes no launch allowlist (config.app_from_environment passes None).
        nationwide_intake=True,
        charge_trigger="first_usps_scan_or_t17_expiry_fallback",
    )


def policy_fingerprint(policy: PolicyRecord) -> str:
    canonical = json.dumps(asdict(policy), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# Owner decisions 2026-10-27/28/30: a $19.99/$24.99/$29.99 certified price test split evenly
# per customer (2026-10-30, replacing $29 flat), 10-day cutoff, AZ and NV held, nationwide
# intake, charge on the first USPS scan (T17). Update only with a new decision.
RECORDED_POLICY_FINGERPRINT = "ea046d0056500572c94d18a143dd7b67d16288bbe9074ebf4da3ed353606afb5"
