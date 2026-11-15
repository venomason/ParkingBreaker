"""Three-price test report: what each price earned per customer offered. Pure.

One row per price: customers offered that price, card holds, charged orders, recorded
revenue and refunds, known net from the per-appeal ledgers, and known net per customer
offered. Money that is not yet billed (processor fees, mailing cost, dispute fees) stays
out of the known net and is counted as pending lines, never estimated. Visitors who left
before intake are not counted until ad click capture exists (T52), so "per customer offered"
is the honest denominator for now.

The report never declares a winner. It only says whether each price has reached the
minimum number of charged orders; the owner alone picks a price (owner decision 2026-10-30).
The minimum is the learned setting price_test_min_orders (settings_registry). The default
is that setting's proposed start; no caller passes the stored current value yet. No customer
identifiers appear in the output.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from parkingbreaker.ledger import AppealLedger, LineKind, LineStatus
from parkingbreaker.mail_config import EXPECTED_MAILING_COST, MailClass
from parkingbreaker.prices import PRICE_ARMS, price_arm
from parkingbreaker.settings_registry import SETTINGS

MIN_CHARGED_ORDERS_PER_PRICE = SETTINGS["price_test_min_orders"].start
STRIPE_PERCENT_BPS = 290  # 2.90%
STRIPE_FIXED_CENTS = 30  # $0.30


def estimate_order_variable_cost_cents(
    price_cents: int,
    mail_class: MailClass = MailClass.CERTIFIED,
) -> int:
    """Computes verified baseline variable COGS for a single charged & mailed order:
    Lob Certified Mail baseline (822c) + Stripe processing (2.9% + 30c).
    """
    stripe_fee_cents = (price_cents * STRIPE_PERCENT_BPS + 5000) // 10000 + STRIPE_FIXED_CENTS
    return EXPECTED_MAILING_COST[mail_class] + stripe_fee_cents


@dataclass(frozen=True)
class OfferedAppeal:
    """One offer: its price, and its ledger once a card hold exists (None before a hold)."""

    amount_cents: int
    ledger: AppealLedger | None


@dataclass(frozen=True)
class PriceRow:
    arm: str
    amount_cents: int
    offered: int
    held: int
    charged: int
    revenue_cents: int
    refunded_cents: int
    known_net_cents: int
    pending_lines: int
    known_net_per_offered_cents: int | None
    minimum_orders_reached: bool
    estimated_contribution_cents: int = 0
    estimated_contribution_per_offered_cents: int | None = None


@dataclass(frozen=True)
class PriceReport:
    rows: tuple[PriceRow, ...]
    minimum_charged_orders: int
    winner: None = None  # never chosen here; the owner decides


def price_report(
    appeals: Iterable[OfferedAppeal], minimum: int = MIN_CHARGED_ORDERS_PER_PRICE
) -> PriceReport:
    if type(minimum) is not int or minimum < 1:
        raise ValueError("minimum orders must be a positive integer")
    totals = {
        cents: {"offered": 0, "held": 0, "charged": 0, "revenue": 0, "refunded": 0,
                "net": 0, "pending": 0}
        for cents in PRICE_ARMS.values()
    }  # fmt: skip
    for appeal in appeals:
        if appeal.amount_cents not in totals:
            raise ValueError("offer is not a price-test amount")
        row = totals[appeal.amount_cents]
        row["offered"] += 1
        if appeal.ledger is None:
            continue
        row["held"] += 1
        lines = appeal.ledger.lines
        recorded = [line for line in lines if line.status is LineStatus.RECORDED]
        sales = [line for line in recorded if line.kind is LineKind.SALE]
        if sales:
            row["charged"] += 1
        row["revenue"] += sum(line.amount_cents or 0 for line in sales)
        row["refunded"] -= sum(
            line.amount_cents or 0 for line in recorded if line.kind is LineKind.REFUND
        )
        row["net"] += appeal.ledger.known_net_cents
        row["pending"] += sum(line.status is LineStatus.PENDING for line in lines)
    rows = tuple(
        PriceRow(
            price_arm(cents),
            cents,
            t["offered"],
            t["held"],
            t["charged"],
            t["revenue"],
            t["refunded"],
            t["net"],
            t["pending"],
            t["net"] // t["offered"] if t["offered"] else None,
            t["charged"] >= minimum,
            estimated_contribution_cents=(
                t["net"] - (t["charged"] * estimate_order_variable_cost_cents(cents))
            ),
            estimated_contribution_per_offered_cents=(
                (t["net"] - (t["charged"] * estimate_order_variable_cost_cents(cents)))
                // t["offered"]
                if t["offered"]
                else None
            ),
        )
        for cents, t in sorted(totals.items())
    )
    return PriceReport(rows, minimum)
