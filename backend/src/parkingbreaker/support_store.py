"""Durable spoken support codes and closed-enum support audit log on SQLite.

Every case receives one short, immutable, spoken-friendly support code (PB-XXXXX) in the
same SQLite transaction that creates the case. Support intake and outbound decisions are
recorded as closed enums plus a SHA-256 hash of the redacted inbound text — never raw
customer text, ticket details, addresses, or card numbers.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from parkingbreaker.identity_store import _stamp, _stored_time, _utc
from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error

SUPPORT_CODE_PATTERN = re.compile(r"^PB-[0-9]{5}$")


class SupportChannel(StrEnum):
    EMAIL = "email"
    VOICEMAIL = "voicemail"


class SupportIntent(StrEnum):
    NO_ID = "NO_ID"
    APPEAL_STATUS = "APPEAL_STATUS"
    PAYMENT_STATUS = "PAYMENT_STATUS"
    STOP_REASON = "STOP_REASON"
    RETURNED_MAIL_STATUS = "RETURNED_MAIL_STATUS"
    ADVICE_OR_STRATEGY = "ADVICE_OR_STRATEGY"
    REFUND_DISPUTE_COMPLAINT = "REFUND_DISPUTE_COMPLAINT"
    AGENCY_NOTICE = "AGENCY_NOTICE"
    RETURNED_MAIL_DISPUTE = "RETURNED_MAIL_DISPUTE"
    OTHER_SENSITIVE = "OTHER_SENSITIVE"


class SupportOutcome(StrEnum):
    NO_ID_NO_RESPONSE = "NO_ID_NO_RESPONSE"
    AUTO_REPLIED = "AUTO_REPLIED"
    FIXED_REPLIED = "FIXED_REPLIED"
    HELD_FOR_OWNER = "HELD_FOR_OWNER"
    RATE_LIMITED_HELD = "RATE_LIMITED_HELD"
    REJECTED_DRAFT_HELD = "REJECTED_DRAFT_HELD"
    API_ERROR_HELD = "API_ERROR_HELD"


class SupportHoldReason(StrEnum):
    NEEDS_OPERATOR = "needs_operator"
    REFUND_DISPUTE_COMPLAINT = "refund_dispute_complaint"
    LEGAL_THREAT = "legal_threat"
    AGENCY_NOTICE = "agency_notice"
    AGENCY_NOTICE_URGENT = "agency_notice_urgent"
    RETURNED_MAIL_BEYOND_REMEDY = "returned_mail_beyond_remedy"
    PAYMENT_DISPUTED = "payment_disputed"
    LOW_AI_CONFIDENCE = "low_ai_confidence"
    DRAFT_GUARD_REJECTED = "draft_guard_rejected"
    RATE_LIMITED = "rate_limited"
    MODEL_UNAVAILABLE = "model_unavailable"
    BUDGET_EXCEEDED = "budget_exceeded"
    NO_BOUND_EMAIL = "no_bound_email"


@dataclass(frozen=True)
class SupportMessageRecord:
    inbound_message_id: str
    message_sha256: str
    channel: SupportChannel
    case_token: str | None
    support_code: str | None
    intent: SupportIntent
    outcome: SupportOutcome
    hold_reason: SupportHoldReason | None
    email_idempotency_key: str | None
    email_status: str | None
    urgent: bool
    created_at: datetime


_CODES_DDL = """CREATE TABLE pb_support_codes (
    case_token TEXT PRIMARY KEY REFERENCES pb_cases(case_token),
    support_code TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
)"""

_MESSAGES_DDL = """CREATE TABLE pb_support_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_message_id TEXT NOT NULL UNIQUE,
    message_sha256 TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('email', 'voicemail')),
    case_token TEXT REFERENCES pb_cases(case_token),
    support_code TEXT,
    intent TEXT NOT NULL,
    outcome TEXT NOT NULL,
    hold_reason TEXT,
    email_idempotency_key TEXT,
    email_status TEXT CHECK (
        email_status IS NULL OR email_status IN ('accepted', 'uncertain', 'rejected')
    ),
    urgent INTEGER NOT NULL DEFAULT 0 CHECK (urgent IN (0, 1)),
    created_at TEXT NOT NULL
)"""

_CONFIRMATIONS_DDL = """CREATE TABLE pb_support_confirmation_emails (
    case_token TEXT PRIMARY KEY REFERENCES pb_cases(case_token),
    support_code TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    email_status TEXT NOT NULL CHECK (email_status IN ('accepted', 'uncertain', 'rejected')),
    sent_at TEXT NOT NULL
)"""

_TABLES = {
    "pb_support_codes": _CODES_DDL,
    "pb_support_messages": _MESSAGES_DDL,
    "pb_support_confirmation_emails": _CONFIRMATIONS_DDL,
}


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


def _new_support_code() -> str:
    return f"PB-{secrets.randbelow(90000) + 10000}"


def initialize_support(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='support'"
        ).fetchone()
        existing = {
            row[0]: " ".join(row[1].upper().split())
            for row in database.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
            if row[0] in _TABLES
        }
        expected = {name: " ".join(sql.upper().split()) for name, sql in _TABLES.items()}
        if version is None:
            if existing:
                raise StorageCorruption("unversioned support schema")
            for ddl in _TABLES.values():
                database.execute(ddl)
            database.execute("INSERT INTO pb_store_versions VALUES ('support', 1)")
        elif version != (1,) or existing != expected:
            raise StorageCorruption("malformed support schema")

        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_support_codes_immutable_update "
            "BEFORE UPDATE ON pb_support_codes "
            "BEGIN SELECT RAISE(ABORT, 'support codes are immutable'); END"
        )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_support_codes_immutable_delete "
            "BEFORE DELETE ON pb_support_codes "
            "BEGIN SELECT RAISE(ABORT, 'support codes are immutable'); END"
        )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_support_messages_no_update "
            "BEFORE UPDATE ON pb_support_messages "
            "BEGIN SELECT RAISE(ABORT, 'support log is append-only'); END"
        )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_support_messages_no_delete "
            "BEFORE DELETE ON pb_support_messages "
            "BEGIN SELECT RAISE(ABORT, 'support log is append-only'); END"
        )
        database.execute(
            "CREATE INDEX IF NOT EXISTS idx_pb_support_messages_case_time "
            "ON pb_support_messages(case_token, created_at)"
        )
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def _row_to_record(row: tuple[object, ...]) -> SupportMessageRecord:
    (
        inbound_id,
        digest,
        channel,
        case_token,
        support_code,
        intent,
        outcome,
        hold_reason,
        email_key,
        email_status,
        urgent,
        created_at,
    ) = row
    parsed_time = _stored_time(str(created_at))
    if parsed_time is None:
        raise StorageCorruption("malformed support message timestamp")
    return SupportMessageRecord(
        inbound_message_id=str(inbound_id),
        message_sha256=str(digest),
        channel=SupportChannel(str(channel)),
        case_token=None if case_token is None else str(case_token),
        support_code=None if support_code is None else str(support_code),
        intent=SupportIntent(str(intent)),
        outcome=SupportOutcome(str(outcome)),
        hold_reason=None if hold_reason is None else SupportHoldReason(str(hold_reason)),
        email_idempotency_key=None if email_key is None else str(email_key),
        email_status=None if email_status is None else str(email_status),
        urgent=bool(urgent),
        created_at=parsed_time,
    )


class SupportStoreMixin:
    def _insert_support_code(self: _Connected, case_token: str, created_at: datetime) -> str:
        """Assign a unique PB-XXXXX support code inside an open case-creation transaction."""
        database = self._require_open()
        existing = database.execute(
            "SELECT support_code FROM pb_support_codes WHERE case_token=?",
            (case_token,),
        ).fetchone()
        if existing is not None:
            return str(existing[0])
        stamp = _stamp(_utc(created_at))
        for _ in range(32):
            code = _new_support_code()
            try:
                database.execute(
                    "INSERT INTO pb_support_codes (case_token, support_code, created_at) "
                    "VALUES (?, ?, ?)",
                    (case_token, code, stamp),
                )
                return code
            except sqlite3.IntegrityError:
                # Collision on UNIQUE support_code; retry with a fresh random code.
                continue
        raise StorageCorruption("unable to allocate unique support code")

    def ensure_support_code(self: _Connected, case_token: str, now: datetime) -> str:
        """Return the case's spoken support code, backfilling legacy cases if absent."""
        existing = SupportStoreMixin.support_code_for_case(self, case_token)
        if existing is not None:
            return existing
        database = self._require_open()
        database.execute("BEGIN IMMEDIATE")
        try:
            code = SupportStoreMixin._insert_support_code(self, case_token, now)
            database.execute("COMMIT")
            return code
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def support_code_for_case(self: _Connected, case_token: str) -> str | None:
        row = (
            self._require_open()
            .execute(
                "SELECT support_code FROM pb_support_codes WHERE case_token=?",
                (case_token,),
            )
            .fetchone()
        )
        return None if row is None else str(row[0])

    def case_token_for_support_code(self: _Connected, support_code: str) -> str | None:
        normalized = support_code.strip().upper()
        if SUPPORT_CODE_PATTERN.fullmatch(normalized) is None:
            return None
        row = (
            self._require_open()
            .execute(
                "SELECT case_token FROM pb_support_codes WHERE support_code=?",
                (normalized,),
            )
            .fetchone()
        )
        return None if row is None else str(row[0])

    def get_support_message(
        self: _Connected, inbound_message_id: str
    ) -> SupportMessageRecord | None:
        row = (
            self._require_open()
            .execute(
                "SELECT inbound_message_id, message_sha256, channel, case_token, support_code, "
                "intent, outcome, hold_reason, email_idempotency_key, email_status, urgent, "
                "created_at FROM pb_support_messages WHERE inbound_message_id=?",
                (inbound_message_id,),
            )
            .fetchone()
        )
        return None if row is None else _row_to_record(row)

    def record_support_message(
        self: _Connected,
        *,
        inbound_message_id: str,
        message_sha256: str,
        channel: SupportChannel,
        case_token: str | None,
        support_code: str | None,
        intent: SupportIntent,
        outcome: SupportOutcome,
        hold_reason: SupportHoldReason | None,
        email_idempotency_key: str | None,
        email_status: str | None,
        urgent: bool,
        created_at: datetime,
    ) -> SupportMessageRecord:
        """Append one closed-enum audit row (never stores free text)."""
        database = self._require_open()
        database.execute("BEGIN IMMEDIATE")
        try:
            existing = database.execute(
                "SELECT inbound_message_id, message_sha256, channel, case_token, support_code, "
                "intent, outcome, hold_reason, email_idempotency_key, email_status, urgent, "
                "created_at FROM pb_support_messages WHERE inbound_message_id=?",
                (inbound_message_id,),
            ).fetchone()
            if existing is not None:
                database.execute("COMMIT")
                return _row_to_record(existing)
            database.execute(
                "INSERT INTO pb_support_messages "
                "(inbound_message_id, message_sha256, channel, case_token, support_code, "
                "intent, outcome, hold_reason, email_idempotency_key, email_status, urgent, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    inbound_message_id,
                    message_sha256,
                    channel.value,
                    case_token,
                    support_code,
                    intent.value,
                    outcome.value,
                    None if hold_reason is None else hold_reason.value,
                    email_idempotency_key,
                    email_status,
                    1 if urgent else 0,
                    _stamp(_utc(created_at)),
                ),
            )
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
        record = SupportStoreMixin.get_support_message(self, inbound_message_id)
        if record is None:
            raise StorageCorruption("support message disappeared after commit")
        return record

    def count_recent_support_replies(self: _Connected, case_token: str, *, since: datetime) -> int:
        """Count automated replies sent for one case since a UTC cutoff."""
        (count,) = (
            self._require_open()
            .execute(
                "SELECT COUNT(*) FROM pb_support_messages "
                "WHERE case_token=? AND outcome IN ('AUTO_REPLIED', 'FIXED_REPLIED') "
                "AND created_at >= ?",
                (case_token, _stamp(_utc(since))),
            )
            .fetchone()
        )
        return int(count)

    def has_support_confirmation(self: _Connected, case_token: str) -> bool:
        row = (
            self._require_open()
            .execute(
                "SELECT 1 FROM pb_support_confirmation_emails WHERE case_token=?",
                (case_token,),
            )
            .fetchone()
        )
        return row is not None

    def record_support_confirmation(
        self: _Connected,
        *,
        case_token: str,
        support_code: str,
        idempotency_key: str,
        email_status: str,
        sent_at: datetime,
    ) -> None:
        database = self._require_open()
        database.execute("BEGIN IMMEDIATE")
        try:
            database.execute(
                "INSERT OR IGNORE INTO pb_support_confirmation_emails "
                "(case_token, support_code, idempotency_key, email_status, sent_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (case_token, support_code, idempotency_key, email_status, _stamp(_utc(sent_at))),
            )
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
