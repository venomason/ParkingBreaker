"""Append-only SQLite journey event ledger and typed internal reconstruction.

This module implements the first production slice of ParkingBreaker's quantified
funnel telemetry: one durable SQLite ledger sharing the authoritative transaction
connection. Events are enqueued inside the same SQLite transaction as the
authoritative business transition, before COMMIT.

Privacy & boundary guarantees:
- Restricted workspace linkage (``pb_journey_links``) and deduplication digests
  never appear in the public ``JourneyEvent`` / ``JourneyEventPage`` projections.
- Closed enums, bounded integers, explicit cost status, and UTC-aware timestamps
  only; no free-text narratives, customer identifiers, provider IDs, exception
  messages, or arbitrary metadata are accepted or stored.
- Reading requires an explicit internal scope (one ``journey_id`` or one
  ``workspace_token``, optionally checked against ``pb_intake_owners``) and
  bounded keyset pagination. No unauthenticated HTTP or external export exists.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from parkingbreaker.journey_context import (
    ContextFunding,
    ContextOperation,
    ContextReason,
    JourneyEventContext,
    JourneyReference,
    ReferenceState,
    decode_context,
    initialize_journey_event_context,
)
from parkingbreaker.journey_context import (
    JourneyBuildIdentity as JourneyBuildIdentity,
)
from parkingbreaker.mail_attempt_store import (
    StorageCorruption,
    check_versions_schema,
    rollback_preserving_error,
)

COMPONENT: Final[str] = "journey_events"
SCHEMA_VERSION: Final[int] = 1
DEFAULT_PAGE_SIZE: Final[int] = 100
MAX_PAGE_SIZE: Final[int] = 100
MAX_DURATION_MS: Final[int] = 10**13
MAX_COST_CENTS: Final[int] = 100_000_000
MAX_TOKEN_LENGTH: Final[int] = 128
MAX_OCCURRENCE_KEY_LENGTH: Final[int] = 512

EVENT_ID_PATTERN: Final[str] = r"^evt_[0-9a-f]{32}$"
JOURNEY_ID_PATTERN: Final[str] = r"^jrn_[0-9a-f]{32}$"
_EVENT_ID_RE: Final[re.Pattern[str]] = re.compile(EVENT_ID_PATTERN)
_JOURNEY_ID_RE: Final[re.Pattern[str]] = re.compile(JOURNEY_ID_PATTERN)
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_OCCURRENCE_DOMAIN: Final[bytes] = b"pb-journey-occurrence-v1\x00"

_LINKS_DDL: Final[str] = """CREATE TABLE pb_journey_links (
    workspace_token TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    CHECK (length(workspace_token) BETWEEN 1 AND 128),
    CHECK (length(journey_id) = 36 AND journey_id GLOB 'jrn_*')
)"""

_EVENTS_DDL: Final[str] = """CREATE TABLE pb_journey_events (
    event_id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES pb_journey_links(journey_id),
    seq INTEGER NOT NULL CHECK (seq >= 1),
    occurrence_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(occurrence_sha256) = 64
            AND occurrence_sha256 NOT GLOB '*[^0-9a-f]*'),
    content_sha256 TEXT NOT NULL
        CHECK (length(content_sha256) = 64
            AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
    kind TEXT NOT NULL CHECK (kind IN (
        'ticket_read',
        'ticket_read_refused',
        'ticket_read_unknown',
        'facts_confirmed',
        'intake_submitted',
        'hold_authorized',
        'packet_approved'
    )),
    outcome TEXT NOT NULL CHECK (outcome IN ('succeeded', 'refused', 'unknown')),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    cost_cents INTEGER CHECK (cost_cents IS NULL OR cost_cents >= 0),
    cost_status TEXT NOT NULL CHECK (cost_status IN ('known', 'unknown', 'not_applicable')),
    UNIQUE (journey_id, seq),
    CHECK (length(event_id) = 36 AND event_id GLOB 'evt_*'),
    CHECK ((cost_status = 'known' AND cost_cents IS NOT NULL)
        OR (cost_status IN ('unknown', 'not_applicable') AND cost_cents IS NULL))
)"""

_DDL: Final[tuple[tuple[str, str], ...]] = (
    ("pb_journey_links", _LINKS_DDL),
    ("pb_journey_events", _EVENTS_DDL),
)


CAPTURE_COMPONENT: Final[str] = "journey_capture"
CAPTURE_VERSION: Final[int] = 1
_CAPTURE_DDL: Final[str] = """CREATE TABLE pb_journey_capture (
    journey_id TEXT PRIMARY KEY REFERENCES pb_journey_links(journey_id),
    started_at TEXT,
    start_basis TEXT NOT NULL CHECK (start_basis IN (
        'workspace_created', 'first_durable_attempt', 'first_durable_event', 'legacy_unknown'
    )),
    build_sha256 TEXT CHECK (build_sha256 IS NULL OR
        (length(build_sha256) = 64 AND build_sha256 NOT GLOB '*[^0-9a-f]*')),
    config_sha256 TEXT CHECK (config_sha256 IS NULL OR
        (length(config_sha256) = 64 AND config_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK ((start_basis = 'legacy_unknown' AND started_at IS NULL)
        OR (start_basis != 'legacy_unknown' AND started_at IS NOT NULL))
)"""


class CaptureStartBasis(StrEnum):
    WORKSPACE_CREATED = "workspace_created"
    FIRST_DURABLE_ATTEMPT = "first_durable_attempt"
    FIRST_DURABLE_EVENT = "first_durable_event"
    LEGACY_UNKNOWN = "legacy_unknown"


@dataclass(frozen=True)
class JourneyCapture:
    """Recorded capture start, not a claim of complete event or attempt coverage."""

    journey_id: str
    started_at: datetime | None
    start_basis: CaptureStartBasis
    identity: JourneyBuildIdentity


def _initialize_journey_capture(database: sqlite3.Connection) -> None:
    # Invoked inside the ledger initializer transaction. Never infer old capture
    # start from a workspace/event timestamp or assign this build to historical rows.
    version = database.execute(
        "SELECT version FROM pb_store_versions WHERE component=?", (CAPTURE_COMPONENT,)
    ).fetchone()
    table = database.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='pb_journey_capture'"
    ).fetchone()
    if version is None:
        if table is not None:
            raise StorageCorruption("unversioned journey capture schema")
        database.execute(_CAPTURE_DDL)
        database.execute(
            "INSERT INTO pb_journey_capture (journey_id, started_at, start_basis) "
            "SELECT journey_id, NULL, 'legacy_unknown' FROM pb_journey_links"
        )
        database.execute(
            "INSERT INTO pb_store_versions VALUES (?, ?)", (CAPTURE_COMPONENT, CAPTURE_VERSION)
        )
    elif (
        version != (CAPTURE_VERSION,)
        or table is None
        or " ".join(table[0].upper().split()) != " ".join(_CAPTURE_DDL.upper().split())
    ):
        raise StorageCorruption("malformed journey capture schema")
    # A missing row is corruption, never permission to backfill modern provenance.
    if (
        database.execute(
            "SELECT 1 FROM pb_journey_links l LEFT JOIN pb_journey_capture c "
            "ON c.journey_id=l.journey_id WHERE c.journey_id IS NULL LIMIT 1"
        ).fetchone()
        is not None
    ):
        raise StorageCorruption("missing journey capture provenance")
    for action in ("UPDATE", "DELETE"):
        database.execute(
            f"CREATE TRIGGER IF NOT EXISTS pb_journey_capture_no_{action.lower()} "
            f"BEFORE {action} ON pb_journey_capture "
            "BEGIN SELECT RAISE(ABORT, 'journey capture is immutable'); END"
        )


def ensure_journey_identity_in_transaction(
    database: _ConnectionLike,
    *,
    workspace_token: str,
    at: datetime,
    basis: CaptureStartBasis,
    identity: JourneyBuildIdentity | None = None,
) -> str:
    """Allocate identity and capture together with the caller's durable creation."""
    if not database.in_transaction:
        raise StorageCorruption("journey identity requires active transaction")
    workspace = _validate_workspace_token(workspace_token)
    at = _to_utc(at, "capture start")
    if type(basis) is not CaptureStartBasis or basis is CaptureStartBasis.LEGACY_UNKNOWN:
        raise ValueError("new capture requires an observed start basis")
    if identity is not None and type(identity) is not JourneyBuildIdentity:
        raise ValueError("invalid journey build identity")
    _assert_initialized(database)
    row = database.execute(
        "SELECT journey_id FROM pb_journey_links WHERE workspace_token=?", (workspace,)
    ).fetchone()
    if row is not None:
        if type(row[0]) is not str or _JOURNEY_ID_RE.fullmatch(row[0]) is None:
            raise StorageCorruption("malformed stored journey link")
        if (
            database.execute(
                "SELECT 1 FROM pb_journey_capture WHERE journey_id=?", (row[0],)
            ).fetchone()
            is None
        ):
            raise StorageCorruption("missing journey capture provenance")
        return row[0]
    resolved = identity or JourneyBuildIdentity()
    journey_id = f"jrn_{uuid.uuid4().hex}"
    database.execute(
        "INSERT INTO pb_journey_links VALUES (?, ?, ?)",
        (workspace, journey_id, at.isoformat()),
    )
    database.execute(
        "INSERT INTO pb_journey_capture VALUES (?, ?, ?, ?, ?)",
        (journey_id, at.isoformat(), basis.value, resolved.build_sha256, resolved.config_sha256),
    )
    return journey_id


class JourneyEventConflict(ValueError):
    """Stable refusal code when an occurrence key replays with conflicting content."""


class JourneyEventAccessError(PermissionError):
    """Stable refusal code when an internal workspace/journey authorization check fails."""


class JourneyEventKind(StrEnum):
    """Closed semantic funnel events recorded by this telemetry slice."""

    TICKET_READ = "ticket_read"
    TICKET_READ_REFUSED = "ticket_read_refused"
    TICKET_READ_UNKNOWN = "ticket_read_unknown"
    FACTS_CONFIRMED = "facts_confirmed"
    INTAKE_SUBMITTED = "intake_submitted"
    HOLD_AUTHORIZED = "hold_authorized"
    PACKET_APPROVED = "packet_approved"


class JourneyEventOutcome(StrEnum):
    """Closed outcome classification for a journey event."""

    SUCCEEDED = "succeeded"
    REFUSED = "refused"
    UNKNOWN = "unknown"


class CostStatus(StrEnum):
    """Explicit cost measurement status; unknown cost is always null + unknown."""

    KNOWN = "known"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


_ALLOWED_OUTCOMES: Final[dict[JourneyEventKind, frozenset[JourneyEventOutcome]]] = {
    JourneyEventKind.TICKET_READ: frozenset({JourneyEventOutcome.SUCCEEDED}),
    JourneyEventKind.TICKET_READ_REFUSED: frozenset({JourneyEventOutcome.REFUSED}),
    JourneyEventKind.TICKET_READ_UNKNOWN: frozenset({JourneyEventOutcome.UNKNOWN}),
    JourneyEventKind.FACTS_CONFIRMED: frozenset({JourneyEventOutcome.SUCCEEDED}),
    JourneyEventKind.INTAKE_SUBMITTED: frozenset({JourneyEventOutcome.SUCCEEDED}),
    JourneyEventKind.HOLD_AUTHORIZED: frozenset({JourneyEventOutcome.SUCCEEDED}),
    JourneyEventKind.PACKET_APPROVED: frozenset({JourneyEventOutcome.SUCCEEDED}),
}

_CONTEXT_OPERATIONS: Final = {
    JourneyEventKind.TICKET_READ: ContextOperation.OCR_EXTRACT,
    JourneyEventKind.TICKET_READ_REFUSED: ContextOperation.OCR_EXTRACT,
    JourneyEventKind.TICKET_READ_UNKNOWN: ContextOperation.OCR_EXTRACT,
    JourneyEventKind.FACTS_CONFIRMED: ContextOperation.FACT_CONFIRMATION,
    JourneyEventKind.INTAKE_SUBMITTED: ContextOperation.INTAKE_SUBMISSION,
    JourneyEventKind.HOLD_AUTHORIZED: ContextOperation.PAYMENT_AUTHORIZATION,
    JourneyEventKind.PACKET_APPROVED: ContextOperation.PACKET_APPROVAL,
}


def _validate_context(
    context: JourneyEventContext | None,
    kind: JourneyEventKind,
    outcome: JourneyEventOutcome,
    duration_ms: int | None,
) -> None:
    if context is None:
        return
    if type(context) is not JourneyEventContext:
        raise ValueError("context must be JourneyEventContext")
    context.validate_duration(duration_ms)
    if context.operation not in {_CONTEXT_OPERATIONS[kind], ContextOperation.UNKNOWN}:
        raise ValueError("context operation disagrees with event kind")
    reason = {
        JourneyEventOutcome.SUCCEEDED: ContextReason.NOT_APPLICABLE,
        JourneyEventOutcome.REFUSED: ContextReason.READING_REFUSED,
        JourneyEventOutcome.UNKNOWN: ContextReason.READING_RESULT_UNKNOWN,
    }[outcome]
    if context.reason not in {reason, ContextReason.UNKNOWN}:
        raise ValueError("context reason disagrees with event outcome")
    if kind is JourneyEventKind.HOLD_AUTHORIZED and context.funding is ContextFunding.FREE_BETA:
        raise ValueError("no-charge journey cannot claim a paid hold")


_EVENT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "event_id",
        "journey_id",
        "seq",
        "kind",
        "outcome",
        "schema_version",
        "occurred_at",
        "recorded_at",
        "duration_ms",
        "cost_cents",
        "cost_status",
    }
)

_INPUT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "kind",
        "outcome",
        "occurred_at",
        "recorded_at",
        "duration_ms",
        "cost_cents",
        "cost_status",
        "schema_version",
        "context",
    }
)


@runtime_checkable
class _ConnectionLike(Protocol):
    @property
    def in_transaction(self) -> bool: ...

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor: ...


@runtime_checkable
class _ConnectionOwner(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


def _to_utc(value: object, field_name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _require_utc(value: object, field_name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be a UTC-aware datetime")
    return value


def _bounded_int(value: object, field_name: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field_name} must be an integer in [{minimum}, {maximum}]")
    return value


def _validate_workspace_token(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > MAX_TOKEN_LENGTH
        or "@" in value
        or any(ord(ch) < 32 or ord(ch) == 127 or ch.isspace() for ch in value)
    ):
        raise ValueError("invalid workspace token")
    return value


def _validate_occurrence_key(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > MAX_OCCURRENCE_KEY_LENGTH
        or "@" in value
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        raise ValueError("invalid occurrence key")
    return value


def _validate_owner(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > MAX_TOKEN_LENGTH
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        raise ValueError("invalid owner")
    return value


def _validate_enum[EnumT: StrEnum](value: object, enum_cls: type[EnumT], field_name: str) -> EnumT:
    if type(value) is enum_cls:
        return value
    raise ValueError(f"{field_name} must be a {enum_cls.__name__}")


def _parse_enum[EnumT: StrEnum](value: object, enum_cls: type[EnumT], field_name: str) -> EnumT:
    if type(value) is enum_cls:
        return value
    if type(value) is str:
        try:
            return enum_cls(value)
        except ValueError:
            pass
    raise ValueError(f"invalid {field_name}")


def _parse_datetime_value(value: object, field_name: str) -> datetime:
    if type(value) is datetime:
        return _to_utc(value, field_name)
    if type(value) is str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"invalid {field_name}") from exc
        return _to_utc(parsed, field_name)
    raise ValueError(f"invalid {field_name}")


def _validate_cost(cost_cents: object, cost_status: CostStatus) -> tuple[int | None, CostStatus]:
    if cost_status is CostStatus.KNOWN:
        validated = _bounded_int(cost_cents, "cost_cents", minimum=0, maximum=MAX_COST_CENTS)
        return validated, cost_status
    if cost_cents is not None:
        raise ValueError("cost_cents must be None unless cost_status is known")
    return None, cost_status


@dataclass(frozen=True)
class JourneyEventInput:
    """Closed input contract for recording one journey event."""

    kind: JourneyEventKind
    outcome: JourneyEventOutcome
    occurred_at: datetime
    recorded_at: datetime | None = None
    duration_ms: int | None = None
    cost_cents: int | None = None
    cost_status: CostStatus = CostStatus.NOT_APPLICABLE
    schema_version: int = SCHEMA_VERSION
    context: JourneyEventContext | None = None

    def __post_init__(self) -> None:
        kind = _validate_enum(self.kind, JourneyEventKind, "kind")
        outcome = _validate_enum(self.outcome, JourneyEventOutcome, "outcome")
        if outcome not in _ALLOWED_OUTCOMES[kind]:
            raise ValueError("outcome is not allowed for event kind")
        _bounded_int(
            self.schema_version, "schema_version", minimum=SCHEMA_VERSION, maximum=SCHEMA_VERSION
        )
        occurred = _to_utc(self.occurred_at, "occurred_at")
        recorded = None if self.recorded_at is None else _to_utc(self.recorded_at, "recorded_at")
        object.__setattr__(self, "occurred_at", occurred)
        object.__setattr__(self, "recorded_at", recorded)
        if self.duration_ms is not None:
            _bounded_int(self.duration_ms, "duration_ms", minimum=0, maximum=MAX_DURATION_MS)
        cost_status = _validate_enum(self.cost_status, CostStatus, "cost_status")
        _validate_cost(self.cost_cents, cost_status)
        _validate_context(self.context, kind, outcome, self.duration_ms)

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> JourneyEventInput:
        """Strictly parse a closed mapping, rejecting any unknown or PII-like fields."""
        if not isinstance(data, Mapping):
            raise ValueError("event payload must be a mapping")
        keys = set(data.keys())
        unknown = keys - _INPUT_FIELDS
        if unknown:
            raise ValueError("unknown journey event fields are forbidden")
        for required in ("kind", "outcome", "occurred_at"):
            if required not in keys:
                raise ValueError(f"missing required field: {required}")
        kind = _parse_enum(data["kind"], JourneyEventKind, "kind")
        outcome = _parse_enum(data["outcome"], JourneyEventOutcome, "outcome")
        occurred_at = _parse_datetime_value(data["occurred_at"], "occurred_at")
        recorded_raw = data.get("recorded_at")
        recorded_at = (
            None if recorded_raw is None else _parse_datetime_value(recorded_raw, "recorded_at")
        )
        duration_raw = data.get("duration_ms")
        duration_ms = (
            None
            if duration_raw is None
            else _bounded_int(duration_raw, "duration_ms", minimum=0, maximum=MAX_DURATION_MS)
        )
        cost_status_raw = data.get("cost_status", CostStatus.NOT_APPLICABLE)
        cost_status = _parse_enum(cost_status_raw, CostStatus, "cost_status")
        cost_raw = data.get("cost_cents")
        cost_cents, cost_status = _validate_cost(cost_raw, cost_status)
        schema_raw = data.get("schema_version", SCHEMA_VERSION)
        schema_version = _bounded_int(
            schema_raw, "schema_version", minimum=SCHEMA_VERSION, maximum=SCHEMA_VERSION
        )
        return cls(
            kind=kind,
            outcome=outcome,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            duration_ms=duration_ms,
            cost_cents=cost_cents,
            cost_status=cost_status,
            schema_version=schema_version,
            context=JourneyEventContext.from_mapping(data["context"])
            if "context" in data
            else None,
        )


@dataclass(frozen=True)
class JourneyEvent:
    """Public ordered projection of one durable journey event.

    Contains no workspace_token, owner, intake_id, case_token, provider ID,
    deduplication key, or free text.
    """

    event_id: str
    journey_id: str
    seq: int
    kind: JourneyEventKind
    outcome: JourneyEventOutcome
    schema_version: int
    occurred_at: datetime
    recorded_at: datetime
    duration_ms: int | None
    cost_cents: int | None
    cost_status: CostStatus
    context: JourneyEventContext | None = None

    def __post_init__(self) -> None:
        if type(self.event_id) is not str or _EVENT_ID_RE.fullmatch(self.event_id) is None:
            raise ValueError("invalid event_id")
        if type(self.journey_id) is not str or _JOURNEY_ID_RE.fullmatch(self.journey_id) is None:
            raise ValueError("invalid journey_id")
        _bounded_int(self.seq, "seq", minimum=1, maximum=MAX_DURATION_MS)
        kind = _validate_enum(self.kind, JourneyEventKind, "kind")
        outcome = _validate_enum(self.outcome, JourneyEventOutcome, "outcome")
        if outcome not in _ALLOWED_OUTCOMES[kind]:
            raise ValueError("outcome is not allowed for event kind")
        _bounded_int(
            self.schema_version, "schema_version", minimum=SCHEMA_VERSION, maximum=SCHEMA_VERSION
        )
        _require_utc(self.occurred_at, "occurred_at")
        _require_utc(self.recorded_at, "recorded_at")
        if self.duration_ms is not None:
            _bounded_int(self.duration_ms, "duration_ms", minimum=0, maximum=MAX_DURATION_MS)
        cost_status = _validate_enum(self.cost_status, CostStatus, "cost_status")
        _validate_cost(self.cost_cents, cost_status)
        _validate_context(self.context, kind, outcome, self.duration_ms)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "event_id": self.event_id,
            "journey_id": self.journey_id,
            "seq": self.seq,
            "kind": self.kind.value,
            "outcome": self.outcome.value,
            "schema_version": self.schema_version,
            "occurred_at": self.occurred_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "duration_ms": self.duration_ms,
            "cost_cents": self.cost_cents,
            "cost_status": self.cost_status.value,
        }

        if self.context is not None:
            result["context"] = self.context.to_dict()
        return result

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> JourneyEvent:
        """Strictly parse a closed public event mapping, rejecting extra fields."""
        if not isinstance(data, Mapping):
            raise ValueError("event must be a mapping")
        keys = set(data.keys())
        if keys not in (_EVENT_FIELDS, _EVENT_FIELDS | {"context"}):
            raise ValueError("journey event keys must match the closed schema exactly")
        event_id = data["event_id"]
        journey_id = data["journey_id"]
        if type(event_id) is not str or _EVENT_ID_RE.fullmatch(event_id) is None:
            raise ValueError("invalid event_id")
        if type(journey_id) is not str or _JOURNEY_ID_RE.fullmatch(journey_id) is None:
            raise ValueError("invalid journey_id")
        seq = _bounded_int(data["seq"], "seq", minimum=1, maximum=MAX_DURATION_MS)
        kind = _parse_enum(data["kind"], JourneyEventKind, "kind")
        outcome = _parse_enum(data["outcome"], JourneyEventOutcome, "outcome")
        schema_version = _bounded_int(
            data["schema_version"],
            "schema_version",
            minimum=SCHEMA_VERSION,
            maximum=SCHEMA_VERSION,
        )
        occurred_at = _parse_datetime_value(data["occurred_at"], "occurred_at")
        recorded_at = _parse_datetime_value(data["recorded_at"], "recorded_at")
        duration_raw = data["duration_ms"]
        duration_ms = (
            None
            if duration_raw is None
            else _bounded_int(duration_raw, "duration_ms", minimum=0, maximum=MAX_DURATION_MS)
        )
        cost_status = _parse_enum(data["cost_status"], CostStatus, "cost_status")
        cost_cents, cost_status = _validate_cost(data["cost_cents"], cost_status)
        return cls(
            event_id=event_id,
            journey_id=journey_id,
            seq=seq,
            kind=kind,
            outcome=outcome,
            schema_version=schema_version,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            duration_ms=duration_ms,
            cost_cents=cost_cents,
            cost_status=cost_status,
            context=JourneyEventContext.from_mapping(data["context"])
            if "context" in data
            else None,
        )


@dataclass(frozen=True)
class JourneyEventPage:
    """Bounded, ordered page of journey events for one opaque journey."""

    journey_id: str | None
    events: tuple[JourneyEvent, ...]
    next_after_seq: int | None

    def __post_init__(self) -> None:
        if self.journey_id is not None and (
            type(self.journey_id) is not str or _JOURNEY_ID_RE.fullmatch(self.journey_id) is None
        ):
            raise ValueError("invalid journey_id")
        if type(self.events) is not tuple:
            raise ValueError("events must be a tuple")
        previous_seq = 0
        for item in self.events:
            if type(item) is not JourneyEvent:
                raise ValueError("events must contain JourneyEvent values")
            if self.journey_id is None or item.journey_id != self.journey_id:
                raise ValueError("event journey_id mismatch")
            if item.seq <= previous_seq:
                raise ValueError("events must be strictly ordered by seq")
            previous_seq = item.seq
        if self.next_after_seq is not None:
            if not self.events:
                raise ValueError("empty page cannot carry next_after_seq")
            if type(self.next_after_seq) is not int or self.next_after_seq != self.events[-1].seq:
                raise ValueError("next_after_seq must match last event seq")


def extract_usage_cost(
    usage_json: str | None, *, force_unknown: bool = False
) -> tuple[int | None, CostStatus]:
    """Read measured integer cost_cents from stored usage JSON, never inventing zero."""
    if force_unknown or usage_json is None:
        return None, CostStatus.UNKNOWN
    try:
        data = json.loads(usage_json)
    except (TypeError, ValueError):
        return None, CostStatus.UNKNOWN
    if not isinstance(data, dict):
        return None, CostStatus.UNKNOWN
    raw_cost = data.get("cost_cents")
    if type(raw_cost) is int and 0 <= raw_cost <= MAX_COST_CENTS:
        return raw_cost, CostStatus.KNOWN
    return None, CostStatus.UNKNOWN


def _occurrence_sha256(occurrence_key: str) -> str:
    return hashlib.sha256(_OCCURRENCE_DOMAIN + occurrence_key.encode("utf-8")).hexdigest()


def _content_sha256(journey_id: str, event: JourneyEventInput) -> str:
    payload = json.dumps(
        {
            "journey_id": journey_id,
            "kind": event.kind.value,
            "outcome": event.outcome.value,
            "schema_version": event.schema_version,
            "occurred_at": event.occurred_at.isoformat(),
            "duration_ms": event.duration_ms,
            "cost_cents": event.cost_cents,
            "cost_status": event.cost_status.value,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _table_exists(database: _ConnectionLike, name: str) -> bool:
    return (
        database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _assert_initialized(database: _ConnectionLike) -> None:
    if not _table_exists(database, "pb_store_versions"):
        raise StorageCorruption("missing journey events prerequisites")
    version = database.execute(
        "SELECT version FROM pb_store_versions WHERE component=?", (COMPONENT,)
    ).fetchone()
    if version != (SCHEMA_VERSION,):
        raise StorageCorruption("journey events schema is not initialized")
    if not _table_exists(database, "pb_journey_links") or not _table_exists(
        database, "pb_journey_events"
    ):
        raise StorageCorruption("journey events tables are missing")


def initialize_journey_events(database: sqlite3.Connection) -> None:
    """Create or validate the append-only journey ledger on the authority connection."""
    if not isinstance(database, sqlite3.Connection):
        raise TypeError("journey events needs the authority SQLite connection")
    if database.in_transaction:
        raise StorageCorruption("journey events initialization needs its own transaction")
    database.execute("BEGIN IMMEDIATE")
    try:
        if not _table_exists(database, "pb_store_versions"):
            raise StorageCorruption("missing journey events prerequisites")
        check_versions_schema(database)
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component=?", (COMPONENT,)
        ).fetchone()
        tables = {
            str(row[0]): row[1]
            for row in database.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name IN ('pb_journey_links', 'pb_journey_events')"
            )
            if isinstance(row[1], str)
        }
        expected = {name: " ".join(ddl.upper().split()) for name, ddl in _DDL}
        actual = {name: " ".join(sql.upper().split()) for name, sql in tables.items()}
        if version is None:
            if tables:
                raise StorageCorruption("unversioned journey events schema")
            for _name, ddl in _DDL:
                database.execute(ddl)
            database.execute(
                "INSERT INTO pb_store_versions (component, version) VALUES (?, ?)",
                (COMPONENT, SCHEMA_VERSION),
            )
        elif version != (SCHEMA_VERSION,) or actual != expected:
            raise StorageCorruption("malformed journey events schema")
        for table, message in (
            ("pb_journey_links", "journey links are immutable"),
            ("pb_journey_events", "journey events are append-only"),
        ):
            for action in ("UPDATE", "DELETE"):
                database.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                    f"BEFORE {action} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{message}'); END"
                )
        _initialize_journey_capture(database)
        initialize_journey_event_context(database)
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def _decode_event_row(row: tuple[Any, ...]) -> JourneyEvent:
    if len(row) not in (11, 12):
        raise StorageCorruption("malformed stored journey event")
    (
        event_id,
        journey_id,
        seq,
        kind_text,
        outcome_text,
        schema_version,
        occurred_text,
        recorded_text,
        duration_ms,
        cost_cents,
        cost_status_text,
    ) = row[:11]
    try:
        if type(occurred_text) is not str or type(recorded_text) is not str:
            raise ValueError("invalid stored timestamp")
        return JourneyEvent(
            event_id=event_id,
            journey_id=journey_id,
            seq=seq,
            kind=_parse_enum(kind_text, JourneyEventKind, "kind"),
            outcome=_parse_enum(outcome_text, JourneyEventOutcome, "outcome"),
            schema_version=schema_version,
            occurred_at=_parse_datetime_value(occurred_text, "occurred_at"),
            recorded_at=_parse_datetime_value(recorded_text, "recorded_at"),
            duration_ms=duration_ms,
            cost_cents=cost_cents,
            cost_status=_parse_enum(cost_status_text, CostStatus, "cost_status"),
            context=decode_context(row[11]) if len(row) == 12 else None,
        )
    except (TypeError, ValueError) as exc:
        raise StorageCorruption(f"malformed stored journey event: {exc}") from exc


def _rollback_preserving_error(database: _ConnectionLike) -> None:
    if isinstance(database, sqlite3.Connection):
        rollback_preserving_error(database)
        return
    original = sys.exception()
    try:
        database.execute("ROLLBACK")
    except Exception as rollback_error:
        if original is not None:
            original.add_note(f"ROLLBACK also failed: {rollback_error!r}")


def prior_journey_event_reference_in_transaction(
    database: _ConnectionLike, *, workspace_token: str, occurrence_key: str
) -> JourneyReference:
    """Reference an exact recorded predecessor in this workspace, never infer one."""
    if not database.in_transaction:
        raise StorageCorruption("journey predecessor lookup requires active transaction")
    workspace = _validate_workspace_token(workspace_token)
    occurrence = _validate_occurrence_key(occurrence_key)
    _assert_initialized(database)
    row = database.execute(
        "SELECT e.event_id FROM pb_journey_events e JOIN pb_journey_links l "
        "ON l.journey_id=e.journey_id WHERE l.workspace_token=? AND e.occurrence_sha256=?",
        (workspace, _occurrence_sha256(occurrence)),
    ).fetchone()
    if row is None:
        return JourneyReference()
    if type(row[0]) is not str or _EVENT_ID_RE.fullmatch(row[0]) is None:
        raise StorageCorruption("malformed predecessor identity")
    return JourneyReference(ReferenceState.KNOWN, row[0])


def append_journey_event_in_transaction(
    database: _ConnectionLike,
    *,
    workspace_token: str,
    occurrence_key: str,
    event: JourneyEventInput,
    clock: Callable[[], datetime] | None = None,
) -> JourneyEvent:
    """Append one journey event inside the caller's already-open SQLite transaction.

    Replay of the exact same ``occurrence_key`` and semantic content is a no-op that
    returns the originally recorded ``JourneyEvent``. Same-key differing content is
    refused with ``JourneyEventConflict``.
    """
    if not database.in_transaction:
        raise StorageCorruption("journey event append requires active transaction")
    workspace = _validate_workspace_token(workspace_token)
    occurrence = _validate_occurrence_key(occurrence_key)
    if type(event) is not JourneyEventInput:
        raise ValueError("event must be a JourneyEventInput")
    _assert_initialized(database)

    resolved_recorded_at = event.recorded_at

    def _recorded_at() -> datetime:
        nonlocal resolved_recorded_at
        if resolved_recorded_at is None:
            raw_now = datetime.now(UTC) if clock is None else clock()
            resolved_recorded_at = _to_utc(raw_now, "recorded_at")
        return resolved_recorded_at

    # Preserve lazy fallback for legacy/internal callers, but new workspaces
    # already have identity before their first event. Replays do not read the clock.
    link_row = database.execute(
        "SELECT journey_id FROM pb_journey_links WHERE workspace_token=?", (workspace,)
    ).fetchone()
    if link_row is None:
        journey_id = ensure_journey_identity_in_transaction(
            database,
            workspace_token=workspace,
            at=_recorded_at(),
            basis=CaptureStartBasis.FIRST_DURABLE_EVENT,
        )
    else:
        if type(link_row[0]) is not str or _JOURNEY_ID_RE.fullmatch(link_row[0]) is None:
            raise StorageCorruption("malformed stored journey link")
        journey_id = link_row[0]

    occ_sha = _occurrence_sha256(occurrence)
    content_sha = _content_sha256(journey_id, event)
    existing = database.execute(
        "SELECT e.content_sha256, e.event_id, e.journey_id, e.seq, e.kind, e.outcome, "
        "e.schema_version, e.occurred_at, e.recorded_at, e.duration_ms, e.cost_cents, "
        "e.cost_status, c.context_json FROM pb_journey_events e "
        "LEFT JOIN pb_journey_event_context c ON c.event_id=e.event_id "
        "WHERE e.occurrence_sha256=?",
        (occ_sha,),
    ).fetchone()
    if existing is not None:
        stored_sha = existing[0]
        if type(stored_sha) is not str or _SHA256_RE.fullmatch(stored_sha) is None:
            raise StorageCorruption("malformed stored journey event digest")
        stored_event = _decode_event_row(existing[1:])
        if stored_event.journey_id != journey_id or stored_sha != content_sha:
            raise JourneyEventConflict("journey_event_occurrence_conflict")
        if (
            stored_event.context is not None
            and event.context is not None
            and stored_event.context != event.context
        ):
            raise JourneyEventConflict("journey_event_context_conflict")
        # A legacy replay never receives newly invented historical context. An
        # omitted context also cannot erase an already-recorded context.
        return stored_event

    recorded_at = _recorded_at()
    seq_row = database.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM pb_journey_events WHERE journey_id=?",
        (journey_id,),
    ).fetchone()
    if seq_row is None or type(seq_row[0]) is not int or seq_row[0] < 1:
        raise StorageCorruption("invalid stored journey sequence")
    next_seq = seq_row[0]
    if event.context is not None and event.context.causation_ref.status is ReferenceState.KNOWN:
        predecessor = database.execute(
            "SELECT seq FROM pb_journey_events WHERE event_id=? AND journey_id=?",
            (event.context.causation_ref.opaque_id, journey_id),
        ).fetchone()
        if predecessor is None or type(predecessor[0]) is not int or predecessor[0] >= next_seq:
            raise ValueError("causation requires an earlier recorded event in the same journey")
    event_id = f"evt_{uuid.uuid4().hex}"
    database.execute(
        "INSERT INTO pb_journey_events "
        "(event_id, journey_id, seq, occurrence_sha256, content_sha256, kind, outcome, "
        "schema_version, occurred_at, recorded_at, duration_ms, cost_cents, cost_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            journey_id,
            next_seq,
            occ_sha,
            content_sha,
            event.kind.value,
            event.outcome.value,
            event.schema_version,
            event.occurred_at.isoformat(),
            recorded_at.isoformat(),
            event.duration_ms,
            event.cost_cents,
            event.cost_status.value,
        ),
    )
    if event.context is not None:
        database.execute(
            "INSERT INTO pb_journey_event_context VALUES (?, ?)",
            (event_id, event.context.to_json()),
        )
    return JourneyEvent(
        event_id=event_id,
        journey_id=journey_id,
        seq=next_seq,
        kind=event.kind,
        outcome=event.outcome,
        schema_version=event.schema_version,
        occurred_at=event.occurred_at,
        recorded_at=recorded_at,
        duration_ms=event.duration_ms,
        cost_cents=event.cost_cents,
        cost_status=event.cost_status,
        context=event.context,
    )


def record_journey_event(
    database: _ConnectionLike,
    *,
    workspace_token: str,
    occurrence_key: str,
    event: JourneyEventInput,
    clock: Callable[[], datetime] | None = None,
) -> JourneyEvent:
    """Record one journey event in its own immediate transaction."""
    if database.in_transaction:
        raise StorageCorruption("record_journey_event requires no active caller transaction")
    database.execute("BEGIN IMMEDIATE")
    try:
        recorded = append_journey_event_in_transaction(
            database,
            workspace_token=workspace_token,
            occurrence_key=occurrence_key,
            event=event,
            clock=clock,
        )
        database.execute("COMMIT")
        return recorded
    except Exception:
        if database.in_transaction:
            _rollback_preserving_error(database)
        raise


def _check_workspace_owner(
    database: _ConnectionLike, workspace_token: str, expected_owner: str
) -> None:
    if not _table_exists(database, "pb_intake_owners"):
        raise JourneyEventAccessError("workspace_owner_mismatch")
    row = database.execute(
        "SELECT owner FROM pb_intake_owners WHERE workspace_token=?",
        (workspace_token,),
    ).fetchone()
    if row is None or type(row[0]) is not str or row[0] != expected_owner:
        raise JourneyEventAccessError("workspace_owner_mismatch")


def read_journey_events(
    database: _ConnectionLike,
    *,
    journey_id: str | None = None,
    workspace_token: str | None = None,
    owner: str | None = None,
    after_seq: int = 0,
    limit: int = DEFAULT_PAGE_SIZE,
) -> JourneyEventPage:
    """Read an ordered, bounded page of journey events for one internal scope.

    Supply exactly one of ``journey_id`` or ``workspace_token``. When ``owner`` is
    supplied, it must match ``pb_intake_owners`` for the resolved workspace. Never
    returns ``workspace_token``, ``owner``, or deduplication keys.
    """
    if (journey_id is None) == (workspace_token is None):
        raise ValueError("supply exactly one of journey_id or workspace_token")
    if journey_id is not None and (
        type(journey_id) is not str or _JOURNEY_ID_RE.fullmatch(journey_id) is None
    ):
        raise ValueError("invalid journey_id")
    validated_workspace = (
        None if workspace_token is None else _validate_workspace_token(workspace_token)
    )
    validated_owner = None if owner is None else _validate_owner(owner)
    _bounded_int(after_seq, "after_seq", minimum=0, maximum=MAX_DURATION_MS)
    _bounded_int(limit, "limit", minimum=1, maximum=MAX_PAGE_SIZE)

    owns_tx = not database.in_transaction
    if owns_tx:
        database.execute("BEGIN")
    try:
        _assert_initialized(database)
        resolved_journey_id: str | None
        if validated_workspace is not None:
            if validated_owner is not None:
                _check_workspace_owner(database, validated_workspace, validated_owner)
            row = database.execute(
                "SELECT journey_id FROM pb_journey_links WHERE workspace_token=?",
                (validated_workspace,),
            ).fetchone()
            if row is None:
                resolved_journey_id = None
            else:
                if type(row[0]) is not str or _JOURNEY_ID_RE.fullmatch(row[0]) is None:
                    raise StorageCorruption("malformed stored journey link")
                resolved_journey_id = row[0]
        else:
            assert journey_id is not None
            row = database.execute(
                "SELECT workspace_token FROM pb_journey_links WHERE journey_id=?",
                (journey_id,),
            ).fetchone()
            if row is None:
                if validated_owner is not None:
                    raise JourneyEventAccessError("workspace_owner_mismatch")
                resolved_journey_id = None
            else:
                if type(row[0]) is not str or not row[0]:
                    raise StorageCorruption("malformed stored journey link")
                if validated_owner is not None:
                    _check_workspace_owner(database, row[0], validated_owner)
                resolved_journey_id = journey_id

        if resolved_journey_id is None:
            if owns_tx:
                database.execute("COMMIT")
            return JourneyEventPage(journey_id=None, events=(), next_after_seq=None)

        rows = database.execute(
            "SELECT e.event_id, e.journey_id, e.seq, e.kind, e.outcome, e.schema_version, "
            "e.occurred_at, e.recorded_at, e.duration_ms, e.cost_cents, "
            "e.cost_status, c.context_json "
            "FROM pb_journey_events e LEFT JOIN pb_journey_event_context c "
            "ON c.event_id=e.event_id WHERE e.journey_id=? AND e.seq>? ORDER BY e.seq ASC LIMIT ?",
            (resolved_journey_id, after_seq, limit + 1),
        ).fetchall()
        decoded = tuple(_decode_event_row(item) for item in rows)
        page_events = decoded[:limit]
        next_after = page_events[-1].seq if len(decoded) > limit and page_events else None
        page = JourneyEventPage(
            journey_id=resolved_journey_id,
            events=page_events,
            next_after_seq=next_after,
        )
        if owns_tx:
            database.execute("COMMIT")
        return page
    except Exception:
        if owns_tx and database.in_transaction:
            _rollback_preserving_error(database)
        raise


def read_journey_capture(
    database: _ConnectionLike, *, workspace_token: str, owner: str | None = None
) -> JourneyCapture | None:
    """Internal scoped snapshot; never exports workspace tokens or customer data."""
    owns_tx = not database.in_transaction
    if owns_tx:
        database.execute("BEGIN")
    try:
        page = read_journey_events(database, workspace_token=workspace_token, owner=owner, limit=1)
        result = None
        if page.journey_id is not None:
            row = database.execute(
                "SELECT started_at, start_basis, build_sha256, config_sha256 "
                "FROM pb_journey_capture WHERE journey_id=?",
                (page.journey_id,),
            ).fetchone()
            if row is None:
                raise StorageCorruption("missing journey capture provenance")
            try:
                basis = CaptureStartBasis(row[1])
                started = None if row[0] is None else _parse_datetime_value(row[0], "capture start")
                if (basis is CaptureStartBasis.LEGACY_UNKNOWN) != (started is None):
                    raise ValueError("invalid capture start")
                result = JourneyCapture(
                    page.journey_id, started, basis, JourneyBuildIdentity(row[2], row[3])
                )
            except (ValueError, TypeError) as exc:
                raise StorageCorruption("malformed journey capture provenance") from exc
        if owns_tx:
            database.execute("COMMIT")
        return result
    except Exception:
        if owns_tx and database.in_transaction:
            _rollback_preserving_error(database)
        raise


class JourneyEventsStoreMixin:
    """Typed journey ledger read surface for SqliteTransactionStore."""

    def journey_capture(
        self: _ConnectionOwner, *, workspace_token: str, owner: str | None = None
    ) -> JourneyCapture | None:
        return read_journey_capture(
            self._require_open(), workspace_token=workspace_token, owner=owner
        )

    def journey_events(
        self: _ConnectionOwner,
        *,
        journey_id: str | None = None,
        workspace_token: str | None = None,
        owner: str | None = None,
        after_seq: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> JourneyEventPage:
        """Read ordered journey events for one internal journey or workspace boundary."""
        return read_journey_events(
            self._require_open(),
            journey_id=journey_id,
            workspace_token=workspace_token,
            owner=owner,
            after_seq=after_seq,
            limit=limit,
        )
