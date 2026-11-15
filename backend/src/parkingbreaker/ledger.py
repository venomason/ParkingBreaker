"""Per-appeal money ledger: sale, refund, dispute and cost lines from recorded facts. Pure.

Inputs are the case's authorized amount, its event log (oplog.appeal_events), the
recorded refund amount and recorded dispute facts. A line whose amount is not yet backed
by billed evidence (processor fees, Lob mailing cost, dispute fees) stays PENDING with no
amount: it is never estimated or treated as zero. Revenue counts at the charge, never at
the card hold. The ledger reports; it never decides a transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from parkingbreaker.disputes import FUNDS_HELD, DisputeFact, DisputeStatus, latest_disputes
from parkingbreaker.oplog.appeal_events import AppealEvent, AppealEventKind


class LineKind(StrEnum):
    SALE = "SALE"
    REFUND = "REFUND"
    PROCESSOR_FEE = "PROCESSOR_FEE"
    MAIL_COST = "MAIL_COST"
    DISPUTE_FUNDS_HELD = "DISPUTE_FUNDS_HELD"
    DISPUTE_LOST = "DISPUTE_LOST"
    DISPUTE_FEE = "DISPUTE_FEE"
    DISPUTE_OUTCOME = "DISPUTE_OUTCOME"


class LineStatus(StrEnum):
    RECORDED = "RECORDED"
    PENDING = "PENDING"


@dataclass(frozen=True)
class LedgerLine:
    kind: LineKind
    status: LineStatus
    amount_cents: int | None  # signed: positive money in, negative money out


@dataclass(frozen=True)
class AppealLedger:
    lines: tuple[LedgerLine, ...]
    fraud_warning: bool

    @property
    def known_net_cents(self) -> int:
        return sum(line.amount_cents or 0 for line in self.lines)

    @property
    def complete(self) -> bool:
        return all(line.status is LineStatus.RECORDED for line in self.lines)


def _recorded(kind: LineKind, cents: int) -> LedgerLine:
    return LedgerLine(kind, LineStatus.RECORDED, cents)


def _pending(kind: LineKind) -> LedgerLine:
    return LedgerLine(kind, LineStatus.PENDING, None)


def appeal_ledger(
    amount_cents: int,
    events: tuple[AppealEvent, ...],
    refund_cents: int | None,
    disputes: tuple[DisputeFact, ...] = (),
) -> AppealLedger:
    if type(amount_cents) is not int or amount_cents <= 0:
        raise ValueError("amount must be positive cents")
    if refund_cents is not None and (
        type(refund_cents) is not int or not 0 < refund_cents <= amount_cents
    ):
        raise ValueError("refund must be within the charged amount")
    kinds = [event.kind for event in events]
    lines: list[LedgerLine] = []
    charged = AppealEventKind.CHARGED in kinds
    if charged:
        lines += [_recorded(LineKind.SALE, amount_cents), _pending(LineKind.PROCESSOR_FEE)]
    lines += [_pending(LineKind.MAIL_COST)] * kinds.count(AppealEventKind.MAILED)
    if AppealEventKind.REFUNDED in kinds:
        if not charged:
            raise ValueError("a refund requires a recorded charge")
        lines.append(
            _pending(LineKind.REFUND)
            if refund_cents is None
            else _recorded(LineKind.REFUND, -refund_cents)
        )
    latest = latest_disputes(disputes)
    if latest and not charged:
        raise ValueError("a dispute requires a recorded charge")
    for fact in latest.values():
        if fact is None:
            lines += [_pending(LineKind.DISPUTE_OUTCOME), _pending(LineKind.DISPUTE_FEE)]
            continue
        assert fact.amount_cents is not None
        assert fact.status is not None
        if fact.status in FUNDS_HELD:
            lines.append(_recorded(LineKind.DISPUTE_FUNDS_HELD, -fact.amount_cents))
        elif fact.status is DisputeStatus.LOST:
            lines.append(_recorded(LineKind.DISPUTE_LOST, -fact.amount_cents))
        if fact.status in FUNDS_HELD | {DisputeStatus.LOST, DisputeStatus.WON}:
            lines.append(_pending(LineKind.DISPUTE_FEE))
    return AppealLedger(
        tuple(lines), fraud_warning=any(fact.kind == "fraud_warning" for fact in disputes)
    )
