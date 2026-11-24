"""Immutable owner-scoped accepted-beta agency notice records.

A notice is customer-reported evidence: page order and literal deadline text are kept
exactly, dates are never interpreted, and no OCR, legal validation, completeness,
acceptance or outcome claim is made. The store only checks the syntax it can see; it
never pretends to validate photo bytes or their ownership. Photo bytes and an
owner-aware traversal belong to the API and later backup work.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol
from uuid import uuid4

from parkingbreaker.beta_store import _entitlement
from parkingbreaker.intake_store import IntakeConflict
from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.transaction_records import _reject_constant, _unique_object

_DDL = """CREATE TABLE pb_beta_feedback_notices (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES pb_intake_records(intake_id),
    owner TEXT NOT NULL,
    notice_id TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    correction_of TEXT,
    needs_confirmation INTEGER NOT NULL CHECK (needs_confirmation = 1),
    review_state TEXT NOT NULL CHECK (review_state = 'unreviewed'),
    UNIQUE (case_id, request_id)
)"""
_REF = re.compile(r"photo_[a-f0-9]{64}\Z")
_NOTICE = re.compile(r"[a-f0-9]{32}\Z")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}")
_ROLES = frozenset({"envelope", "page"})
_PAYLOAD_KEYS = {
    "case_id",
    "owner",
    "request_id",
    "pages",
    "customer_seen_on",
    "printed_on",
    "stated_deadline_text",
    "correction_of",
}
_COLUMNS = (
    "sequence, case_id, owner, notice_id, request_id, received_at, payload_json, "
    "payload_sha256, correction_of, needs_confirmation, review_state"
)
_TRIGGER_TABLE = "pb_beta_feedback_notices"
_TRIGGER_KEYS = ("no_update", "no_delete")


def _trigger_ddl(action: str) -> str:
    return (
        f"CREATE TRIGGER {_TRIGGER_TABLE}_no_{action.lower()} "
        f"BEFORE {action} ON {_TRIGGER_TABLE} "
        "BEGIN SELECT RAISE(ABORT, 'beta feedback notice is immutable'); END"
    )


_TRIGGERS = {action: _trigger_ddl(action) for action in ("UPDATE", "DELETE")}


def _normalized(sql: object) -> str:
    return " ".join(sql.upper().split()) if isinstance(sql, str) else ""


class BetaFeedbackConflict(ValueError):
    """Fixed refusal keys only; never customer text or photo bytes."""


@dataclass(frozen=True)
class NoticePage:
    role: str
    photo_ref: str


@dataclass(frozen=True)
class NoticeReceipt:
    notice_id: str
    case_id: str
    owner: str
    request_id: str
    received_at: datetime
    pages: tuple[NoticePage, ...]
    customer_seen_on: str | None
    printed_on: str | None
    stated_deadline_text: str | None
    correction_of: str | None
    needs_confirmation: bool
    review_state: str
    payload_sha256: str


@dataclass(frozen=True)
class NoticeList:
    notices: tuple[NoticeReceipt, ...]
    next_cursor: str | None


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


def _key(value: object, field: str) -> str:
    if not isinstance(value, str) or _KEY.fullmatch(value) is None:
        raise ValueError(f"invalid beta feedback {field}")
    return value


def _stamp(value: object, field: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"invalid beta feedback {field}")
    return value.astimezone(UTC).isoformat()


def _pages(value: object) -> tuple[NoticePage, ...]:
    if not isinstance(value, (tuple, list)) or not 1 <= len(value) <= 20:
        raise ValueError("invalid beta feedback pages")
    result: list[NoticePage] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, NoticePage):
            raise ValueError("invalid beta feedback page")
        role, ref = item.role, item.photo_ref
        if not isinstance(role, str) or role not in _ROLES:
            raise ValueError("invalid beta feedback page role")
        if not isinstance(ref, str) or _REF.fullmatch(ref) is None:
            raise ValueError("invalid beta feedback photo reference")
        if ref in seen:
            raise ValueError("duplicate beta feedback photo reference")
        seen.add(ref)
        result.append(NoticePage(role, ref))
    return tuple(result)


def _date_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid beta feedback {field}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"invalid beta feedback {field}") from None
    if parsed.isoformat() != value:
        raise ValueError(f"invalid beta feedback {field}")
    return value


def _deadline_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 1000:
        raise ValueError("invalid beta feedback stated deadline text")
    return value


def _notice_id(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _NOTICE.fullmatch(value) is None:
        raise ValueError(f"invalid beta feedback {field}")
    return value


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _payload(
    case_id: str,
    owner: str,
    request_id: str,
    pages: tuple[NoticePage, ...],
    customer_seen_on: str | None,
    printed_on: str | None,
    stated_deadline_text: str | None,
    correction_of: str | None,
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "owner": owner,
        "request_id": request_id,
        "pages": [{"role": item.role, "photo_ref": item.photo_ref} for item in pages],
        "customer_seen_on": customer_seen_on,
        "printed_on": printed_on,
        "stated_deadline_text": stated_deadline_text,
        "correction_of": correction_of,
    }


def _digest(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def initialize_beta_feedback(database: sqlite3.Connection) -> None:
    """Install the independent versioned table after the existing beta component."""
    try:
        database.execute("BEGIN IMMEDIATE")
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='beta_feedback'"
        ).fetchone()
        table = database.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='pb_beta_feedback_notices'"
        ).fetchone()
        if version is None:
            if table is not None:
                raise StorageCorruption("unversioned beta feedback schema")
            database.execute(_DDL)
            database.execute("INSERT INTO pb_store_versions VALUES ('beta_feedback', 1)")
            for action in ("UPDATE", "DELETE"):
                database.execute(_TRIGGERS[action])
        elif (
            version != (1,)
            or table is None
            or " ".join(table[0].upper().split()) != " ".join(_DDL.upper().split())
        ):
            raise StorageCorruption("invalid beta feedback schema")
        else:
            for key, action in zip(_TRIGGER_KEYS, ("UPDATE", "DELETE"), strict=True):
                row = database.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                    (f"{_TRIGGER_TABLE}_{key}",),
                ).fetchone()
                if row is None or _normalized(row[0]) != _normalized(_TRIGGERS[action]):
                    raise StorageCorruption("invalid beta feedback immutability trigger")
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def _accepted_beta(database: sqlite3.Connection, case_id: str, owner: str) -> str:
    row = database.execute(
        "SELECT r.case_token, r.workspace_token, c.workspace_token, c.payment_state, "
        "c.amount_cents, o.owner, cw.owner "
        "FROM pb_intake_records r "
        "JOIN pb_intake_owners o ON o.workspace_token=r.workspace_token "
        "LEFT JOIN pb_cases c ON c.case_token=r.case_token "
        "LEFT JOIN pb_intake_owners cw ON cw.workspace_token=c.workspace_token "
        "WHERE r.intake_id=? AND o.owner=?",
        (case_id, owner),
    ).fetchone()
    if row is None:
        raise IntakeConflict("not_found")
    (
        case_token,
        intake_workspace,
        durable_case_workspace,
        payment_state,
        amount_cents,
        intake_owner,
        durable_case_owner,
    ) = row
    if case_token is not None and (
        payment_state != PaymentState.NO_CHARGE.value or amount_cents != 0
    ):
        raise BetaFeedbackConflict("beta_feedback_no_charge_required")
    entitlement = _entitlement(database, case_id)
    if entitlement is None or entitlement.accepted_at is None:
        raise BetaFeedbackConflict("beta_feedback_accepted_beta_required")
    if not isinstance(case_token, str):
        raise StorageCorruption("beta feedback case binding mismatch")
    if (
        entitlement.case_token != case_token
        or intake_workspace != durable_case_workspace
        or intake_owner != owner
        or durable_case_owner != intake_owner
    ):
        raise StorageCorruption("beta feedback case binding mismatch")
    return case_token


def _receipt(row: tuple[object, ...]) -> tuple[int, NoticeReceipt]:
    try:
        (
            sequence,
            case_id,
            owner,
            notice_id,
            request_id,
            received_at,
            payload_json,
            payload_sha256,
            correction_of,
            needs_confirmation,
            review_state,
        ) = row
        if type(sequence) is not int or sequence < 1:
            raise ValueError("invalid stored notice sequence")
        case_id = _key(case_id, "case id")
        owner = _key(owner, "owner")
        request_id = _key(request_id, "request id")
        notice_id = _notice_id(notice_id, "notice id")
        correction_of = _notice_id(correction_of, "correction reference")
        if (
            notice_id is None
            or not isinstance(payload_json, str)
            or not isinstance(payload_sha256, str)
        ):
            raise ValueError("invalid stored notice identity")
        if needs_confirmation != 1 or review_state != "unreviewed":
            raise ValueError("invalid stored notice review state")
        if not isinstance(received_at, str):
            raise ValueError("invalid stored notice timestamp")
        parsed = datetime.fromisoformat(received_at)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("invalid stored notice timestamp")
        if parsed.astimezone(UTC).isoformat() != received_at:
            raise ValueError("stored notice timestamp is not UTC")
        payload = json.loads(
            payload_json, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        if not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS:
            raise ValueError("invalid stored notice payload")
        if (
            payload["case_id"] != case_id
            or payload["owner"] != owner
            or payload["request_id"] != request_id
            or payload["correction_of"] != correction_of
        ):
            raise ValueError("stored notice identity mismatch")
        pages = _pages_from_payload(payload["pages"])
        customer_seen_on = _date_text(payload["customer_seen_on"], "customer seen date")
        printed_on = _date_text(payload["printed_on"], "printed date")
        stated = _deadline_text(payload["stated_deadline_text"])
        if _digest(payload) != payload_sha256:
            raise ValueError("stored notice hash mismatch")
        return sequence, NoticeReceipt(
            notice_id=notice_id,
            case_id=case_id,
            owner=owner,
            request_id=request_id,
            received_at=parsed.astimezone(UTC),
            pages=pages,
            customer_seen_on=customer_seen_on,
            printed_on=printed_on,
            stated_deadline_text=stated,
            correction_of=correction_of,
            needs_confirmation=True,
            review_state="unreviewed",
            payload_sha256=payload_sha256,
        )
    except StorageCorruption:
        raise
    except (TypeError, ValueError, KeyError, OverflowError, RecursionError) as exc:
        raise StorageCorruption("invalid beta feedback notice record") from exc


def _pages_from_payload(value: object) -> tuple[NoticePage, ...]:
    if not isinstance(value, list):
        raise ValueError("invalid stored notice pages")
    raw: list[NoticePage] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"role", "photo_ref"}:
            raise ValueError("invalid stored notice page")
        role, ref = item["role"], item["photo_ref"]
        if not isinstance(role, str) or not isinstance(ref, str):
            raise ValueError("invalid stored notice page")
        raw.append(NoticePage(role, ref))
    return _pages(tuple(raw))


def _encode_cursor(case_id: str, owner: str, sequence: int) -> str:
    raw = _canonical({"case_id": case_id, "owner": owner, "sequence": sequence}).encode()
    return "bfc_" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: object, case_id: str, owner: str) -> int:
    if not isinstance(cursor, str) or not cursor.startswith("bfc_"):
        raise BetaFeedbackConflict("invalid_beta_feedback_cursor")
    try:
        text = cursor[4:]
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if not isinstance(value, dict) or set(value) != {"case_id", "owner", "sequence"}:
            raise ValueError
        sequence = value["sequence"]
        if type(sequence) is not int or sequence < 1:
            raise ValueError
        if value["case_id"] != case_id or value["owner"] != owner:
            raise ValueError
    except (TypeError, ValueError, KeyError, RecursionError):
        raise BetaFeedbackConflict("invalid_beta_feedback_cursor") from None
    return sequence


class BetaFeedbackStoreMixin:
    def record_beta_feedback_notice(
        self: _Connected,
        case_id: str,
        owner: str,
        request_id: str,
        pages: tuple[NoticePage, ...] | list[NoticePage],
        now: datetime,
        *,
        customer_seen_on: str | None = None,
        printed_on: str | None = None,
        stated_deadline_text: str | None = None,
        correction_of: str | None = None,
    ) -> NoticeReceipt:
        """Persist one accepted-beta notice; identical payload replay is exact."""
        case_id = _key(case_id, "case id")
        owner = _key(owner, "owner")
        request_id = _key(request_id, "request id")
        normalized = _pages(pages)
        seen = _date_text(customer_seen_on, "customer seen date")
        printed = _date_text(printed_on, "printed date")
        stated = _deadline_text(stated_deadline_text)
        correction = _notice_id(correction_of, "correction reference")
        received = _stamp(now, "received time")
        payload = _payload(
            case_id, owner, request_id, normalized, seen, printed, stated, correction
        )
        digest = _digest(payload)
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            _accepted_beta(database, case_id, owner)
            existing = database.execute(
                f"SELECT {_COLUMNS} FROM pb_beta_feedback_notices WHERE case_id=? AND request_id=?",
                (case_id, request_id),
            ).fetchone()
            if existing is not None:
                _, receipt = _receipt(existing)
                if receipt.case_id != case_id or receipt.owner != owner:
                    raise StorageCorruption("beta feedback replay identity mismatch")
                if receipt.payload_sha256 != digest:
                    raise BetaFeedbackConflict("beta_feedback_request_conflict")
                database.execute("COMMIT")
                return receipt
            if correction is not None and (
                database.execute(
                    "SELECT 1 FROM pb_beta_feedback_notices "
                    "WHERE case_id=? AND owner=? AND notice_id=?",
                    (case_id, owner, correction),
                ).fetchone()
                is None
            ):
                raise BetaFeedbackConflict("beta_feedback_correction_not_found")
            notice_id = uuid4().hex
            database.execute(
                "INSERT INTO pb_beta_feedback_notices "
                "(case_id, owner, notice_id, request_id, received_at, payload_json, "
                "payload_sha256, correction_of, needs_confirmation, review_state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'unreviewed')",
                (
                    case_id,
                    owner,
                    notice_id,
                    request_id,
                    received,
                    _canonical(payload),
                    digest,
                    correction,
                ),
            )
            row = database.execute(
                f"SELECT {_COLUMNS} FROM pb_beta_feedback_notices WHERE case_id=? AND request_id=?",
                (case_id, request_id),
            ).fetchone()
            if row is None:
                raise StorageCorruption("beta feedback notice disappeared")
            _, receipt = _receipt(row)
            # TODO(T04/P07): Enqueue review with this receipt before commit, keyed by notice ID.
            # Start the acknowledgment clock at receipt even when the deadline is unreadable.
            # A correction should refresh the private brief and retain both source versions.
            database.execute("COMMIT")
            return receipt
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def beta_feedback_notice(
        self: _Connected, case_id: str, owner: str, notice_id: str
    ) -> NoticeReceipt:
        case_id = _key(case_id, "case id")
        owner = _key(owner, "owner")
        validated_notice_id = _notice_id(notice_id, "notice id")
        if validated_notice_id is None:
            raise ValueError("invalid beta feedback notice id")
        database = self._require_open()
        _accepted_beta(database, case_id, owner)
        row = database.execute(
            f"SELECT {_COLUMNS} FROM pb_beta_feedback_notices "
            "WHERE case_id=? AND owner=? AND notice_id=?",
            (case_id, owner, validated_notice_id),
        ).fetchone()
        if row is None:
            raise IntakeConflict("not_found")
        _, receipt = _receipt(row)
        return receipt

    def beta_feedback_notice_cursor(
        self: _Connected, case_id: str, owner: str, notice_id: str
    ) -> str:
        """Translate an owned public last-notice id into the internal sequence cursor.

        Read-only: the notice must already exist for this exact case/owner and its
        stored record must pass the same integrity checks as every other read. No
        admission-expiry or write path is re-run here.
        """
        case_id = _key(case_id, "case id")
        owner = _key(owner, "owner")
        try:
            validated_notice_id = _notice_id(notice_id, "notice id")
        except ValueError:
            raise BetaFeedbackConflict("invalid_beta_feedback_cursor") from None
        if validated_notice_id is None:
            raise BetaFeedbackConflict("invalid_beta_feedback_cursor")
        database = self._require_open()
        row = database.execute(
            f"SELECT {_COLUMNS} FROM pb_beta_feedback_notices "
            "WHERE case_id=? AND owner=? AND notice_id=?",
            (case_id, owner, validated_notice_id),
        ).fetchone()
        if row is None:
            raise IntakeConflict("not_found")
        sequence, _ = _receipt(row)
        return _encode_cursor(case_id, owner, sequence)

    def list_beta_feedback_notices(
        self: _Connected,
        case_id: str,
        owner: str,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> NoticeList:
        """Return durable insertion order, bounded and resumable by an owned cursor."""
        case_id = _key(case_id, "case id")
        owner = _key(owner, "owner")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid beta feedback limit")
        after = 0 if cursor is None else _decode_cursor(cursor, case_id, owner)
        database = self._require_open()
        database.execute("BEGIN")
        try:
            _accepted_beta(database, case_id, owner)
            rows = database.execute(
                f"SELECT {_COLUMNS} FROM pb_beta_feedback_notices "
                "WHERE case_id=? AND owner=? AND sequence>? ORDER BY sequence LIMIT ?",
                (case_id, owner, after, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            decoded = tuple(_receipt(row) for row in rows)
            next_cursor = (
                _encode_cursor(case_id, owner, decoded[-1][0]) if has_more and decoded else None
            )
            database.execute("COMMIT")
            return NoticeList(tuple(item for _, item in decoded), next_cursor)
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
