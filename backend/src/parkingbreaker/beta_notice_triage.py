"""Private operator observations and a read-only controlled-beta prerequisite gate.

The caller authenticates operators and supplies an explicit allowlist. Customer notice
rows are never modified. All dates and observations are supplied, never inferred from
notice prose; decisions/dismissals are not scores. Coverage is a caller's attestation,
not proof that anyone is on duty. Passing this gate does not select or admit a case.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from parkingbreaker.beta_contact_store import (
    BetaContactConflict,
    BetaContactStoreMixin,
    ContactReadiness,
    _owned_case,
)
from parkingbreaker.beta_feedback_store import _COLUMNS as _NOTICE_COLUMNS
from parkingbreaker.beta_feedback_store import _receipt as _notice_receipt
from parkingbreaker.intake_store import IntakeConflict
from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error

OBSERVED_KINDS = frozenset(
    {
        "agency_acknowledgment",
        "procedural_deficiency",
        "decision_notice",
        "deadline_escalation",
        "unconfirmed_status",
    }
)
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}\Z")
_OPERATOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_ID = re.compile(r"[a-f0-9]{32}\Z")
_TABLE = "pb_beta_notice_triage"
_DDL = """CREATE TABLE pb_beta_notice_triage (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    triage_id TEXT NOT NULL UNIQUE,
    notice_id TEXT NOT NULL REFERENCES pb_beta_feedback_notices(notice_id),
    case_id TEXT NOT NULL REFERENCES pb_intake_records(intake_id),
    owner TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    command_id TEXT NOT NULL UNIQUE,
    observed_kind TEXT NOT NULL CHECK (observed_kind IN (
        'agency_acknowledgment', 'procedural_deficiency', 'decision_notice',
        'deadline_escalation', 'unconfirmed_status')),
    literal_stated_deadline_text TEXT,
    action_deadline_date TEXT,
    needs_confirmation INTEGER NOT NULL CHECK (needs_confirmation IN (0, 1)),
    urgent INTEGER NOT NULL CHECK (urgent IN (0, 1)),
    escalation_owner TEXT,
    evidence_ref TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    CHECK ((urgent = 0 AND observed_kind != 'deadline_escalation')
        OR escalation_owner IS NOT NULL),
    CHECK (action_deadline_date IS NOT NULL OR needs_confirmation = 1
        OR (COALESCE(length(literal_stated_deadline_text), 0) = 0
            AND observed_kind != 'deadline_escalation'))
)"""
_TRIGGERS = {
    f"{_TABLE}_no_{action.lower()}": (
        f"CREATE TRIGGER {_TABLE}_no_{action.lower()} BEFORE {action} ON {_TABLE} "
        "BEGIN SELECT RAISE(ABORT, 'beta notice triage is immutable'); END"
    )
    for action in ("UPDATE", "DELETE")
}
_TRIGGERS[f"{_TABLE}_no_replace"] = (
    f"CREATE TRIGGER {_TABLE}_no_replace BEFORE INSERT ON {_TABLE} "
    f"WHEN EXISTS (SELECT 1 FROM {_TABLE} WHERE sequence=NEW.sequence "
    "OR triage_id=NEW.triage_id OR command_id=NEW.command_id) "
    "BEGIN SELECT RAISE(ABORT, 'beta notice triage is immutable'); END"
)
_TRIGGERS[f"{_TABLE}_notice_binding"] = (
    f"CREATE TRIGGER {_TABLE}_notice_binding BEFORE INSERT ON {_TABLE} "
    "WHEN NOT EXISTS (SELECT 1 FROM pb_beta_feedback_notices "
    "WHERE notice_id=NEW.notice_id AND case_id=NEW.case_id AND owner=NEW.owner) "
    "BEGIN SELECT RAISE(ABORT, 'beta notice triage notice mismatch'); END"
)
_COLUMNS = (
    "sequence, triage_id, notice_id, case_id, owner, operator_id, command_id, "
    "observed_kind, literal_stated_deadline_text, action_deadline_date, "
    "needs_confirmation, urgent, escalation_owner, evidence_ref, observed_at, "
    "recorded_at, request_sha256"
)


class BetaNoticeTriageConflict(ValueError):
    """Fixed refusal keys only; no private evidence or notice text."""


def _key(value: object, label: str, pattern: re.Pattern[str] = _KEY) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise BetaNoticeTriageConflict(f"beta_notice_triage_invalid_{label}")
    return value


def _operators(value: object) -> frozenset[str]:
    if type(value) is not frozenset or not value:
        raise BetaNoticeTriageConflict("beta_notice_triage_operator_not_authorized")
    checked: set[str] = set()
    for operator in value:
        if not isinstance(operator, str) or _OPERATOR.fullmatch(operator) is None:
            raise BetaNoticeTriageConflict("beta_notice_triage_operator_not_authorized")
        checked.add(operator)
    return frozenset(checked)


def _authorize(operator: object, allowed: frozenset[str]) -> str:
    if not isinstance(operator, str) or operator not in allowed:
        raise BetaNoticeTriageConflict("beta_notice_triage_operator_not_authorized")
    return operator


def _timestamp(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BetaNoticeTriageConflict("beta_notice_triage_invalid_timestamp")
    try:
        return value.astimezone(UTC)
    except (ValueError, OverflowError):
        raise BetaNoticeTriageConflict("beta_notice_triage_invalid_timestamp") from None


def _date(value: object) -> str:
    if isinstance(value, str):
        try:
            if date.fromisoformat(value).isoformat() == value:
                return value
        except ValueError:
            pass
    raise BetaNoticeTriageConflict("beta_notice_triage_invalid_date")


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise BetaNoticeTriageConflict("beta_notice_triage_invalid_boolean")
    return value


def _literal(value: object) -> str | None:
    if value is not None and (not isinstance(value, str) or len(value) > 1000):
        raise BetaNoticeTriageConflict("beta_notice_triage_invalid_literal_deadline")
    return value


@dataclass(frozen=True, repr=False)
class NoticeTriageRecord:
    sequence: int
    triage_id: str
    notice_id: str
    case_id: str
    owner: str
    operator_id: str
    command_id: str
    observed_kind: str
    literal_stated_deadline_text: str | None
    action_deadline_date: str | None
    needs_confirmation: bool
    urgent: bool
    escalation_owner: str | None
    evidence_ref: str
    observed_at: datetime
    recorded_at: datetime

    def __repr__(self) -> str:
        return "NoticeTriageRecord(<redacted>)"


@dataclass(frozen=True, repr=False)
class NoticeTriageEvaluation:
    needs_confirmation: bool
    escalation_required: bool
    escalation_owner: str | None
    confirmed_action_deadline_date: str | None

    def __repr__(self) -> str:
        return "NoticeTriageEvaluation(<redacted>)"


def evaluate_notice_triage(record: NoticeTriageRecord) -> NoticeTriageEvaluation:
    """Keep uncertain dates uncertain; never infer a legal deadline or acknowledgment."""
    _boolean(record.needs_confirmation)
    _boolean(record.urgent)
    if record.observed_kind not in OBSERVED_KINDS:
        raise BetaNoticeTriageConflict("beta_notice_triage_invalid_kind")
    _literal(record.literal_stated_deadline_text)
    if record.action_deadline_date is not None:
        _date(record.action_deadline_date)
    escalation = record.urgent or record.observed_kind == "deadline_escalation"
    if escalation and record.escalation_owner is None:
        raise BetaNoticeTriageConflict("beta_notice_triage_escalation_owner_required")
    if record.escalation_owner is not None:
        _key(record.escalation_owner, "escalation_owner", _OPERATOR)
    if (
        record.action_deadline_date is None
        and (record.literal_stated_deadline_text or record.observed_kind == "deadline_escalation")
        and not record.needs_confirmation
    ):
        raise BetaNoticeTriageConflict("beta_notice_triage_needs_confirmation_required")
    return NoticeTriageEvaluation(
        record.needs_confirmation,
        escalation,
        record.escalation_owner,
        None if record.needs_confirmation else record.action_deadline_date,
    )


def _fingerprint(record: NoticeTriageRecord) -> str:
    # Receipt time and generated identifiers are not part of an idempotent command.
    payload = [
        record.notice_id,
        record.case_id,
        record.owner,
        record.operator_id,
        record.command_id,
        record.observed_kind,
        record.literal_stated_deadline_text,
        record.action_deadline_date,
        record.needs_confirmation,
        record.urgent,
        record.escalation_owner,
        record.evidence_ref,
        record.observed_at.isoformat(),
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _stored_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    parsed = _timestamp(datetime.fromisoformat(value))
    if parsed.isoformat() != value:
        raise ValueError
    return parsed


def _stored_boolean(value: object) -> bool:
    if type(value) is not int or value not in (0, 1):
        raise ValueError
    return bool(value)


def _decode(row: tuple[object, ...]) -> NoticeTriageRecord:
    try:
        if len(row) != 17 or type(row[0]) is not int or row[0] < 1:
            raise ValueError
        record = NoticeTriageRecord(
            row[0],
            _key(row[1], "triage_id", _ID),
            _key(row[2], "notice_id", _ID),
            _key(row[3], "case_id"),
            _key(row[4], "owner"),
            _key(row[5], "operator", _OPERATOR),
            _key(row[6], "command_id", _OPERATOR),
            _key(row[7], "kind"),
            _literal(row[8]),
            None if row[9] is None else _date(row[9]),
            _stored_boolean(row[10]),
            _stored_boolean(row[11]),
            None if row[12] is None else _key(row[12], "escalation_owner", _OPERATOR),
            _key(row[13], "evidence_ref"),
            _stored_timestamp(row[14]),
            _stored_timestamp(row[15]),
        )
        evaluate_notice_triage(record)
        if record.observed_at > record.recorded_at or _fingerprint(record) != row[16]:
            raise ValueError
        return record
    except (TypeError, ValueError, OverflowError):
        raise StorageCorruption("invalid beta notice triage record") from None


def _normalized(sql: object) -> str:
    return " ".join(sql.upper().split()) if isinstance(sql, str) else ""


def _begin(database: sqlite3.Connection, *, write: bool) -> None:
    if database.in_transaction:
        raise StorageCorruption("beta notice triage needs its own transaction")
    database.execute("BEGIN IMMEDIATE" if write else "BEGIN")


def initialize_beta_notice_triage(database: sqlite3.Connection) -> None:
    """Install only this component; refuse partial or altered schema on reopening."""
    _begin(database, write=True)
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='beta_notice_triage'"
        ).fetchone()
        tables = dict(
            database.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)
            )
        )
        triggers = dict(
            database.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (_TABLE,)
            )
        )
        if version is None:
            if tables or triggers:
                raise StorageCorruption("unversioned beta notice triage schema")
            database.execute(_DDL)
            for ddl in _TRIGGERS.values():
                database.execute(ddl)
            database.execute("INSERT INTO pb_store_versions VALUES ('beta_notice_triage', 1)")
        elif (
            version != (1,)
            or {name: _normalized(sql) for name, sql in tables.items()}
            != {_TABLE: _normalized(_DDL)}
            or {name: _normalized(sql) for name, sql in triggers.items()}
            != {name: _normalized(sql) for name, sql in _TRIGGERS.items()}
        ):
            raise StorageCorruption("invalid beta notice triage schema")
        database.execute("COMMIT")
    except Exception:
        rollback_preserving_error(database)
        raise


def _notice(database: sqlite3.Connection, notice_id: str, case_id: str, owner: str) -> None:
    _owned_case(database, case_id, owner)
    row = database.execute(
        f"SELECT {_NOTICE_COLUMNS} FROM pb_beta_feedback_notices "
        "WHERE notice_id=? AND case_id=? AND owner=?",
        (notice_id, case_id, owner),
    ).fetchone()
    if row is None:
        raise IntakeConflict("not_found")
    _notice_receipt(row)


class BetaNoticeTriageStore:
    """Private store on the existing database. All reads/writes require an operator."""

    def __init__(
        self, database: sqlite3.Connection, *, authorized_operators: frozenset[str]
    ) -> None:
        self._operators = _operators(authorized_operators)
        initialize_beta_notice_triage(database)
        self._database = database

    def record(
        self,
        notice_id: str,
        case_id: str,
        owner: str,
        operator_id: str,
        command_id: str,
        observed_kind: str,
        literal_stated_deadline_text: str | None,
        action_deadline_date: str | None,
        needs_confirmation: bool,
        escalation_owner: str | None,
        evidence_ref: str,
        observed_at: datetime,
        recorded_at: datetime,
        *,
        urgent: bool = False,
    ) -> NoticeTriageRecord:
        operator = _authorize(operator_id, self._operators)
        if escalation_owner is not None:
            _authorize(escalation_owner, self._operators)
        proposed = NoticeTriageRecord(
            0,
            uuid4().hex,
            _key(notice_id, "notice_id", _ID),
            _key(case_id, "case_id"),
            _key(owner, "owner"),
            operator,
            _key(command_id, "command_id", _OPERATOR),
            _key(observed_kind, "kind"),
            _literal(literal_stated_deadline_text),
            None if action_deadline_date is None else _date(action_deadline_date),
            _boolean(needs_confirmation),
            _boolean(urgent),
            escalation_owner,
            _key(evidence_ref, "evidence_ref"),
            _timestamp(observed_at),
            _timestamp(recorded_at),
        )
        evaluate_notice_triage(proposed)
        if proposed.observed_at > proposed.recorded_at:
            raise BetaNoticeTriageConflict("beta_notice_triage_future_observation")
        digest = _fingerprint(proposed)
        database = self._database
        _begin(database, write=True)
        try:
            _notice(database, proposed.notice_id, proposed.case_id, proposed.owner)
            row = database.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} WHERE command_id=?", (proposed.command_id,)
            ).fetchone()
            if row is not None:
                result = _decode(row)
                if _fingerprint(result) != digest:
                    raise BetaNoticeTriageConflict("beta_notice_triage_command_conflict")
            else:
                database.execute(
                    f"INSERT INTO {_TABLE} ({_COLUMNS.split(', ', 1)[1]}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        proposed.triage_id,
                        proposed.notice_id,
                        proposed.case_id,
                        proposed.owner,
                        proposed.operator_id,
                        proposed.command_id,
                        proposed.observed_kind,
                        proposed.literal_stated_deadline_text,
                        proposed.action_deadline_date,
                        int(proposed.needs_confirmation),
                        int(proposed.urgent),
                        proposed.escalation_owner,
                        proposed.evidence_ref,
                        proposed.observed_at.isoformat(),
                        proposed.recorded_at.isoformat(),
                        digest,
                    ),
                )
                row = database.execute(
                    f"SELECT {_COLUMNS} FROM {_TABLE} WHERE command_id=?", (proposed.command_id,)
                ).fetchone()
                if row is None:
                    raise StorageCorruption("beta notice triage record disappeared")
                result = _decode(row)
            database.execute("COMMIT")
            return result
        except Exception:
            rollback_preserving_error(database)
            raise

    def list_records(
        self,
        case_id: str,
        owner: str,
        operator_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[NoticeTriageRecord, ...]:
        """Private bounded insertion order, including historical/unconfirmed observations."""
        _authorize(operator_id, self._operators)
        case_id, owner = _key(case_id, "case_id"), _key(owner, "owner")
        if type(after_sequence) is not int or after_sequence < 0:
            raise BetaNoticeTriageConflict("beta_notice_triage_invalid_cursor")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BetaNoticeTriageConflict("beta_notice_triage_invalid_limit")
        database = self._database
        _begin(database, write=False)
        try:
            _owned_case(database, case_id, owner)
            rows = database.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} WHERE case_id=? AND owner=? AND sequence>? "
                "ORDER BY sequence LIMIT ?",
                (case_id, owner, after_sequence, limit),
            ).fetchall()
            records = tuple(_decode(row) for row in rows)
            for record in records:
                _notice(database, record.notice_id, case_id, owner)
            database.execute("COMMIT")
            return records
        except Exception:
            rollback_preserving_error(database)
            raise


class ContactStatusReader(Protocol):
    def read_status(self, case_id: str, owner: str) -> ContactReadiness: ...


class BetaContactStore:
    """Read-status adapter for the existing, already initialized contact component."""

    def __init__(self, database: sqlite3.Connection) -> None:
        self._database = database

    def _require_open(self) -> sqlite3.Connection:
        return self._database

    def read_status(self, case_id: str, owner: str) -> ContactReadiness:
        _begin(self._database, write=False)
        try:
            status = BetaContactStoreMixin.beta_contact_readiness(self, case_id, owner)
            self._database.execute("COMMIT")
            return status
        except Exception:
            rollback_preserving_error(self._database)
            raise


@dataclass(frozen=True, repr=False)
class ReviewedFeedbackPlan:
    issuer: str
    stage: str
    source_url: str
    retrieved_on: str
    escalation_owner: str
    reviewed: bool

    def __repr__(self) -> str:
        return "ReviewedFeedbackPlan(<redacted>)"


@dataclass(frozen=True, repr=False)
class CohortCoverageRoster:
    primary_operator: str | None
    backup_operator: str | None
    weekend_operator: str | None
    reviewed_feedback_plan: ReviewedFeedbackPlan | None

    def __repr__(self) -> str:
        return "CohortCoverageRoster(<redacted>)"


@dataclass(frozen=True)
class CohortAdmissionReadiness:
    qualified: bool
    reasons: tuple[str, ...]


def _plan_ready(
    plan: ReviewedFeedbackPlan | None,
    issuer: str,
    stage: str,
    as_of: date,
    allowed: frozenset[str],
) -> bool:
    if not isinstance(plan, ReviewedFeedbackPlan) or plan.reviewed is not True:
        return False
    try:
        _key(issuer, "issuer")
        _key(stage, "stage")
        if plan.issuer != issuer or plan.stage != stage or type(as_of) is not date:
            return False
        if date.fromisoformat(_date(plan.retrieved_on)) > as_of:
            return False
        _authorize(plan.escalation_owner, allowed)
        url = plan.source_url
        if not isinstance(url, str) or not url or len(url) > 2048 or any(c.isspace() for c in url):
            return False
        parsed = urlsplit(url)
        return (
            parsed.scheme in {"https", "http"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except (TypeError, ValueError):
        return False


def evaluate_cohort_admission(
    contact_store: ContactStatusReader,
    case_id: str,
    owner: str,
    coverage: CohortCoverageRoster | None,
    *,
    issuer: str,
    stage: str,
    as_of: date,
    authorized_operators: frozenset[str],
) -> CohortAdmissionReadiness:
    """Evaluate fresh contact facts and explicit coverage; never mutate admission.

    Primary and backup must be different people. Either may explicitly cover the
    weekend; an absent weekend assignment never inherits a weekday assignment.
    Source URLs are provenance attestations, not automatically qualified sources.
    """
    reasons: list[str] = []
    try:
        allowed = _operators(authorized_operators)
    except BetaNoticeTriageConflict:
        allowed = frozenset()
        reasons.append("coverage_operator_allowlist_required")
    try:
        status = contact_store.read_status(case_id, owner)
    except (BetaContactConflict, IntakeConflict):
        reasons.append("contact_status_unavailable")
    else:
        if status.permission != "permitted":
            reasons.append("backup_contact_permission_required")
        if status.verification_evidence != "supplied":
            reasons.append("primary_email_verification_required")
        if status.reply_observation != "supplied":
            reasons.append("primary_email_two_way_reply_required")
        if status.reason != "beta_contact_ready" or not status.target_id:
            reasons.append("contact_not_ready")
    if not isinstance(coverage, CohortCoverageRoster):
        reasons.append("coverage_roster_required")
    else:
        for role, operator in (
            ("primary", coverage.primary_operator),
            ("backup", coverage.backup_operator),
            ("weekend", coverage.weekend_operator),
        ):
            if not isinstance(operator, str) or operator not in allowed:
                reasons.append(f"coverage_{role}_operator_required")
        if coverage.primary_operator == coverage.backup_operator:
            reasons.append("coverage_primary_backup_must_differ")
        if not _plan_ready(coverage.reviewed_feedback_plan, issuer, stage, as_of, allowed):
            reasons.append("reviewed_feedback_plan_required")
    return CohortAdmissionReadiness(not reasons, tuple(reasons))
