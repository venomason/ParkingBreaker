"""Restricted original-letter evidence in the transaction database."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from parkingbreaker.lob_submission import Accepted
from parkingbreaker.lob_webhook import has_usps_scan
from parkingbreaker.mail_attempt_store import rollback_preserving_error

RAW_LOOKUP_RETENTION = timedelta(days=90)


def initialize_paid_capture_evidence(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS pb_original_capture_basis (
        case_token TEXT PRIMARY KEY REFERENCES pb_cases(case_token),
        basis TEXT NOT NULL CHECK (basis IN ('usps_scan', 'hold_expiry_fallback', 'unknown'))
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS pb_capture_fallback_evidence (
        case_token TEXT PRIMARY KEY REFERENCES pb_cases(case_token),
        operation_id TEXT NOT NULL UNIQUE REFERENCES pb_submission_operations(operation_id),
        letter_id TEXT NOT NULL,
        send_date TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        raw_sha256 TEXT NOT NULL CHECK (length(raw_sha256) = 64),
        raw_expires_at TEXT NOT NULL,
        printer_handoff INTEGER NOT NULL CHECK (printer_handoff IN (0, 1)),
        raw_body BLOB CHECK (raw_body IS NULL OR length(raw_body) BETWEEN 1 AND 1048576)
    )""")


def record_fallback_lookup(
    db: sqlite3.Connection,
    path: Path,
    case_token: str,
    operation_id: str,
    submission_token: str,
    outcome: Accepted,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    proof = outcome.fallback_lookup
    if (
        not db.in_transaction
        or proof is None
        or not proof.send_date < proof.observed_at
        or not started_at <= proof.observed_at <= finished_at
        or path.stat().st_mode & 0o077
    ):
        raise ValueError("fallback requires private decision-time lookup evidence")
    letter = json.loads(proof.body)
    if not isinstance(letter, dict):
        raise ValueError("invalid fallback letter evidence")
    metadata = letter.get("metadata")
    if (
        letter.get("id") != outcome.letter_id
        or not isinstance(metadata, dict)
        or metadata.get("submission_token") != submission_token
        or letter.get("status") != "rendered"
        or any(
            letter.get(flag) is not None and letter[flag] is not False
            for flag in ("deleted", "canceled", "cancelled")
        )
        or letter.get("failure_reason") is not None
        or not isinstance(letter.get("send_date"), str)
        or datetime.fromisoformat(letter["send_date"]) != proof.send_date
        or has_usps_scan(letter.get("tracking_events"))
    ):
        raise ValueError("fallback body does not substantiate capture")
    tracking = letter.get("tracking_events")
    printer_handoff = isinstance(tracking, list) and any(
        isinstance(event, dict) and event.get("name") == "Mailed" for event in tracking
    )
    db.execute(
        "INSERT INTO pb_capture_fallback_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            case_token,
            operation_id,
            outcome.letter_id,
            proof.send_date.astimezone(UTC).isoformat(),
            proof.observed_at.astimezone(UTC).isoformat(),
            hashlib.sha256(proof.body).hexdigest(),
            (proof.observed_at.astimezone(UTC) + RAW_LOOKUP_RETENTION).isoformat(),
            int(printer_handoff),
            proof.body,
        ),
    )


def expire_raw_fallback_lookups(db: sqlite3.Connection, now: datetime) -> None:
    """Remove expired response bytes; retain the digest, dates and exact decision."""
    if now.utcoffset() is None:
        raise ValueError("retention clock must include a UTC offset")
    db.execute("PRAGMA secure_delete=ON")
    db.execute("BEGIN IMMEDIATE")
    try:
        holds_installed = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pb_retention_holds'"
        ).fetchone()
        holds_clause = (
            "AND NOT EXISTS (SELECT 1 FROM pb_retention_holds h "
            "WHERE h.case_token=e.case_token AND h.active=1)"
            if holds_installed
            else ""
        )
        db.execute(
            "UPDATE pb_capture_fallback_evidence AS e SET raw_body=NULL "
            "WHERE raw_body IS NOT NULL AND raw_expires_at <= ? " + holds_clause,
            (now.astimezone(UTC).isoformat(),),
        )
        db.execute("COMMIT")
    except Exception:
        rollback_preserving_error(db)
        raise


CAPTURE_BASIS_SQL = """
    CASE
      WHEN h.event_name <> 'CaptureSucceeded' OR NOT EXISTS (
        SELECT 1 FROM pb_payment_operations p
        WHERE p.case_token=h.case_token AND json_extract(p.intent, '$.kind')='capture'
          AND json_extract(p.intent, '$.call_kind')='capture'
          AND julianday(json_extract(p.intent, '$.next_at')) <= julianday(h.occurred_at)
      ) THEN 'unknown'
      WHEN json_extract(acceptance.outcome_json, '$.evidence')='expiry_fallback'
        THEN 'hold_expiry_fallback'
      WHEN json_extract(acceptance.outcome_json, '$.evidence')='production' AND (
        json_array_length(acceptance.outcome_json, '$.scans') > 0
        OR json_extract(acceptance.outcome_json, '$.verified_scan_webhook')=1
      ) THEN 'usps_scan'
      ELSE 'unknown'
    END
"""


CAPTURE_ACCEPTANCE_JOIN_SQL = """
    LEFT JOIN pb_submission_operations acceptance ON acceptance.operation_id=(
        SELECT so.operation_id FROM pb_submission_operations so
        WHERE so.case_token=h.case_token AND so.attempt=1
          AND json_extract(so.outcome_json, '$.kind')='accepted'
          AND julianday(so.completed_at) <= (
            SELECT MIN(julianday(json_extract(p.intent, '$.next_at')))
            FROM pb_payment_operations p
            WHERE p.case_token=h.case_token AND json_extract(p.intent, '$.kind')='capture'
              AND json_extract(p.intent, '$.call_kind')='capture'
          )
        ORDER BY so.rowid LIMIT 1
    )
"""


def initialize_original_mail_findings(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS pb_original_mail_findings (
        case_token TEXT NOT NULL REFERENCES pb_cases(case_token),
        letter_id TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('lob_lookup', 'lob_webhook', 'verified_city')),
        finding TEXT NOT NULL CHECK (finding IN ('usps_scan', 'city_receipt', 'never_mailed')),
        evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
        verified_by TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        PRIMARY KEY (source, evidence_sha256)
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS pb_original_mail_findings_case
        ON pb_original_mail_findings(case_token, letter_id)""")
    db.execute("""CREATE TABLE IF NOT EXISTS pb_original_mail_rechecks (
        case_token TEXT PRIMARY KEY REFERENCES pb_cases(case_token),
        checked_at TEXT NOT NULL,
        checks INTEGER NOT NULL CHECK (checks BETWEEN 1 AND 30)
    )""")


def original_mail_facts(
    db: sqlite3.Connection, case_token: str, letter_id: str, *, has_findings_tables: bool = True
) -> frozenset[str]:
    """Exact original-letter observations. A printer handoff can block a refund."""
    findings: set[str] = set()
    if has_findings_tables:
        findings.update(
            row[0]
            for row in db.execute(
                "SELECT finding FROM pb_original_mail_findings WHERE case_token=? AND letter_id=?",
                (case_token, letter_id),
            )
        )
        if (
            db.execute(
                "SELECT 1 FROM pb_capture_fallback_evidence WHERE case_token=? AND letter_id=? "
                "AND printer_handoff=1",
                (case_token, letter_id),
            ).fetchone()
            is not None
        ):
            findings.add("printer_handoff")
    for scan, handoff in db.execute(
        """
        SELECT json_extract(payload, '$.scan_evidence'),
               json_extract(payload, '$.printer_handoff_evidence')
        FROM pb_webhooks WHERE provider='lob' AND case_token=?
          AND json_extract(payload, '$.attempt')=1 AND json_extract(payload, '$.letter_id')=?
        """,
        (case_token, letter_id),
    ):
        if scan == 1:
            findings.add("usps_scan")
        if handoff == 1:
            findings.add("printer_handoff")
    return frozenset(findings)


def automatic_nonmailing_refund_supported(facts: frozenset[str]) -> bool:
    return "never_mailed" in facts and facts.isdisjoint(
        {"usps_scan", "city_receipt", "printer_handoff", "legacy_production"}
    )
