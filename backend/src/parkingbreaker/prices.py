"""The prices, in one place (owner decision 2026-10-30: a three-way price test).

Each customer is assigned one of three certified-mail prices and keeps it on every appeal.
Payment validators require the case's recorded amount exactly and membership in the allowed
set. CERTIFIED_CENTS is the default price where no customer is known (tests, previews).
"""

import hashlib

# Owner decision 2026-10-30: $19.99, $24.99 and $29.99, from the first customer.
PRICE_ARMS: dict[str, int] = {"P1999": 1999, "P2499": 2499, "P2999": 2999}
ALLOWED_PRICE_CENTS: frozenset[int] = frozenset(PRICE_ARMS.values())
CERTIFIED_CENTS = PRICE_ARMS["P2999"]
STANDARD_CENTS = 2500
PRICE_EXPERIMENT_ID = "t56-price-test-2026-10-30"
_ASSIGNMENT_SALT = PRICE_EXPERIMENT_ID


def assigned_price(customer_id: str) -> int:
    """The customer's price: an even three-way split, the same for every appeal they file."""
    if not isinstance(customer_id, str) or not customer_id.strip():
        raise ValueError("customer id required")
    digest = hashlib.sha256(f"{_ASSIGNMENT_SALT}:{customer_id}".encode()).digest()
    arms = sorted(PRICE_ARMS.values())
    return arms[int.from_bytes(digest[:8], "big") % len(arms)]


def price_arm(amount_cents: int) -> str:
    """The arm label recorded with a case's amount."""
    for arm, cents in PRICE_ARMS.items():
        if cents == amount_cents:
            return arm
    raise ValueError("not a price-test amount")
