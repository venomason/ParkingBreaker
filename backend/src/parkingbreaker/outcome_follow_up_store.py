"""Append-only outcome follow-up record: consent, sends and reported outcomes.

One append-only table holds e-mail consent (scoped to a hashed address), the D+14 and
D+30 sends, and reported outcomes. First-class pilot reports can update routing,
acknowledgment and disposition independently without changing the outcome record.

The outcome kinds keep agency-reported and customer-reported results distinct, and record
non-response as UNKNOWN. No path in this module records a dismissal or a defeat on the
customer's behalf.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Protocol

from parkingbreaker.lob_submission import Accepted, Canceled, Uncertain, UspsScan
from parkingbreaker.lob_webhook import USPS_SCAN_NAMES
from parkingbreaker.mail_attempt_store import (
    PreparedMailAttempt,
    StorageCorruption,
    check_versions_schema,
    rollback_preserving_error,
)
from parkingbreaker.mail_config import MailClass
from parkingbreaker.submission_records import SubmissionCursor

COMPONENT = "outcome_follow_up"
TABLE = "pb_outcome_follow_up_events"
MAX_ACTOR_LENGTH = 120
MAX_REASON_LENGTH = 500
MAX_DETAIL_LENGTH = 2000

CONSENT_KINDS: Final = frozenset({"OPT_IN", "OPT_OUT"})
SEND_WINDOWS: Final = {"D14": "SENT_D14", "D30": "SENT_D30"}
OUTCOME_KINDS: Final = frozenset({"OUTCOME_AGENCY", "OUTCOME_CUSTOMER", "OUTCOME_UNKNOWN"})
_PILOT_KINDS: Final = frozenset({"PILOT_FEEDBACK", "PILOT_SEND_STARTED"})
_ALL_KINDS: Final = CONSENT_KINDS | frozenset(SEND_WINDOWS.values()) | OUTCOME_KINDS | _PILOT_KINDS

# The kind list is a closed set enforced by the schema itself.
_DDL_V1 = """CREATE TABLE pb_outcome_follow_up_events (
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
_DDL = _DDL_V1.replace(
    "'OUTCOME_AGENCY', 'OUTCOME_CUSTOMER', 'OUTCOME_UNKNOWN'",
    "'OUTCOME_AGENCY', 'OUTCOME_CUSTOMER', 'OUTCOME_UNKNOWN',\n"
    "        'PILOT_FEEDBACK', 'PILOT_SEND_STARTED'",
)

_INDEXES: Final = (
    f"CREATE UNIQUE INDEX {TABLE}_one_d14 ON {TABLE} (case_token) WHERE kind='SENT_D14'",
    f"CREATE UNIQUE INDEX {TABLE}_one_d30 ON {TABLE} (case_token) WHERE kind='SENT_D30'",
    f"CREATE UNIQUE INDEX {TABLE}_one_outcome ON {TABLE} (case_token) "
    "WHERE kind IN ('OUTCOME_AGENCY', 'OUTCOME_CUSTOMER', 'OUTCOME_UNKNOWN')",
    f"CREATE INDEX {TABLE}_pilot_updates ON {TABLE} (case_token, revision) "
    "WHERE kind='PILOT_FEEDBACK'",
)

_COMMAND_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_PILOT_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}")
_PRINTABLE = re.compile(r"[^\x00-\x1f\x7f]+")
_COLUMNS = "revision, case_token, email_key, kind, detail, command_id, actor, reason, recorded_at"
_SELECT = f"SELECT {_COLUMNS} FROM {TABLE} ORDER BY revision ASC"
_SELECT_FOR_CASE = f"SELECT {_COLUMNS} FROM {TABLE} WHERE case_token=? ORDER BY revision ASC"
_PILOT_CASE = """c.payment_state='NO_CHARGE' AND c.amount_cents=0 AND EXISTS (
    SELECT 1 FROM pb_beta_entitlements e JOIN pb_beta_campaigns p USING (campaign_id)
    WHERE e.case_token=c.case_token AND e.accepted_at IS NOT NULL AND p.pilot_issuer_id IS NOT NULL
) AND EXISTS (
    SELECT 1 FROM pb_mail_attempts a WHERE a.case_token=c.case_token AND a.attempt=1
    AND CASE WHEN json_valid(a.envelope_json)
        THEN json_extract(a.envelope_json, '$.options.mail_class') END = 'first_class'
)"""


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


class PilotRouting(StrEnum):
    RETURNED = "returned"
    REDIRECTED = "redirected"
    WRONG_OFFICE = "wrong_office"
    NO_KNOWN_PROBLEM = "no_known_problem"
    UNKNOWN = "unknown"


class PilotAcknowledgment(StrEnum):
    ACKNOWLEDGED = "acknowledged"
    MORE_INFO_REQUESTED = "more_info_requested"
    PENDING = "pending"
    UNKNOWN = "unknown"


class PilotDisposition(StrEnum):
    DISMISSED = "dismissed"
    REDUCED = "reduced"
    UPHELD = "upheld"
    FILING_REJECTED = "filing_rejected"
    PENDING = "pending"


@dataclass(frozen=True)
class PilotFeedback:
    eligible: bool = False
    revision: int = 0
    first_postal_scan_at: datetime | None = None
    printer_handoff_at: datetime | None = None
    routing: str | None = None
    acknowledgment: str | None = None
    disposition: str | None = None
    updated_at: datetime | None = None


_PILOT_FIELDS: Final = {
    "routing": PilotRouting,
    "acknowledgment": PilotAcknowledgment,
    "disposition": PilotDisposition,
}


def _pilot_patch(values: Mapping[str, object]) -> dict[str, str]:
    if not values:
        raise FollowUpConflict("pilot_feedback_empty")
    validated = {}
    for name, value in values.items():
        field = _PILOT_FIELDS.get(name)
        if field is None or not isinstance(value, str):
            raise FollowUpConflict("pilot_feedback_invalid")
        try:
            validated[name] = field(value).value
        except ValueError:
            raise FollowUpConflict("pilot_feedback_invalid") from None
    return validated


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
    """Create or migrate the versioned append-only table in one transaction."""
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
        elif version not in {(1,), (2,)} or table is None:
            raise StorageCorruption("malformed outcome follow-up schema")
        else:
            expected = _DDL_V1 if version == (1,) else _DDL
            if " ".join(str(table[0]).upper().split()) != " ".join(expected.upper().split()):
                raise StorageCorruption("malformed outcome follow-up schema")
        if version == (1,):
            database.execute(f"ALTER TABLE {TABLE} RENAME TO {TABLE}_v1")
        if version != (2,):
            database.execute(_DDL)
            if version == (1,):
                database.execute(f"INSERT INTO {TABLE} SELECT {_COLUMNS} FROM {TABLE}_v1")
                database.execute(f"DROP TABLE {TABLE}_v1")
            for statement in _INDEXES:
                database.execute(statement)
            database.execute(
                "INSERT INTO pb_store_versions VALUES (?, 2) "
                "ON CONFLICT(component) DO UPDATE SET version=excluded.version",
                (COMPONENT,),
            )
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


class FollowUpStoreProtocol(Protocol):
    """The slice of the store the follow-up service needs."""

    def follow_up_consent_state(self, email: str) -> tuple[bool | None, int]: ...

    def delivered_cases(self, limit: int = 100) -> list[tuple[str, datetime]]: ...

    def pilot_follow_up_candidates(self) -> tuple[str, ...]: ...

    def is_pilot_case(self, case_token: str) -> bool: ...

    def pilot_feedback(self, case_token: str) -> PilotFeedback: ...

    def record_pilot_feedback(
        self,
        case_token: str,
        request_id: str,
        expected_revision: int,
        values: Mapping[str, object],
        *,
        now: datetime,
    ) -> PilotFeedback: ...

    def start_pilot_follow_up(
        self, case_token: str, window: str, *, now: datetime
    ) -> FollowUpEvent: ...

    def intake_json_for_case(self, case_token: str) -> str | None: ...

    def follow_up_allowed(self, email: str) -> bool: ...

    def follow_up_sent(self, case_token: str, window: str) -> bool: ...

    def record_follow_up_send(
        self,
        case_token: str,
        window: str,
        command_id: str,
        actor: str,
        reason: str = ...,
        *,
        now: datetime,
    ) -> FollowUpEvent: ...

    def set_follow_up_consent(
        self,
        email: str,
        allowed: bool,
        command_id: str,
        actor: str,
        reason: str = ...,
        *,
        now: datetime,
    ) -> FollowUpEvent: ...

    def record_outcome(
        self,
        case_token: str,
        kind: str,
        detail: str,
        command_id: str,
        actor: str,
        reason: str = ...,
        *,
        now: datetime,
    ) -> FollowUpEvent: ...

    def outcome_for(self, case_token: str) -> FollowUpEvent | None: ...

    def record_unknown_outcome(
        self,
        case_token: str,
        command_id: str,
        actor: str,
        reason: str = ...,
        *,
        now: datetime,
    ) -> FollowUpEvent: ...


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...

    def _follow_up_events(self, case_token: str | None = None) -> list[FollowUpEvent]: ...

    def get(self, case_token: str, attempt: int) -> PreparedMailAttempt | None: ...

    def usps_scans(self, case_id: str, attempt: int = 1) -> tuple[UspsScan, ...]: ...

    def submission_cursor(self, case_id: str, attempt: int) -> SubmissionCursor | None: ...

    def tracking_payloads(self, case_id: str) -> tuple[str, ...]: ...

    def is_pilot_case(self, case_token: str) -> bool: ...

    def pilot_feedback(self, case_token: str) -> PilotFeedback: ...

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
        expected_pilot_revision: int | None = None,
    ) -> FollowUpEvent: ...


def _date_from_observation(value: object, prepared_at: datetime) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        when = _utc(datetime.fromisoformat(value))
    except (ValueError, OverflowError):
        return None
    return when if when >= prepared_at else None


def _pilot_postal_evidence(store: _Connected, case_token: str) -> PilotFeedback:
    if not store.is_pilot_case(case_token):
        return PilotFeedback()
    prepared = store.get(case_token, 1)
    if prepared is None or prepared.request.options.mail_class is not MailClass.FIRST_CLASS:
        return PilotFeedback()
    cursor = store.submission_cursor(case_token, 1)
    outcome = None if cursor is None else cursor.outcome
    if not isinstance(outcome, Accepted | Uncertain | Canceled) or outcome.letter_id is None:
        return PilotFeedback()
    if isinstance(outcome, Canceled):
        return PilotFeedback(eligible=True)
    scans = (
        scan.time
        for scan in store.usps_scans(case_token, 1)
        if scan.event_type == "normal" and scan.name in USPS_SCAN_NAMES
    )
    scan_times = (
        parsed
        for timestamp in scans
        if (parsed := _date_from_observation(timestamp, prepared.prepared_at)) is not None
    )
    handoffs = []
    for payload in store.tracking_payloads(case_token):
        data = json.loads(payload)
        if (
            data.get("event") != "letter.mailed"
            or data.get("attempt") != 1
            or data.get("letter_id") != outcome.letter_id
        ):
            continue
        when = _date_from_observation(data.get("pilot_printer_handoff_at"), prepared.prepared_at)
        if when is not None:
            handoffs.append(when)
    return PilotFeedback(
        eligible=True,
        first_postal_scan_at=min(scan_times, default=None),
        printer_handoff_at=min(handoffs, default=None),
    )


class OutcomeFollowUpStoreMixin:
    """Reads and the append-only writes for one case's follow-up record."""

    def _follow_up_events(self: _Connected, case_token: str | None = None) -> list[FollowUpEvent]:
        database = self._require_open()
        if case_token is None:
            rows = database.execute(_SELECT).fetchall()
        else:
            rows = database.execute(_SELECT_FOR_CASE, (case_token,)).fetchall()
        return [_event_from_row(tuple(row)) for row in rows]

    # Discovery -------------------------------------------------------------------------
    def delivered_cases(self: _Connected, limit: int = 100) -> list[tuple[str, datetime]]:
        """Delivered cases without a recorded outcome, oldest delivery first."""
        if type(limit) is not int or limit < 1:
            raise FollowUpConflict("invalid limit")
        rows = (
            self._require_open()
            .execute(
                "SELECT c.case_token, MIN(h.occurred_at) AS delivered_at FROM pb_cases c "
                "JOIN pb_case_history h ON h.case_token = c.case_token "
                "WHERE c.letter_state = 'DELIVERED' AND h.event_name = 'DeliveryProofStored' "
                f"AND NOT ({_PILOT_CASE}) "
                f"AND NOT EXISTS (SELECT 1 FROM {TABLE} e WHERE e.case_token = c.case_token "
                "AND e.kind IN ('OUTCOME_AGENCY', 'OUTCOME_CUSTOMER', 'OUTCOME_UNKNOWN')) "
                "GROUP BY c.case_token "
                "ORDER BY delivered_at ASC, c.case_token ASC LIMIT ?",
                (limit,),
            )
            .fetchall()
        )
        delivered: list[tuple[str, datetime]] = []
        for case_token, occurred_text in rows:
            try:
                occurred = datetime.fromisoformat(str(occurred_text))
                if occurred.tzinfo is None or occurred.utcoffset() is None:
                    raise ValueError("delivery time is not timezone-aware")
            except (TypeError, ValueError, OverflowError) as exc:
                raise StorageCorruption("invalid stored delivery time") from exc
            delivered.append((str(case_token), occurred.astimezone(UTC)))
        return delivered

    def intake_json_for_case(self: _Connected, case_token: str) -> str | None:
        """The customer's submitted intake JSON for a case, if the case has one."""
        row = (
            self._require_open()
            .execute(
                "SELECT r.intake_json FROM pb_intake_records r "
                "JOIN pb_cases c ON c.workspace_token = r.workspace_token "
                "WHERE c.case_token=?",
                (case_token,),
            )
            .fetchone()
        )
        return None if row is None else str(row[0])

    def is_pilot_case(self: _Connected, case_token: str) -> bool:
        row = (
            self._require_open()
            .execute(
                f"SELECT 1 FROM pb_cases c WHERE c.case_token=? AND ({_PILOT_CASE})", (case_token,)
            )
            .fetchone()
        )
        return row is not None

    def pilot_follow_up_candidates(self: _Connected) -> tuple[str, ...]:
        """First-class no-charge cases with at least one reminder still unsent."""
        rows = self._require_open().execute(
            f"SELECT c.case_token FROM pb_cases c WHERE ({_PILOT_CASE}) "
            f"AND NOT EXISTS (SELECT 1 FROM {TABLE} e WHERE e.case_token=c.case_token "
            "AND e.kind='SENT_D30') "
            "ORDER BY c.authorized_at, c.case_token"
        )
        return tuple(str(row[0]) for row in rows)

    def pilot_feedback(self: _Connected, case_token: str) -> PilotFeedback:
        evidence = _pilot_postal_evidence(self, case_token)
        if not evidence.eligible:
            return evidence
        values: dict[str, str] = {}
        updated_at = None
        revision = 0
        rows = self._require_open().execute(
            f"SELECT {_COLUMNS} FROM {TABLE} WHERE case_token=? AND kind='PILOT_FEEDBACK' "
            "ORDER BY revision",
            (case_token,),
        )
        for row in rows:
            event = _event_from_row(tuple(row))
            try:
                payload = json.loads(event.detail)
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"expected_revision", "values"}
                    or type(payload["expected_revision"]) is not int
                    or payload["expected_revision"] != revision
                    or not isinstance(payload["values"], dict)
                ):
                    raise ValueError("invalid pilot feedback revision")
                values.update(_pilot_patch(payload["values"]))
            except ValueError as exc:
                raise StorageCorruption("invalid stored pilot feedback") from exc
            updated_at = event.recorded_at
            revision += 1
        return replace(
            evidence,
            revision=revision,
            routing=values.get("routing"),
            acknowledgment=values.get("acknowledgment"),
            disposition=values.get("disposition"),
            updated_at=updated_at,
        )

    def record_pilot_feedback(
        self: _Connected,
        case_token: str,
        request_id: str,
        expected_revision: int,
        values: Mapping[str, object],
        *,
        now: datetime,
    ) -> PilotFeedback:
        if not isinstance(request_id, str) or _PILOT_REQUEST_ID.fullmatch(request_id) is None:
            raise FollowUpConflict("invalid_pilot_feedback_request")
        if type(expected_revision) is not int or expected_revision < 0:
            raise FollowUpConflict("invalid_pilot_feedback_revision")
        patch = _pilot_patch(values)
        digest = hashlib.sha256(f"{case_token}\0{request_id}".encode()).hexdigest()
        self._append(
            case_token=case_token,
            email_key=None,
            kind="PILOT_FEEDBACK",
            detail=json.dumps(
                {"expected_revision": expected_revision, "values": patch},
                sort_keys=True,
                separators=(",", ":"),
            ),
            command_id=f"pilot-feedback:{digest}",
            actor="customer",
            reason="customer reported an ordinary-post update",
            now=now,
            expected_pilot_revision=expected_revision,
        )
        return self.pilot_feedback(case_token)

    def start_pilot_follow_up(
        self: _Connected, case_token: str, window: str, *, now: datetime
    ) -> FollowUpEvent:
        """Persist the first attempt so a crash cannot extend provider idempotency."""
        if window not in SEND_WINDOWS:
            raise FollowUpConflict("unknown follow-up window")
        return self._append(
            case_token=case_token,
            email_key=None,
            kind="PILOT_SEND_STARTED",
            detail=window,
            command_id=f"pilot-start:{window}:{case_token}",
            actor="worker",
            reason="scheduled ordinary-post reminder attempt",
            now=now,
        )

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

    def follow_up_consent_state(self: _Connected, email: str) -> tuple[bool | None, int]:
        """The latest consent value and its revision; absent means never recorded."""
        key = canonical_email_key(email)
        row = (
            self._require_open()
            .execute(
                "SELECT kind, revision FROM " + TABLE + " WHERE email_key=? "
                "ORDER BY revision DESC LIMIT 1",
                (key,),
            )
            .fetchone()
        )
        if row is None:
            return None, 0
        return str(row[0]) == "OPT_IN", int(row[1])

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
        expected_pilot_revision: int | None = None,
    ) -> FollowUpEvent:
        named_actor = _text(actor, "actor", MAX_ACTOR_LENGTH)
        explanation = _text(reason, "reason", MAX_REASON_LENGTH)
        command = _command_id(command_id)
        recorded_at = _utc(now)
        database = self._require_open()
        database.execute("BEGIN IMMEDIATE")
        try:
            if kind in _PILOT_KINDS and (
                case_token is None or not _pilot_postal_evidence(self, case_token).eligible
            ):
                raise FollowUpConflict("pilot_feedback_not_eligible")
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
            if kind == "PILOT_FEEDBACK":
                (current_revision,) = database.execute(
                    f"SELECT COUNT(*) FROM {TABLE} WHERE case_token=? AND kind='PILOT_FEEDBACK'",
                    (case_token,),
                ).fetchone()
                if current_revision != expected_pilot_revision:
                    raise FollowUpConflict("pilot_feedback_revision_changed")
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
