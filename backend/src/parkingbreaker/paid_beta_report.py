"""Read the paid cohort's recorded money and outstanding work from one SQLite snapshot."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from parkingbreaker.case_state import CaseState
from parkingbreaker.payment_state import PaymentState

_OPEN_HOLD = {
    PaymentState.AUTHORIZED,
    PaymentState.CAPTURE_PENDING,
    PaymentState.CAPTURE_RETRYING,
}
_CAPTURED = {PaymentState.CAPTURED, PaymentState.REFUNDED}
_CASES = """
    WITH pending AS (
        SELECT case_token,
               MAX(effect IN ('CAPTURE_PAYMENT', 'RELEASE_HOLD',
                              'REFUND_FULL', 'REFUND_RETURNED_MAIL')) AS payment,
               MAX(effect IN ('SUBMIT_LETTER', 'SUBMIT_REMAIL')) AS mailing
          FROM pb_pending_effects
         WHERE outcome IS NULL AND case_token IS NOT NULL
         GROUP BY case_token
    )
    SELECT c.authorized_at, c.authorization_expires_at, c.amount_cents,
           c.payment_state, c.letter_state,
           (SELECT COUNT(*) FROM pb_case_history h WHERE h.case_token = c.case_token
            AND h.machine = 'payment' AND h.target = 'CAPTURED'
            AND h.event_name IN ('CaptureSucceeded', 'UnexpectedCaptureConfirmed')) AS captures,
           (SELECT COUNT(*) FROM pb_case_history h WHERE h.case_token = c.case_token
            AND h.machine = 'payment' AND h.target = 'REFUNDED'
            AND h.event_name = 'RefundIssued') AS refunds,
           (SELECT h.event_json FROM pb_case_history h WHERE h.case_token = c.case_token
            AND h.machine = 'payment' AND h.target = 'REFUNDED' AND h.event_name = 'RefundIssued'
            ORDER BY h.revision DESC LIMIT 1) AS refund_record,
           EXISTS (SELECT 1 FROM pb_case_history h WHERE h.case_token = c.case_token
                   AND h.machine = 'payment' AND h.event_name = 'ReconciliationExhausted')
                   AS unknown_capture_outcome,
           COALESCE(p.payment, 0) AS pending_payment,
           COALESCE(p.mailing, 0) AS pending_mailing
      FROM pb_cases c
      LEFT JOIN pending p USING (case_token)
     WHERE c.amount_cents > 0
"""


def _utc(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return result.astimezone(UTC)


def _confirmed_refund(payload: str, capture_cents: int) -> int | None:
    try:
        record = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    refunded = record.get("amount_cents")
    recorded_capture = record.get("captured_cents")
    if (
        type(refunded) is int
        and type(recorded_capture) is int
        and recorded_capture == capture_cents
        and 0 < refunded <= capture_cents
    ):
        return refunded
    return None


def paid_beta_report(database: Path, *, since: datetime, now: datetime) -> dict[str, object]:
    """Group paid cases by hold start. Include older unresolved pre-case attempts separately."""
    if since.utcoffset() is None or now.utcoffset() is None:
        raise ValueError("invalid paid cohort window")
    since, now = since.astimezone(UTC), now.astimezone(UTC)
    if since > now:
        raise ValueError("invalid paid cohort window")
    totals: Counter[str] = Counter()
    payment_states: Counter[str] = Counter()
    letter_states: Counter[str] = Counter()
    with closing(
        sqlite3.connect(database.resolve(strict=True).as_uri() + "?mode=ro", uri=True)
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        for row in db.execute(_CASES):
            if not since <= _utc(row["authorized_at"]) <= now:
                continue
            payment = PaymentState(row["payment_state"])
            letter = CaseState(row["letter_state"])
            payment_states[payment.value] += 1
            letter_states[letter.value] += 1
            if payment in _OPEN_HOLD:
                expires = _utc(row["authorization_expires_at"])
                totals["expired_holds"] += int(expires <= now)
                totals["holds_expiring_within_24h"] += int(now < expires <= now + timedelta(days=1))
            totals["cases_with_pending_payment_work"] += row["pending_payment"]
            totals["cases_with_pending_mailing_work"] += row["pending_mailing"]
            if payment in _CAPTURED and row["captures"] == 1:
                totals["recorded_gross_capture_cents"] += row["amount_cents"]
            elif payment in _CAPTURED or row["captures"] or row["unknown_capture_outcome"]:
                totals["capture_outcome_unknown"] += 1
            if payment is PaymentState.REFUNDED:
                cents = (
                    _confirmed_refund(row["refund_record"], row["amount_cents"])
                    if row["refunds"] == 1
                    else None
                )
                if cents is None:
                    totals["refund_amount_unknown"] += 1
                else:
                    totals["recorded_refund_cents"] += cents
            elif row["refunds"]:
                totals["refund_amount_unknown"] += 1
        unresolved = dict(
            db.execute(
                "SELECT state, COUNT(*) FROM pb_intake_authorization_attempts "
                "WHERE state IN ('pending', 'uncertain') GROUP BY state"
            ).fetchall()
        )

    return {
        "scope": "recorded SQLite state",
        "cohort_authorized_since": since.isoformat(),
        "observed_at": now.isoformat(),
        "paid_cases": payment_states.total(),
        "payment_states": dict(sorted(payment_states.items())),
        "letter_states": dict(sorted(letter_states.items())),
        "outstanding_pre_case_attempts_all_cohorts": {
            state: unresolved.get(state, 0) for state in ("pending", "uncertain")
        },
        **{
            name: totals[name]
            for name in (
                "recorded_gross_capture_cents",
                "recorded_refund_cents",
                "capture_outcome_unknown",
                "refund_amount_unknown",
                "expired_holds",
                "holds_expiring_within_24h",
                "cases_with_pending_payment_work",
                "cases_with_pending_mailing_work",
            )
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--since", required=True, help="cohort hold-start boundary with UTC offset")
    args = parser.parse_args(argv)
    try:
        report = paid_beta_report(args.database, since=_utc(args.since), now=datetime.now(UTC))
    except (OSError, ValueError, sqlite3.Error):
        print(
            json.dumps({"status": "unavailable", "reason": "window_or_transaction_store_invalid"})
        )
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
