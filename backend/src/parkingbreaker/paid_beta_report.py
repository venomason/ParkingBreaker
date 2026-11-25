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
from parkingbreaker.paid_capture_evidence import (
    CAPTURE_ACCEPTANCE_JOIN_SQL,
    CAPTURE_BASIS_SQL,
    original_mail_facts,
)
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


_FIRST_PAID_REVIEW_SIZE = 20
_ORIGINAL_CAPTURES = """
    SELECT h.case_token, h.occurred_at, c.amount_cents,
           json_extract(h.event_json, '$.charge_ref') AS charge_ref,
           json_extract(s.outcome_json, '$.letter_id') AS letter_id,
           {basis} AS basis,
           (SELECT json_extract(p.result, '$.outcome.livemode')
            FROM pb_payment_operations p
            WHERE p.case_token=h.case_token
              AND json_extract(p.result, '$.outcome.status')='succeeded'
              AND json_extract(p.result, '$.outcome.charge_ref')=
                  json_extract(h.event_json, '$.charge_ref')
              AND p.completed_at=h.occurred_at
            ORDER BY p.rowid LIMIT 1) AS livemode,
           (SELECT f.event_json FROM pb_case_history f
            WHERE f.case_token=h.case_token AND f.machine='payment'
              AND f.event_name='RefundIssued'
            ORDER BY f.revision DESC LIMIT 1) AS refund_record,
           (SELECT json_extract(f.event_json, '$.kind') FROM pb_case_history f
            WHERE f.case_token=h.case_token AND f.machine='payment'
              AND f.event_name='ReturnedMailRefundRequested'
            ORDER BY f.revision DESC LIMIT 1) AS refund_intent
    FROM pb_case_history h
    JOIN pb_cases c ON c.case_token=h.case_token
    LEFT JOIN pb_submission_cursors s ON s.case_token=h.case_token AND s.attempt=1
    {acceptance_join}
    WHERE h.machine='payment' AND h.target='CAPTURED' AND c.amount_cents>0
      AND h.event_name IN ('CaptureSucceeded', 'UnexpectedCaptureConfirmed')
    ORDER BY h.rowid
"""


def _capture_totals(records: list[dict[str, object]]) -> dict[str, object]:
    fallback_captures = sum(r["basis"] == "hold_expiry_fallback" for r in records)
    return {
        "successful_original_captures": len(records),
        "hold_expiry_fallback_captures": fallback_captures,
        "hold_expiry_fallback_share": fallback_captures / len(records) if records else None,
        "usps_scan_captures": sum(r["basis"] == "usps_scan" for r in records),
        "unknown_capture_basis": sum(r["basis"] == "unknown" for r in records),
        "full_refund_confirmed": sum(r["refund"] == "full_refund_confirmed" for r in records),
        "partial_refund_confirmed": sum(r["refund"] == "partial_refund_confirmed" for r in records),
        "open_nonmailing_refunds": sum(r["refund"] == "refund_intent_open" for r in records),
        "unresolved_fallback_mailings": sum(bool(r["unresolved_mailing"]) for r in records),
        "conflicting_fallback_evidence": sum(bool(r["evidence_conflict"]) for r in records),
    }


def _original_capture_review(db: sqlite3.Connection, now: datetime) -> dict[str, object]:
    """Freeze live cohort membership by the order successful captures were recorded."""
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    evidence_available = (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pb_original_mail_findings'"
        ).fetchone()
        is not None
    )
    basis_sql = CAPTURE_BASIS_SQL
    if (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pb_original_capture_basis'"
        ).fetchone()
        is not None
    ):
        basis_sql = (
            "COALESCE((SELECT b.basis FROM pb_original_capture_basis b "
            f"WHERE b.case_token=h.case_token), {CAPTURE_BASIS_SQL})"
        )
    ops_table = (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pb_case_ops_ids'"
        ).fetchone()
        is not None
    )
    for row in db.execute(
        _ORIGINAL_CAPTURES.format(basis=basis_sql, acceptance_join=CAPTURE_ACCEPTANCE_JOIN_SQL)
    ):
        charge_ref = row["charge_ref"]
        if not isinstance(charge_ref, str) or not charge_ref or charge_ref in seen:
            continue
        if _utc(row["occurred_at"]) > now:
            continue
        seen.add(charge_ref)
        basis = row["basis"]
        facts = (
            original_mail_facts(
                db, row["case_token"], row["letter_id"], has_findings_tables=evidence_available
            )
            if basis == "hold_expiry_fallback" and row["letter_id"]
            else frozenset()
        )
        refund_cents = _confirmed_refund(row["refund_record"], row["amount_cents"])
        if refund_cents == row["amount_cents"]:
            refund = "full_refund_confirmed"
        elif refund_cents is not None:
            refund = "partial_refund_confirmed"
        elif row["refund_intent"] == "NEVER_MAILED":
            refund = "refund_intent_open"
        else:
            refund = "none_recorded"
        ops_ref = (
            db.execute(
                "SELECT ops_id FROM pb_case_ops_ids WHERE case_token=?", (row["case_token"],)
            ).fetchone()
            if ops_table
            else None
        )
        records.append(
            {
                "capture_number": len(records) + 1,
                "operations_ref": None if ops_ref is None else ops_ref[0],
                "environment": "live"
                if row["livemode"] == 1
                else "test"
                if row["livemode"] == 0
                else "unknown",
                "basis": basis,
                "refund": refund,
                "usps_scan_reported": "usps_scan" in facts,
                "city_receipt_verified": "city_receipt" in facts,
                "never_mailed_confirmed": "never_mailed" in facts,
                "evidence_conflict": "never_mailed" in facts
                and bool(facts & {"usps_scan", "city_receipt", "printer_handoff"}),
                "unresolved_mailing": basis == "hold_expiry_fallback"
                and not bool(facts & {"usps_scan", "city_receipt", "never_mailed"}),
            }
        )
    live = [item for item in records if item["environment"] == "live"]
    first_twenty = live[:_FIRST_PAID_REVIEW_SIZE]
    return {
        **_capture_totals(records),
        "by_environment": {
            mode: _capture_totals([item for item in records if item["environment"] == mode])
            for mode in ("live", "test", "unknown")
        },
        "fallback_outcomes": [item for item in records if item["basis"] == "hold_expiry_fallback"],
        "first_20_live_review": {
            "review_due": len(first_twenty) == _FIRST_PAID_REVIEW_SIZE,
            "target": _FIRST_PAID_REVIEW_SIZE,
            **_capture_totals(first_twenty),
            "captures": first_twenty,
        },
    }


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
        capture_review = _original_capture_review(db, now)
        unresolved = dict(
            db.execute(
                "SELECT state, COUNT(*) FROM pb_intake_authorization_attempts "
                "WHERE state IN ('pending', 'uncertain') GROUP BY state"
            ).fetchall()
        )

    return {
        "scope": "recorded SQLite state",
        "original_capture_review_all_dates": capture_review,
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
