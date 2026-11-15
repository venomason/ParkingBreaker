"""T56 report: per-price results from ledgers, with no automatic winner."""

from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from parkingbreaker.ledger import appeal_ledger
from parkingbreaker.oplog.appeal_events import AppealEvent, AppealEventKind
from parkingbreaker.price_report import OfferedAppeal, price_report

T0 = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)


def _charged(cents: int, refund: int | None = None) -> OfferedAppeal:
    kinds = [AppealEventKind.CARD_AUTHORIZED, AppealEventKind.MAILED, AppealEventKind.CHARGED]
    if refund is not None:
        kinds.append(AppealEventKind.REFUNDED)
    events = tuple(AppealEvent(kind, T0) for kind in kinds)
    return OfferedAppeal(cents, appeal_ledger(cents, events, refund))


def _released(cents: int) -> OfferedAppeal:
    events = (AppealEvent(AppealEventKind.CARD_AUTHORIZED, T0),)
    return OfferedAppeal(cents, appeal_ledger(cents, events, None))


def test_rows_follow_ledgers_and_never_name_a_winner() -> None:
    appeals = [
        _charged(1999),
        _charged(1999),
        _released(1999),
        OfferedAppeal(1999, None),
        _charged(2999, refund=999),
        OfferedAppeal(2999, None),
    ]
    report = price_report(appeals, minimum=2)
    rows = {row.amount_cents: row for row in report.rows}
    low, mid, high = rows[1999], rows[2499], rows[2999]
    assert (low.offered, low.held, low.charged, low.revenue_cents) == (4, 3, 2, 3998)
    assert (low.known_net_cents, low.known_net_per_offered_cents) == (3998, 999)
    assert low.minimum_orders_reached is True
    assert (mid.offered, mid.charged, mid.known_net_per_offered_cents) == (0, 0, None)
    assert (high.charged, high.refunded_cents, high.known_net_cents) == (1, 999, 2000)
    assert high.minimum_orders_reached is False
    assert low.pending_lines == 4  # fee and mailing cost per charged order, never estimated
    assert report.winner is None


def test_only_price_test_amounts_and_a_positive_minimum() -> None:
    with pytest.raises(ValueError, match="price-test amount"):
        price_report([OfferedAppeal(2900, None)])
    with pytest.raises(ValueError, match="positive"):
        price_report([], minimum=0)
    report = price_report([])
    assert [row.arm for row in report.rows] == ["P1999", "P2499", "P2999"]
    assert "customer" not in repr([asdict(row) for row in report.rows])
