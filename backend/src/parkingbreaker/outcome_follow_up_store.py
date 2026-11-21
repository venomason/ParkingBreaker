"""Append-only outcome follow-up record: consent, sends and reported outcomes.

One append-only table holds the whole follow-up story for a case: an e-mail consent or
opt-out decision (scoped to the canonical e-mail identity, never the raw address), the
D+14 and D+30 sends, and the reported outcome. Partial unique indexes make the invariants
structural rather than conventional: at most one send per window, one UNKNOWN outcome, and
one append-only history that no UPDATE or DELETE can rewrite.

The outcome kinds keep agency-reported and customer-reported results distinct, and record
non-response as UNKNOWN. No path in this module records a dismissal or a defeat on the
customer's behalf.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final, Protocol

from parkingbreaker.mail_attempt_store import (
    StorageCorruption,
    check_versions_schema,
    rollback_preserving_error,
)

COMPONENT = "outcome_follow_up"
TABLE = "pb_outcome_follow_up_events"
MAX_ACTOR_LENGTH = 120
MAX_REASON_LENGTH = 500
MAX_DETAIL_LENGTH = 2000

CONSENT_KINDS: Final = frozenset({"OPT_IN", "OPT_OUT"})
SEND_WINDOWS: Final = {"D14": "SENT_D14", "D30": "SENT_D30"}
OUTCOME_KINDS: Final = frozenset({"OUTCOME_AGENCY", "OUTCOME_CUSTOMER", "OUTCOME_UNKNOWN"})
_ALL_KINDS: Final = CONSENT_KINDS | frozenset(SEND_WINDOWS.values()) | OUTCOME_KINDS

# The kind list is a closed set enforced by the schema itself.
_DDL = """CREATE TABLE pb_outcome_follow_up_events (
    revision INTEGER PRIMARY KEY CHECK (revision >= 1),
    case_token TEXT,
    email_key TEXT,
    kind TEXT NOT NULL CHECK (kind IN (
        'OPT_IN', 'OPT_OUT', 'SENT_D14', 'SENT_D30',
        'OUTCOME_AGENCY', 'OUTCOME_CUSTOMER', 'OUTCOME_UNKNOWN'
    )),
    detail TEXT NOT NULL,
    command_id TEXT NOT NULL UNIQUE,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    CHECK (
        (kind IN ('OPT_IN', 'OPT_OUT') AND email_key IS NOT NULL AND case_token IS NULL)
        OR (kind NOT IN ('OPT_IN', 'OPT_OUT') AND case_token IS NOT NULL)
    )
)"""

_INDEXES: Final = (
    f"CREATE UNIQUE INDEX {TABLE}_one_d14 ON {TABLE} (case_token) WHERE kind='SENT_D14'",
    f"CREATE UNIQUE INDEX {TABLE}_one_d30 ON {TABLE} (case_token) WHERE kind='SENT_D30'",
    f"CREATE UNIQUE INDEX {TABLE}_one_outcome ON {TABLE} (case_token) "
    "WHERE kind IN ('OUTCOME_AGENCY', 'OUTCOME_CUSTOMER', 'OUTCOME_UNKNOWN')",
)

_COMMAND_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_PRINTABLE = re.compile(r"[^\x00-\x1f\x7f]+")
_COLUMNS = "revision, case_token, email_key, kind, detail, command_id, actor, reason, recorded_at"
_SELECT = f"SELECT {_COLUMNS} FROM {TABLE} ORDER BY revision ASC"
_SELECT_FOR_CASE = f"SELECT {_COLUMNS} FROM {TABLE} WHERE case_token=? ORDER BY revision ASC"


class FollowUpConflict(ValueError):
    """A repeated command, a stale identity, or a state the schema forbids."""


@dataclass(frozen=True)
class FollowUpEvent:
    revision: int
    case_token: str | None
    email_key: str | None
    kind: str
    detail: str
    command_id: str
    actor: str
    reason: str
    recorded_at: datetime
    replayed: bool = False


def canonical_email_key(email: str) -> str:
    """The stable consent key: normalised address, hashed, never stored raw."""
    normalised = email.strip().lower()
    if not normalised or "@" not in normalised or len(normalised) > 320:
        raise FollowUpConflict("invalid email")
    return hashlib.sha256(normalised.encode()).hexdigest()


def _text(value: object, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or _PRINTABLE.fullmatch(value) is None
    ):
        raise FollowUpConflict(f"invalid {field}")
    return value


def _optional_text(value: object, field: str, maximum: int) -> str:
    if value in ("", None):
        return ""
    return _text(value, field, maximum)


def _command_id(value: object) -> str:
    if not isinstance(value, str) or _COMMAND_ID.fullmatch(value) is None:
        raise FollowUpConflict("invalid command_id")
    return value


def _utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FollowUpConflict("invalid recorded time")
    return value.astimezone(UTC)


def _event_from_row(row: tuple[object, ...]) -> FollowUpEvent:
    (
        revision,
        case_token,
        email_key,
        kind,
        detail,
        command_id,
        actor,
        reason,
        recorded_text,
    ) = row
    try:
        if type(revision) is not int or revision < 1:
            raise ValueError("invalid revision")
        if kind not in _ALL_KINDS:
            raise ValueError("invalid kind")
        if (case_token is None) == (email_key is None):
            raise ValueError("exactly one identity required")
        if case_token is not None and not isinstance(case_token, str):
            raise ValueError("invalid case token")
        if email_key is not None and (
            not isinstance(email_key, str) or re.fullmatch(r"[0-9a-f]{64}", email_key) is None
        ):
            raise ValueError("invalid email key")
        if (
            not isinstance(detail, str)
            or len(detail) > MAX_DETAIL_LENGTH
            or not isinstance(command_id, str)
            or _COMMAND_ID.fullmatch(command_id) is None
            or not isinstance(actor, str)
            or not actor
            or len(actor) > MAX_ACTOR_LENGTH
            or not isinstance(reason, str)
            or not reason
            or len(reason) > MAX_REASON_LENGTH
            or not isinstance(recorded_text, str)
        ):
            raise ValueError("invalid operator fields")
        recorded_at = datetime.fromisoformat(recorded_text)
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise ValueError("timestamp is not timezone-aware")
    except (TypeError, ValueError, OverflowError) as exc:
        raise StorageCorruption("invalid stored outcome follow-up event") from exc
    return FollowUpEvent(
        revision, case_token, email_key, kind, detail, command_id, actor, reason, recorded_at
    )


def initialize_outcome_follow_up(database: sqlite3.Connection) -> None:
    """Create or validate the versioned append-only table inside a caller transaction."""
    database.execute("BEGIN IMMEDIATE")
    try:
        check_versions_schema(database)
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component=?", (COMPONENT,)
        ).fetchone()
        table = database.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
        if version is None:
            if table is not None:
                raise StorageCorruption("unversioned outcome follow-up schema")
            database.execute(_DDL)
            for statement in _INDEXES:
                database.execute(statement)
            database.execute("INSERT INTO pb_store_versions VALUES (?, 1)", (COMPONENT,))
        elif (
            type(version[0]) is not int
            or version[0] != 1
            or table is None
            or " ".join(str(table[0]).upper().split()) != " ".join(_DDL.upper().split())
        ):
            raise StorageCorruption("malformed outcome follow-up schema")
        for action in ("UPDATE", "DELETE"):
            database.execute(
                f"CREATE TRIGGER IF NOT EXISTS {TABLE}_no_{action.lower()} "
                f"BEFORE {action} ON {TABLE} "
                "BEGIN SELECT RAISE(ABORT, 'outcome follow-up history is append-only'); END"
            )
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...

    def _follow_up_events(self, case_token: str | None = None) -> list[FollowUpEvent]: ...

    def _append(
        self,
        *,
        case_token: str | None,
        email_key: str | None,
        kind: str,
        detail: str,
        command_id: str,
        actor: str,
        reason: str,
        now: datetime,
    ) -> FollowUpEvent: ...


class OutcomeFollowUpStoreMixin:
    """Reads and the append-only writes for one case's follow-up record."""

    def _follow_up_events(self: _Connected, case_token: str | None = None) -> list[FollowUpEvent]:
        database = self._require_open()
        if case_token is None:
            rows = database.execute(_SELECT).fetchall()
        else:
            rows = database.execute(_SELECT_FOR_CASE, (case_token,)).fetchall()
        return [_event_from_row(tuple(row)) for row in rows]

    # Consent ---------------------------------------------------------------------------
    def set_follow_up_consent(
        self: _Connected,
        email: str,
        allowed: bool,
        command_id: str,
        actor: str,
        reason: str = "customer choice",
        *,
        now: datetime,
    ) -> FollowUpEvent:
        """Record an opt-in or opt-out for the canonical e-mail identity."""
        if not isinstance(allowed, bool):
            raise FollowUpConflict("invalid consent flag")
        return self._append(
            case_token=None,
            email_key=canonical_email_key(email),
            kind="OPT_IN" if allowed else "OPT_OUT",
            detail="",
            command_id=command_id,
            actor=actor,
            reason=reason,
            now=now,
        )

    def follow_up_allowed(self: _Connected, email: str) -> bool:
        """Strictly the latest recorded choice; no record means allowed (transactional)."""
        key = canonical_email_key(email)
        database = self._require_open()
        row = database.execute(
            "SELECT kind FROM " + TABLE + " WHERE email_key=? ORDER BY revision DESC LIMIT 1",
            (key,),
        ).fetchone()
        return True if row is None else row[0] == "OPT_IN"

    # Sends -----------------------------------------------------------------------------
    def record_follow_up_send(
        self: _Connected,
        case_token: str,
        window: str,
        command_id: str,
        actor: str,
        reason: str = "scheduled follow-up",
        *,
        now: datetime,
    ) -> FollowUpEvent:
        """Record one send for a window; a second send for the same window refuses."""
        kind = SEND_WINDOWS.get(window)
        if kind is None:
            raise FollowUpConflict("unknown follow-up window")
        return self._append(
            case_token=case_token,
            email_key=None,
            kind=kind,
            detail="",
            command_id=command_id,
            actor=actor,
            reason=reason,
            now=now,
        )

    def follow_up_sent(self: _Connected, case_token: str, window: str) -> bool:
        kind = SEND_WINDOWS.get(window)
        if kind is None:
            raise FollowUpConflict("unknown follow-up window")
        database = self._require_open()
        row = database.execute(
            "SELECT 1 FROM " + TABLE + " WHERE case_token=? AND kind=? LIMIT 1",
            (case_token, kind),
        ).fetchone()
        return row is not None

    # Outcomes --------------------------------------------------------------------------
    def record_outcome(
        self: _Connected,
        case_token: str,
        kind: str,
        detail: str,
        command_id: str,
        actor: str,
        reason: str = "reported outcome",
        *,
        now: datetime,
    ) -> FollowUpEvent:
        """Record an agency-reported or customer-reported outcome, once."""
        if kind not in {"OUTCOME_AGENCY", "OUTCOME_CUSTOMER"}:
            raise FollowUpConflict("outcome must be agency or customer reported")
        return self._append(
            case_token=case_token,
            email_key=None,
            kind=kind,
            detail=_optional_text(detail, "detail", MAX_DETAIL_LENGTH),
            command_id=command_id,
            actor=actor,
            reason=reason,
            now=now,
        )

    def record_unknown_outcome(
        self: _Connected,
        case_token: str,
        command_id: str,
        actor: str,
        reason: str = "no response after the final window",
        *,
        now: datetime,
    ) -> FollowUpEvent:
        """Record non-response as UNKNOWN; never as a dismissal or a defeat."""
        return self._append(
            case_token=case_token,
            email_key=None,
            kind="OUTCOME_UNKNOWN",
            detail="",
            command_id=command_id,
            actor=actor,
            reason=reason,
            now=now,
        )

    def outcome_for(self: _Connected, case_token: str) -> FollowUpEvent | None:
        for event in self._follow_up_events(case_token):
            if event.kind in OUTCOME_KINDS:
                return event
        return None

    # Write -----------------------------------------------------------------------------
    def _append(
        self: _Connected,
        *,
        case_token: str | None,
        email_key: str | None,
        kind: str,
        detail: str,
        command_id: str,
        actor: str,
        reason: str,
        now: datetime,
    ) -> FollowUpEvent:
        named_actor = _text(actor, "actor", MAX_ACTOR_LENGTH)
        explanation = _text(reason, "reason", MAX_REASON_LENGTH)
        command = _command_id(command_id)
        recorded_at = _utc(now)
        database = self._require_open()
        database.execute("BEGIN IMMEDIATE")
        try:
            existing = database.execute(
                "SELECT revision, case_token, email_key, kind, detail, command_id, actor, "
                f"reason, recorded_at FROM {TABLE} WHERE command_id=?",
                (command,),
            ).fetchone()
            if existing is not None:
                saved = _event_from_row(tuple(existing))
                if (
                    saved.kind == kind
                    and saved.case_token == case_token
                    and saved.email_key == email_key
                    and saved.detail == detail
                    and saved.actor == named_actor
                    and saved.reason == explanation
                ):
                    database.execute("COMMIT")
                    return replace(saved, replayed=True)
                raise FollowUpConflict("command_id_payload_changed")
            revision_row = database.execute(
                f"SELECT COALESCE(MAX(revision), 0) FROM {TABLE}"
            ).fetchone()
            if revision_row is None:
                raise StorageCorruption("missing follow-up revision")
            revision = int(revision_row[0]) + 1
            database.execute(
                f"INSERT INTO {TABLE} "
                "(revision, case_token, email_key, kind, detail, command_id, actor, reason, "
                "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision,
                    case_token,
                    email_key,
                    kind,
                    detail,
                    command,
                    named_actor,
                    explanation,
                    recorded_at.isoformat(),
                ),
            )
            saved_row = database.execute(
                "SELECT revision, case_token, email_key, kind, detail, command_id, actor, "
                f"reason, recorded_at FROM {TABLE} WHERE revision=?",
                (revision,),
            ).fetchone()
            if saved_row is None:
                raise StorageCorruption("missing saved outcome follow-up event")
            event = _event_from_row(tuple(saved_row))
            database.execute("COMMIT")
            return event
        except sqlite3.IntegrityError as exc:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise FollowUpConflict("follow_up_already_recorded") from exc
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
