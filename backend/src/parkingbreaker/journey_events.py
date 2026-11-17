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

    def to_dict(self) -> dict[str, object]:
        return {
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

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> JourneyEvent:
        """Strictly parse a closed public event mapping, rejecting extra fields."""
        if not isinstance(data, Mapping):
            raise ValueError("event must be a mapping")
        keys = set(data.keys())
        if keys != _EVENT_FIELDS:
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
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def _decode_event_row(row: tuple[Any, ...]) -> JourneyEvent:
    if len(row) != 11:
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
    ) = row
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

    link_row = database.execute(
        "SELECT journey_id FROM pb_journey_links WHERE workspace_token=?",
        (workspace,),
    ).fetchone()
    if link_row is None:
        journey_id = f"jrn_{uuid.uuid4().hex}"
        database.execute(
            "INSERT INTO pb_journey_links (workspace_token, journey_id, created_at) "
            "VALUES (?, ?, ?)",
            (workspace, journey_id, _recorded_at().isoformat()),
        )
    else:
        raw_journey_id = link_row[0]
        if type(raw_journey_id) is not str or _JOURNEY_ID_RE.fullmatch(raw_journey_id) is None:
            raise StorageCorruption("malformed stored journey link")
        journey_id = raw_journey_id

    occ_sha = _occurrence_sha256(occurrence)
    content_sha = _content_sha256(journey_id, event)
    existing = database.execute(
        "SELECT content_sha256, event_id, journey_id, seq, kind, outcome, schema_version, "
        "occurred_at, recorded_at, duration_ms, cost_cents, cost_status "
        "FROM pb_journey_events WHERE occurrence_sha256=?",
        (occ_sha,),
    ).fetchone()
    if existing is not None:
        stored_sha = existing[0]
        if type(stored_sha) is not str or _SHA256_RE.fullmatch(stored_sha) is None:
            raise StorageCorruption("malformed stored journey event digest")
        stored_event = _decode_event_row(existing[1:])
        if stored_event.journey_id != journey_id or stored_sha != content_sha:
            raise JourneyEventConflict("journey_event_occurrence_conflict")
        return stored_event

    recorded_at = _recorded_at()
    seq_row = database.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM pb_journey_events WHERE journey_id=?",
        (journey_id,),
    ).fetchone()
    if seq_row is None or type(seq_row[0]) is not int or seq_row[0] < 1:
        raise StorageCorruption("invalid stored journey sequence")
    next_seq = seq_row[0]
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
            "SELECT event_id, journey_id, seq, kind, outcome, schema_version, "
            "occurred_at, recorded_at, duration_ms, cost_cents, cost_status "
            "FROM pb_journey_events WHERE journey_id=? AND seq>? ORDER BY seq ASC LIMIT ?",
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


class JourneyEventsStoreMixin:
    """Typed journey ledger read surface for SqliteTransactionStore."""

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
