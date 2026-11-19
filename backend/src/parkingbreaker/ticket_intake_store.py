"""Durable vision-intake reservations, operation claims and customer confirmations.

This store is deliberately not a service, route or provider boundary. It records the
small amount of durable truth needed *before* a vision/review call is paid for, and it
records the customer's later confirmation as a separate, hash-only fact. It never calls a
provider, ledger or AI-spend store, never invents a result for an unknown call, and never
turns a confirmation into verified truth.

The authority is the existing transaction SQLite connection. Table prefixes and the
``pb_store_versions`` component keep the schema isolated; callers wire it explicitly with
:func:`initialize_ticket_intake` once the surrounding store is initialized.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import NoReturn, Protocol, runtime_checkable

from parkingbreaker.journey_context import (
    ContextFactRoute,
    ContextOperation,
    ContextReason,
    DurationBasis,
    JourneyBuildIdentity,
    JourneyEventContext,
    normalize_journey_identity,
    opaque_reference,
    runtime_journey_identity,
)
from parkingbreaker.journey_events import (
    CaptureStartBasis,
    CostStatus,
    JourneyEventInput,
    JourneyEventKind,
    JourneyEventOutcome,
    append_journey_event_in_transaction,
    ensure_journey_identity_in_transaction,
    extract_usage_cost,
)
from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.ticket_observability import (
    OcrObservationInput,
    record_ocr_observation_in_transaction,
)

COMPONENT = "ticket_intake"
MAX_ID_LENGTH = 128
MAX_RESULT_BYTES = 64 * 1024
MAX_USAGE_BYTES = 16 * 1024
_DIGEST = re.compile(r"[0-9a-fA-F]{64}\Z")

_RESERVATIONS_DDL = """CREATE TABLE pb_ticket_intake_reservations (
    workspace_token TEXT PRIMARY KEY REFERENCES pb_intake_owners(workspace_token),
    intake_id TEXT NOT NULL UNIQUE,
    owner TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    CHECK (length(workspace_token) BETWEEN 1 AND 128),
    CHECK (length(intake_id) BETWEEN 1 AND 128),
    CHECK (length(owner) BETWEEN 1 AND 128)
)"""

_OPERATIONS_DDL = """CREATE TABLE pb_ticket_intake_operations (
    request_id TEXT PRIMARY KEY,
    intake_id TEXT NOT NULL REFERENCES pb_ticket_intake_reservations(intake_id),
    workspace_token TEXT NOT NULL,
    owner TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('extract', 'review')),
    binding_sha256 TEXT NOT NULL
        CHECK (length(binding_sha256) = 64
            AND binding_sha256 NOT GLOB '*[^0-9a-f]*'),
    claim_token_sha256 TEXT NOT NULL
        CHECK (length(claim_token_sha256) = 64
            AND claim_token_sha256 NOT GLOB '*[^0-9a-f]*'),
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'success', 'refused', 'unknown')),
    result_json TEXT
        CHECK (result_json IS NULL OR length(result_json) <= 65536),
    usage_json TEXT
        CHECK (usage_json IS NULL OR length(usage_json) <= 16384),
    claimed_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (intake_id, operation, binding_sha256),
    CHECK ((status = 'pending') = (completed_at IS NULL)),
    CHECK (status != 'pending'
        OR (result_json IS NULL AND usage_json IS NULL)),
    CHECK (status != 'unknown' OR result_json IS NULL),
    CHECK (status != 'success' OR result_json IS NOT NULL)
)"""

_ALIASES_DDL = """CREATE TABLE pb_ticket_intake_operation_aliases (
    request_id TEXT PRIMARY KEY,
    claim_request_id TEXT NOT NULL
        REFERENCES pb_ticket_intake_operations(request_id),
    intake_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('extract', 'review')),
    binding_sha256 TEXT NOT NULL
        CHECK (length(binding_sha256) = 64
            AND binding_sha256 NOT GLOB '*[^0-9a-f]*'),
    first_seen_at TEXT NOT NULL
)"""

_CONFIRMATIONS_DDL = """CREATE TABLE pb_ticket_intake_confirmations (
    command_id TEXT PRIMARY KEY,
    intake_id TEXT NOT NULL REFERENCES pb_ticket_intake_reservations(intake_id),
    workspace_token TEXT NOT NULL,
    owner TEXT NOT NULL,
    images_sha256 TEXT NOT NULL
        CHECK (length(images_sha256) = 64
            AND images_sha256 NOT GLOB '*[^0-9a-f]*'),
    fields_sha256 TEXT NOT NULL
        CHECK (length(fields_sha256) = 64
            AND fields_sha256 NOT GLOB '*[^0-9a-f]*'),
    review_request_id TEXT NOT NULL
        REFERENCES pb_ticket_intake_operations(request_id),
    review_result_sha256 TEXT NOT NULL
        CHECK (length(review_result_sha256) = 64
            AND review_result_sha256 NOT GLOB '*[^0-9a-f]*'),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    discrepancy_status TEXT NOT NULL
        CHECK (discrepancy_status IN ('none', 'resolved', 'unresolved')),
    confirmed_at TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0 CHECK (verified = 0),
    CHECK (length(workspace_token) BETWEEN 1 AND 128),
    CHECK (length(owner) BETWEEN 1 AND 128)
)"""

_DDL = (_RESERVATIONS_DDL, _OPERATIONS_DDL, _ALIASES_DDL, _CONFIRMATIONS_DDL)

_CLAIM_COLUMNS = (
    "request_id, intake_id, workspace_token, owner, operation, binding_sha256, "
    "claim_token_sha256, status, result_json, usage_json, claimed_at, completed_at"
)
_ALIAS_COLUMNS = "request_id, claim_request_id, intake_id, operation, binding_sha256, first_seen_at"
_CONFIRMATION_COLUMNS = (
    "command_id, intake_id, workspace_token, owner, images_sha256, fields_sha256, "
    "review_request_id, review_result_sha256, revision, generation, discrepancy_status, "
    "confirmed_at, verified"
)


class TicketIntakeConflict(ValueError):
    """A fixed refusal code only; it never contains customer content."""


class IntakeOperation(StrEnum):
    """The two closed paid-operation kinds that may claim a reserved intake subject."""

    EXTRACT = "extract"
    REVIEW = "review"


class ClaimStatus(StrEnum):
    """A claim's immutable lifecycle status."""

    PENDING = "pending"
    SUCCESS = "success"
    REFUSED = "refused"
    UNKNOWN = "unknown"


class DiscrepancyStatus(StrEnum):
    """The customer's explicit resolution state; never inferred by this store."""

    NONE = "none"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class ConfirmationState(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    MISSING = "missing"


@dataclass(frozen=True, repr=False)
class IntakeReservation:
    """The one immutable intake id reserved for an owned workspace."""

    intake_id: str
    workspace_token: str
    owner: str
    reserved_at: datetime

    @property
    def reserved_intake_id(self) -> str:
        return self.intake_id

    def __repr__(self) -> str:
        return "IntakeReservation(<redacted>)"


@dataclass(frozen=True, repr=False)
class OperationClaim:
    """An immutable operation claim, including its redacted-at-rest token state."""

    request_id: str
    intake_id: str
    workspace_token: str
    owner: str
    operation: IntakeOperation
    binding_sha256: str
    status: ClaimStatus
    result_json: str | None
    usage_json: str | None
    claimed_at: datetime
    completed_at: datetime | None
    # Only the claim creator receives the raw token; every replay receives None.
    claim_token: str | None = field(default=None, compare=False)

    @property
    def should_call(self) -> bool:
        """Whether this caller owns a new in-flight provider call."""
        return self.status is ClaimStatus.PENDING and self.claim_token is not None

    @property
    def call_required(self) -> bool:
        return self.should_call

    @property
    def completed(self) -> bool:
        return self.status is not ClaimStatus.PENDING

    def __repr__(self) -> str:
        return "OperationClaim(<redacted>)"


@dataclass(frozen=True, repr=False)
class _ClaimAlias:
    request_id: str
    claim_request_id: str
    intake_id: str
    operation: IntakeOperation
    binding_sha256: str
    first_seen_at: datetime

    def __repr__(self) -> str:
        return "_ClaimAlias(<redacted>)"


@dataclass(frozen=True, repr=False)
class CustomerConfirmation:
    """A hash-only customer confirmation; ``verified`` is structurally always false."""

    command_id: str
    intake_id: str
    workspace_token: str
    owner: str
    images_sha256: str
    fields_sha256: str
    review_request_id: str
    review_result_sha256: str
    revision: int
    generation: int
    discrepancy_status: DiscrepancyStatus
    confirmed_at: datetime
    verified: bool

    def __repr__(self) -> str:
        return "CustomerConfirmation(<redacted>)"


@dataclass(frozen=True, repr=False)
class ConfirmationRead:
    """A caller-bound confirmation read: current, stale with a fixed reason, or missing."""

    state: ConfirmationState
    confirmation: CustomerConfirmation | None
    reason: str | None

    @property
    def current(self) -> bool:
        return self.state is ConfirmationState.CURRENT

    @property
    def stale(self) -> bool:
        return self.state is ConfirmationState.STALE

    def __repr__(self) -> str:
        return "ConfirmationRead(<redacted>)"


@runtime_checkable
class _ConnectionOwner(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


ConnectionSource = sqlite3.Connection | _ConnectionOwner


def _utc(value: datetime, field_name: str = "timestamp") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _opaque(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_ID_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"invalid {field_name}")
    return value


def _digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"invalid {field_name}")
    return value.lower()


def _raw_token(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("invalid claim token")
    return value


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"non-JSON constant: {value}")


def _json_text(value: str | None, *, field_name: str, max_bytes: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid {field_name}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"invalid {field_name}") from None
    if len(encoded) > max_bytes:
        raise ValueError(f"{field_name} is too large")
    try:
        json.loads(
            value,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {field_name}") from exc
    return value


def _load_json_stored(value: object, *, field_name: str, max_bytes: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise StorageCorruption(f"malformed {field_name}")
    try:
        return _json_text(value, field_name=field_name, max_bytes=max_bytes)
    except ValueError as exc:
        raise StorageCorruption(f"malformed {field_name}") from exc


def _parse_time(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise StorageCorruption(f"malformed {field_name}")
    try:
        parsed = datetime.fromisoformat(value)
        _utc(parsed, field_name)
    except (TypeError, ValueError) as exc:
        raise StorageCorruption(f"malformed {field_name}") from exc
    return parsed


def _operation(value: IntakeOperation | str) -> IntakeOperation:
    if isinstance(value, IntakeOperation):
        return value
    try:
        return IntakeOperation(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid operation") from exc


def _status(value: ClaimStatus | str) -> ClaimStatus:
    if isinstance(value, ClaimStatus):
        return value
    try:
        return ClaimStatus(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid claim status") from exc


def _discrepancy(value: DiscrepancyStatus | str) -> DiscrepancyStatus:
    if isinstance(value, DiscrepancyStatus):
        return value
    try:
        return DiscrepancyStatus(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid discrepancy status") from exc


def _revision(value: object, field_name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"invalid {field_name}")
    return value


def _table_exists(database: sqlite3.Connection, name: str) -> bool:
    return (
        database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _begin(database: sqlite3.Connection) -> None:
    if database.in_transaction:
        raise StorageCorruption("ticket intake needs its own transaction")
    database.execute("BEGIN IMMEDIATE")


def initialize_ticket_intake(database: sqlite3.Connection) -> None:
    """Create or validate the isolated ticket-intake schema on the authority connection."""
    if not isinstance(database, sqlite3.Connection):
        raise TypeError("ticket intake needs the authority SQLite connection")
    _begin(database)
    try:
        if not _table_exists(database, "pb_store_versions") or not _table_exists(
            database, "pb_intake_owners"
        ):
            raise StorageCorruption("missing ticket intake prerequisites")
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component=?",
            (COMPONENT,),
        ).fetchone()
        tables = {
            str(row[0]): row[1]
            for row in database.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'pb_ticket_intake_%'"
            )
            if isinstance(row[1], str)
        }
        expected = {ddl.split()[2]: " ".join(ddl.upper().split()) for ddl in _DDL}
        actual = {name: " ".join(sql.upper().split()) for name, sql in tables.items()}
        if version is None:
            if tables:
                raise StorageCorruption("unversioned ticket intake schema")
            for ddl in _DDL:
                database.execute(ddl)
            database.execute(
                "INSERT INTO pb_store_versions (component, version) VALUES (?, 1)",
                (COMPONENT,),
            )
        elif version != (1,) or actual != expected:
            raise StorageCorruption("malformed ticket intake schema")
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_ticket_intake_reservations_fixed "
            "BEFORE UPDATE ON pb_ticket_intake_reservations "
            "BEGIN SELECT RAISE(ABORT, 'ticket intake reservations are fixed'); END"
        )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_ticket_intake_reservations_no_delete "
            "BEFORE DELETE ON pb_ticket_intake_reservations "
            "BEGIN SELECT RAISE(ABORT, 'ticket intake reservations are fixed'); END"
        )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_ticket_intake_operations_no_delete "
            "BEFORE DELETE ON pb_ticket_intake_operations "
            "BEGIN SELECT RAISE(ABORT, 'ticket intake claims are append-only'); END"
        )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_ticket_intake_operations_complete_once "
            "BEFORE UPDATE ON pb_ticket_intake_operations "
            "WHEN OLD.status != 'pending' "
            "OR NEW.request_id IS NOT OLD.request_id "
            "OR NEW.intake_id IS NOT OLD.intake_id "
            "OR NEW.workspace_token IS NOT OLD.workspace_token "
            "OR NEW.owner IS NOT OLD.owner "
            "OR NEW.operation IS NOT OLD.operation "
            "OR NEW.binding_sha256 IS NOT OLD.binding_sha256 "
            "OR NEW.claim_token_sha256 IS NOT OLD.claim_token_sha256 "
            "OR NEW.claimed_at IS NOT OLD.claimed_at "
            "OR NEW.status = 'pending' "
            "BEGIN SELECT RAISE(ABORT, 'ticket intake claims complete once'); END"
        )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_ticket_intake_operation_aliases_fixed "
            "BEFORE UPDATE ON pb_ticket_intake_operation_aliases "
            "BEGIN SELECT RAISE(ABORT, 'ticket intake request aliases are fixed'); END"
        )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_ticket_intake_operation_aliases_no_delete "
            "BEFORE DELETE ON pb_ticket_intake_operation_aliases "
            "BEGIN SELECT RAISE(ABORT, 'ticket intake request aliases are fixed'); END"
        )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_ticket_intake_confirmations_no_update "
            "BEFORE UPDATE ON pb_ticket_intake_confirmations "
            "BEGIN SELECT RAISE(ABORT, 'ticket intake confirmations are append-only'); END"
        )
        database.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_ticket_intake_confirmations_no_delete "
            "BEFORE DELETE ON pb_ticket_intake_confirmations "
            "BEGIN SELECT RAISE(ABORT, 'ticket intake confirmations are append-only'); END"
        )
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def _reservation(row: tuple[object, ...]) -> IntakeReservation:
    if len(row) != 4:
        raise StorageCorruption("malformed ticket intake reservation")
    workspace_token, intake_id, owner, reserved_at = row
    if not (
        isinstance(workspace_token, str)
        and isinstance(intake_id, str)
        and isinstance(owner, str)
        and workspace_token
        and intake_id
        and owner
    ):
        raise StorageCorruption("malformed ticket intake reservation")
    return IntakeReservation(
        intake_id,
        workspace_token,
        owner,
        _parse_time(reserved_at, "ticket intake reservation time"),
    )


def _load_reservation(
    database: sqlite3.Connection, by: str, value: str
) -> IntakeReservation | None:
    column = "workspace_token" if by == "workspace_token" else "intake_id"
    row = database.execute(
        "SELECT workspace_token, intake_id, owner, reserved_at "
        f"FROM pb_ticket_intake_reservations WHERE {column}=?",
        (value,),
    ).fetchone()
    if row is None:
        return None
    reservation = _reservation(row)
    owner_row = database.execute(
        "SELECT owner FROM pb_intake_owners WHERE workspace_token=?",
        (reservation.workspace_token,),
    ).fetchone()
    if owner_row is None or not isinstance(owner_row[0], str) or owner_row[0] != reservation.owner:
        raise StorageCorruption("ticket intake reservation owner mismatch")
    return reservation


def _claim(row: tuple[object, ...], *, claim_token: str | None = None) -> OperationClaim:
    if len(row) != 12:
        raise StorageCorruption("malformed ticket intake claim")
    (
        request_id,
        intake_id,
        workspace_token,
        owner,
        operation_text,
        binding_sha256,
        _claim_token_sha256,
        status_text,
        result_json,
        usage_json,
        claimed_at,
        completed_at,
    ) = row
    if not (
        isinstance(request_id, str)
        and isinstance(intake_id, str)
        and isinstance(workspace_token, str)
        and isinstance(owner, str)
        and isinstance(operation_text, str)
        and isinstance(binding_sha256, str)
        and isinstance(status_text, str)
        and request_id
        and intake_id
        and workspace_token
        and owner
    ):
        raise StorageCorruption("malformed ticket intake claim")
    try:
        operation = IntakeOperation(operation_text)
        status = ClaimStatus(status_text)
        binding = _digest(binding_sha256, "binding digest")
        _digest(_claim_token_sha256, "claim token digest")
    except ValueError as exc:
        raise StorageCorruption("malformed ticket intake claim") from exc
    stored_result = _load_json_stored(
        result_json, field_name="ticket intake result", max_bytes=MAX_RESULT_BYTES
    )
    stored_usage = _load_json_stored(
        usage_json, field_name="ticket intake usage", max_bytes=MAX_USAGE_BYTES
    )
    claimed = _parse_time(claimed_at, "ticket intake claim time")
    completed = (
        None if completed_at is None else _parse_time(completed_at, "ticket intake completion time")
    )
    if status is ClaimStatus.PENDING:
        if completed is not None or stored_result is not None or stored_usage is not None:
            raise StorageCorruption("malformed pending ticket intake claim")
    else:
        if completed is None or completed < claimed:
            raise StorageCorruption("malformed completed ticket intake claim")
        if status is ClaimStatus.UNKNOWN and stored_result is not None:
            raise StorageCorruption("unknown ticket intake claim has a result")
        if status is ClaimStatus.SUCCESS and stored_result is None:
            raise StorageCorruption("successful ticket intake claim has no result")
    return OperationClaim(
        request_id,
        intake_id,
        workspace_token,
        owner,
        operation,
        binding,
        status,
        stored_result,
        stored_usage,
        claimed,
        completed,
        claim_token,
    )


def _alias(row: tuple[object, ...]) -> _ClaimAlias:
    if len(row) != 6:
        raise StorageCorruption("malformed ticket intake request alias")
    (
        request_id,
        claim_request_id,
        intake_id,
        operation_text,
        binding_sha256,
        first_seen_at,
    ) = row
    if not (
        isinstance(request_id, str)
        and isinstance(claim_request_id, str)
        and isinstance(intake_id, str)
        and isinstance(operation_text, str)
        and request_id
        and claim_request_id
        and intake_id
    ):
        raise StorageCorruption("malformed ticket intake request alias")
    try:
        operation = IntakeOperation(operation_text)
        binding = _digest(binding_sha256, "binding digest")
    except (TypeError, ValueError) as exc:
        raise StorageCorruption("malformed ticket intake request alias") from exc
    return _ClaimAlias(
        request_id,
        claim_request_id,
        intake_id,
        operation,
        binding,
        _parse_time(first_seen_at, "ticket intake request alias time"),
    )


def _alias_for(database: sqlite3.Connection, request_id: str) -> _ClaimAlias | None:
    row = database.execute(
        f"SELECT {_ALIAS_COLUMNS} FROM pb_ticket_intake_operation_aliases WHERE request_id=?",
        (request_id,),
    ).fetchone()
    return None if row is None else _alias(row)


def _store_alias(
    database: sqlite3.Connection, request_id: str, claim: OperationClaim, at: datetime
) -> None:
    if request_id == claim.request_id:
        return
    try:
        database.execute(
            "INSERT INTO pb_ticket_intake_operation_aliases "
            "(request_id, claim_request_id, intake_id, operation, binding_sha256, first_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                request_id,
                claim.request_id,
                claim.intake_id,
                claim.operation.value,
                claim.binding_sha256,
                at.isoformat(),
            ),
        )
    except sqlite3.IntegrityError:
        raise TicketIntakeConflict("request_id_reused_for_another_claim") from None


def _confirmation(row: tuple[object, ...]) -> CustomerConfirmation:
    if len(row) != 13:
        raise StorageCorruption("malformed ticket intake confirmation")
    (
        command_id,
        intake_id,
        workspace_token,
        owner,
        images_sha256,
        fields_sha256,
        review_request_id,
        review_result_sha256,
        revision,
        generation,
        discrepancy_text,
        confirmed_at,
        verified,
    ) = row
    if not (
        isinstance(command_id, str)
        and isinstance(intake_id, str)
        and isinstance(workspace_token, str)
        and isinstance(owner, str)
        and isinstance(review_request_id, str)
        and isinstance(discrepancy_text, str)
        and command_id
        and intake_id
        and workspace_token
        and owner
        and review_request_id
        and type(revision) is int
        and revision >= 1
        and type(generation) is int
        and generation >= 1
        and type(verified) is int
        and verified == 0
    ):
        raise StorageCorruption("malformed ticket intake confirmation")
    try:
        images = _digest(images_sha256, "images digest")
        fields = _digest(fields_sha256, "fields digest")
        review_result = _digest(review_result_sha256, "review result digest")
        discrepancy = DiscrepancyStatus(discrepancy_text)
    except (TypeError, ValueError) as exc:
        raise StorageCorruption("malformed ticket intake confirmation") from exc
    return CustomerConfirmation(
        command_id,
        intake_id,
        workspace_token,
        owner,
        images,
        fields,
        review_request_id,
        review_result,
        revision,
        generation,
        discrepancy,
        _parse_time(confirmed_at, "ticket intake confirmation time"),
        False,
    )


def _claim_reservation(database: sqlite3.Connection, claim: OperationClaim) -> IntakeReservation:
    reservation = _load_reservation(database, "intake_id", claim.intake_id)
    if (
        reservation is None
        or reservation.workspace_token != claim.workspace_token
        or reservation.owner != claim.owner
    ):
        raise StorageCorruption("ticket intake claim subject mismatch")
    return reservation


def _validate_confirmation(
    database: sqlite3.Connection, confirmation: CustomerConfirmation
) -> None:
    reservation = _load_reservation(database, "intake_id", confirmation.intake_id)
    if (
        reservation is None
        or reservation.workspace_token != confirmation.workspace_token
        or reservation.owner != confirmation.owner
    ):
        raise StorageCorruption("ticket intake confirmation subject mismatch")
    row = database.execute(
        f"SELECT {_CLAIM_COLUMNS} FROM pb_ticket_intake_operations WHERE request_id=?",
        (confirmation.review_request_id,),
    ).fetchone()
    if row is None:
        raise StorageCorruption("ticket intake confirmation review missing")
    review = _claim(row)
    _claim_reservation(database, review)
    if (
        review.intake_id != confirmation.intake_id
        or review.operation is not IntakeOperation.REVIEW
        or review.status is not ClaimStatus.SUCCESS
        or review.result_json is None
        or _digest_text(review.result_json) != confirmation.review_result_sha256
        or review.binding_sha256
        != review_confirmation_binding(
            confirmation.images_sha256,
            confirmation.fields_sha256,
            customer_supplied=_customer_review_result(review.result_json),
        )
    ):
        raise StorageCorruption("ticket intake confirmation review mismatch")


def ticket_intake_reservation(
    database: sqlite3.Connection, workspace_token: str
) -> IntakeReservation | None:
    """Read the reservation for one workspace, or None when it has none yet."""
    return _load_reservation(
        database, "workspace_token", _opaque(workspace_token, "workspace token")
    )


def ticket_intake_operation_claim(
    database: sqlite3.Connection, request_id: str
) -> OperationClaim | None:
    """Read one immutable claim by its globally unique caller request id."""
    request = _opaque(request_id, "request id")
    row = database.execute(
        f"SELECT {_CLAIM_COLUMNS} FROM pb_ticket_intake_operations WHERE request_id=?",
        (request,),
    ).fetchone()
    if row is None:
        alias = _alias_for(database, request)
        if alias is None:
            return None
        row = database.execute(
            f"SELECT {_CLAIM_COLUMNS} FROM pb_ticket_intake_operations WHERE request_id=?",
            (alias.claim_request_id,),
        ).fetchone()
        if row is None:
            raise StorageCorruption("ticket intake request alias has no claim")
    claim = _claim(row)
    _claim_reservation(database, claim)
    return claim


def ticket_intake_claims(
    database: sqlite3.Connection, intake_id: str
) -> tuple[OperationClaim, ...]:
    """Read every claim for one reserved intake subject in claim order."""
    rows = database.execute(
        f"SELECT {_CLAIM_COLUMNS} FROM pb_ticket_intake_operations "
        "WHERE intake_id=? ORDER BY rowid",
        (_opaque(intake_id, "intake id"),),
    ).fetchall()
    claims = tuple(_claim(row) for row in rows)
    for claim in claims:
        _claim_reservation(database, claim)
    return claims


def latest_ticket_intake_confirmation(
    database: sqlite3.Connection, intake_id: str
) -> CustomerConfirmation | None:
    """Read the most recently recorded confirmation for one subject, without judging it."""
    row = database.execute(
        f"SELECT {_CONFIRMATION_COLUMNS} FROM pb_ticket_intake_confirmations "
        "WHERE intake_id=? ORDER BY rowid DESC LIMIT 1",
        (_opaque(intake_id, "intake id"),),
    ).fetchone()
    if row is None:
        return None
    confirmation = _confirmation(row)
    _validate_confirmation(database, confirmation)
    return confirmation


def _customer_review_result(result_json: str | None) -> bool:
    if result_json is None:
        return False
    try:
        data = json.loads(result_json)
    except (ValueError, TypeError):
        raise StorageCorruption("malformed review result") from None
    return isinstance(data, dict) and data.get("customer_supplied") is True


def review_confirmation_binding(
    images_sha256: str, fields_sha256: str, *, customer_supplied: bool = False
) -> str:
    """Domain-separated SHA256 binding of the exact ordered images plus closed fields.

    A REVIEW operation claim uses this value as its ``binding_sha256``, and a recorded
    customer confirmation must reference that same claim. This deliberately differs from
    any exact-input hash computed by a provider reader: orchestration hashes the actual
    owner images and closed facts with this precise domain prefix before claiming.
    """
    images = _digest(images_sha256, "images digest")
    fields = _digest(fields_sha256, "fields digest")
    payload = json.dumps(
        [
            "ticket-customer-confirmation-v1" if customer_supplied else "ticket-review-binding-v1",
            images,
            fields,
        ],
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def reserve_ticket_intake(
    database: sqlite3.Connection,
    proposed_intake_id: str,
    workspace_token: str,
    owner: str,
    now: datetime,
) -> IntakeReservation:
    """Atomically return the one immutable reservation for an owned workspace.

    A caller-generated ``proposed_intake_id`` is used only when the workspace has no
    reservation and no already-submitted intake record. Two callers for the same owned
    workspace always receive the same id; a different owner, unknown workspace, an id
    owned by another workspace, or an id already bound elsewhere is refused.
    """
    proposed = _opaque(proposed_intake_id, "intake id")
    workspace = _opaque(workspace_token, "workspace token")
    actor = _opaque(owner, "owner")
    at = _utc(now)
    _begin(database)
    try:
        owner_row = database.execute(
            "SELECT owner FROM pb_intake_owners WHERE workspace_token=?", (workspace,)
        ).fetchone()
        if owner_row is None:
            raise TicketIntakeConflict("workspace_not_found")
        if not isinstance(owner_row[0], str) or owner_row[0] != actor:
            raise TicketIntakeConflict("workspace_owned_by_another_customer")
        existing = _load_reservation(database, "workspace_token", workspace)
        if existing is not None:
            database.execute("COMMIT")
            return existing
        chosen = proposed
        if _table_exists(database, "pb_intake_records"):
            record = database.execute(
                "SELECT intake_id FROM pb_intake_records WHERE workspace_token=?",
                (workspace,),
            ).fetchone()
            if record is not None:
                if not isinstance(record[0], str) or not record[0]:
                    raise StorageCorruption("malformed intake record")
                chosen = record[0]
        reserved_elsewhere = _load_reservation(database, "intake_id", chosen)
        if reserved_elsewhere is not None and reserved_elsewhere.workspace_token != workspace:
            raise TicketIntakeConflict("intake_id_already_reserved")
        if _table_exists(database, "pb_intake_records"):
            used = database.execute(
                "SELECT workspace_token FROM pb_intake_records WHERE intake_id=?", (chosen,)
            ).fetchone()
            if used is not None and used[0] != workspace:
                raise TicketIntakeConflict("intake_id_already_submitted")
        try:
            database.execute(
                "INSERT INTO pb_ticket_intake_reservations "
                "(workspace_token, intake_id, owner, reserved_at) VALUES (?, ?, ?, ?)",
                (workspace, chosen, actor, at.isoformat()),
            )
        except sqlite3.IntegrityError:
            raise TicketIntakeConflict("intake_reservation_conflict") from None
        stored = _load_reservation(database, "workspace_token", workspace)
        if stored is None:
            raise StorageCorruption("ticket intake reservation was not stored")
        database.execute("COMMIT")
        return stored
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def claim_ticket_intake_operation(
    database: sqlite3.Connection,
    request_id: str,
    intake_id: str,
    operation: IntakeOperation | str,
    binding_sha256: str,
    now: datetime,
    *,
    workspace_token: str | None = None,
    owner: str | None = None,
    journey_identity: JourneyBuildIdentity | None = None,
) -> OperationClaim:
    """Persist one paid-operation claim before any provider call.

    The first caller for a ``(subject, operation, binding)`` tuple receives the raw
    ``claim_token`` (and therefore ``should_call``). A replay through the same request id,
    or through a new request id with the same subject/operation/binding, receives the
    existing claim without a token, so it is never told to call the provider again.
    """
    request = _opaque(request_id, "request id")
    subject = _opaque(intake_id, "intake id")
    kind = _operation(operation)
    binding = _digest(binding_sha256, "binding digest")
    at = _utc(now)
    supplied_workspace = (
        None if workspace_token is None else _opaque(workspace_token, "workspace token")
    )
    supplied_owner = None if owner is None else _opaque(owner, "owner")
    identity = normalize_journey_identity(journey_identity)
    _begin(database)
    try:
        reservation = _load_reservation(database, "intake_id", subject)
        if reservation is None:
            raise TicketIntakeConflict("intake_not_reserved")
        if supplied_workspace is not None and supplied_workspace != reservation.workspace_token:
            raise TicketIntakeConflict("workspace_mismatch")
        if supplied_owner is not None and supplied_owner != reservation.owner:
            raise TicketIntakeConflict("owner_mismatch")
        existing = database.execute(
            f"SELECT {_CLAIM_COLUMNS} FROM pb_ticket_intake_operations WHERE request_id=?",
            (request,),
        ).fetchone()
        if existing is not None:
            claim = _claim(existing)
            if (
                claim.intake_id != subject
                or claim.workspace_token != reservation.workspace_token
                or claim.owner != reservation.owner
                or claim.operation is not kind
                or claim.binding_sha256 != binding
            ):
                raise TicketIntakeConflict("request_id_reused_for_another_claim")
            database.execute("COMMIT")
            return claim
        alias = _alias_for(database, request)
        if alias is not None:
            if (
                alias.intake_id != subject
                or alias.operation is not kind
                or alias.binding_sha256 != binding
            ):
                raise TicketIntakeConflict("request_id_reused_for_another_claim")
            canonical = database.execute(
                f"SELECT {_CLAIM_COLUMNS} FROM pb_ticket_intake_operations WHERE request_id=?",
                (alias.claim_request_id,),
            ).fetchone()
            if canonical is None:
                raise StorageCorruption("ticket intake request alias has no claim")
            claim = _claim(canonical)
            _claim_reservation(database, claim)
            if (
                claim.intake_id != subject
                or claim.operation is not kind
                or claim.binding_sha256 != binding
            ):
                raise StorageCorruption("ticket intake request alias claim mismatch")
            database.execute("COMMIT")
            return claim
        same_attempt = database.execute(
            f"SELECT {_CLAIM_COLUMNS} FROM pb_ticket_intake_operations "
            "WHERE intake_id=? AND operation=? AND binding_sha256=?",
            (subject, kind.value, binding),
        ).fetchone()
        if same_attempt is not None:
            claim = _claim(same_attempt)
            _claim_reservation(database, claim)
            _store_alias(database, request, claim, at)
            database.execute("COMMIT")
            return claim
        raw_token = secrets.token_urlsafe(32)
        try:
            database.execute(
                "INSERT INTO pb_ticket_intake_operations "
                "(request_id, intake_id, workspace_token, owner, operation, binding_sha256, "
                "claim_token_sha256, status, result_json, usage_json, claimed_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, NULL)",
                (
                    request,
                    subject,
                    reservation.workspace_token,
                    reservation.owner,
                    kind.value,
                    binding,
                    _digest_text(raw_token),
                    at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError:
            raise TicketIntakeConflict("operation_claim_conflict") from None
        row = database.execute(
            f"SELECT {_CLAIM_COLUMNS} FROM pb_ticket_intake_operations WHERE request_id=?",
            (request,),
        ).fetchone()
        if row is None:
            raise StorageCorruption("ticket intake claim was not stored")
        ensure_journey_identity_in_transaction(
            database,
            workspace_token=reservation.workspace_token,
            at=at,
            basis=CaptureStartBasis.FIRST_DURABLE_ATTEMPT,
            identity=identity,
        )
        claim = _claim(row, claim_token=raw_token)
        database.execute("COMMIT")
        return claim
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def complete_ticket_intake_operation(
    database: sqlite3.Connection,
    request_id: str,
    status: ClaimStatus | str,
    result_json: str | None,
    usage_json: str | None,
    now: datetime,
    *,
    claim_token: str | None = None,
    intake_id: str | None = None,
    operation: IntakeOperation | str | None = None,
    binding_sha256: str | None = None,
    ocr_observation: OcrObservationInput | None = None,
    journey_identity: JourneyBuildIdentity | None = None,
) -> OperationClaim:
    """Complete exactly one pending claim once, replaying an identical completion.

    ``unknown`` never accepts a result. A completed claim cannot be changed, and an
    identical completion returns the stored bytes rather than rewriting the timestamp.
    """
    request = _opaque(request_id, "request id")
    final_status = _status(status)
    if final_status is ClaimStatus.PENDING:
        raise ValueError("cannot complete a claim as pending")
    at = _utc(now)
    stored_result = _json_text(
        result_json, field_name="ticket intake result", max_bytes=MAX_RESULT_BYTES
    )
    stored_usage = _json_text(
        usage_json, field_name="ticket intake usage", max_bytes=MAX_USAGE_BYTES
    )
    if final_status is ClaimStatus.UNKNOWN and stored_result is not None:
        raise ValueError("unknown completion has no result")
    if final_status is ClaimStatus.SUCCESS and stored_result is None:
        raise ValueError("successful completion needs a result")
    supplied_token = None if claim_token is None else _raw_token(claim_token)
    supplied_subject = None if intake_id is None else _opaque(intake_id, "intake id")
    supplied_operation = None if operation is None else _operation(operation)
    supplied_binding = None if binding_sha256 is None else _digest(binding_sha256, "binding digest")
    identity = normalize_journey_identity(journey_identity)
    _begin(database)
    try:
        canonical_request = request
        row = database.execute(
            f"SELECT {_CLAIM_COLUMNS} FROM pb_ticket_intake_operations WHERE request_id=?",
            (request,),
        ).fetchone()
        alias: _ClaimAlias | None = None
        if row is None:
            alias = _alias_for(database, request)
            if alias is None:
                raise TicketIntakeConflict("claim_not_found")
            canonical_request = alias.claim_request_id
            row = database.execute(
                f"SELECT {_CLAIM_COLUMNS} FROM pb_ticket_intake_operations WHERE request_id=?",
                (canonical_request,),
            ).fetchone()
            if row is None:
                raise StorageCorruption("ticket intake request alias has no claim")
        token_row = database.execute(
            "SELECT claim_token_sha256 FROM pb_ticket_intake_operations WHERE request_id=?",
            (canonical_request,),
        ).fetchone()
        if token_row is None or not isinstance(token_row[0], str):
            raise StorageCorruption("malformed ticket intake claim token")
        claim = _claim(row)
        _claim_reservation(database, claim)
        if alias is not None and (
            alias.intake_id != claim.intake_id
            or alias.operation is not claim.operation
            or alias.binding_sha256 != claim.binding_sha256
        ):
            raise StorageCorruption("ticket intake request alias claim mismatch")
        if supplied_token is None:
            if claim.status is ClaimStatus.PENDING:
                raise TicketIntakeConflict("claim_token_required")
        elif _digest_text(supplied_token) != token_row[0]:
            raise TicketIntakeConflict("claim_token_mismatch")
        if (
            (supplied_subject is not None and claim.intake_id != supplied_subject)
            or (supplied_operation is not None and claim.operation is not supplied_operation)
            or (supplied_binding is not None and claim.binding_sha256 != supplied_binding)
        ):
            raise TicketIntakeConflict("claim_identity_mismatch")
        if at < claim.claimed_at:
            raise TicketIntakeConflict("clock_moved_backwards")
        if claim.status is not ClaimStatus.PENDING:
            if (
                claim.status is final_status
                and claim.result_json == stored_result
                and claim.usage_json == stored_usage
            ):
                if ocr_observation is not None:
                    evidence = database.execute(
                        "SELECT journey_id FROM pb_ticket_ocr_observations WHERE request_id=?",
                        (claim.request_id,),
                    ).fetchone()
                    if evidence is None:
                        raise TicketIntakeConflict("completed_observation_cannot_be_backfilled")
                    record_ocr_observation_in_transaction(
                        database,
                        request_id=claim.request_id,
                        intake_id=claim.intake_id,
                        workspace_token=claim.workspace_token,
                        journey_id=evidence[0],
                        operation_status=claim.status.value,
                        observation=ocr_observation,
                        recorded_at=at,
                    )
                database.execute("COMMIT")
                return claim
            raise TicketIntakeConflict("claim_already_completed_differently")
        try:
            database.execute(
                "UPDATE pb_ticket_intake_operations "
                "SET status=?, result_json=?, usage_json=?, completed_at=? "
                "WHERE request_id=? AND status='pending'",
                (
                    final_status.value,
                    stored_result,
                    stored_usage,
                    at.isoformat(),
                    canonical_request,
                ),
            )
        except sqlite3.IntegrityError:
            raise TicketIntakeConflict("operation_completion_conflict") from None
        completed_row = database.execute(
            f"SELECT {_CLAIM_COLUMNS} FROM pb_ticket_intake_operations WHERE request_id=?",
            (canonical_request,),
        ).fetchone()
        if completed_row is None:
            raise StorageCorruption("ticket intake claim disappeared")
        completed = _claim(completed_row)
        if (
            completed.status is not final_status
            or completed.result_json != stored_result
            or completed.usage_json != stored_usage
        ):
            raise StorageCorruption("ticket intake completion did not persist")
        if completed.operation is IntakeOperation.EXTRACT:
            assert completed.completed_at is not None
            if completed.status is ClaimStatus.SUCCESS:
                kind, outcome = JourneyEventKind.TICKET_READ, JourneyEventOutcome.SUCCEEDED
            elif completed.status is ClaimStatus.REFUSED:
                kind, outcome = JourneyEventKind.TICKET_READ_REFUSED, JourneyEventOutcome.REFUSED
            else:
                kind, outcome = JourneyEventKind.TICKET_READ_UNKNOWN, JourneyEventOutcome.UNKNOWN
            cost_cents, cost_status = extract_usage_cost(
                completed.usage_json,
                force_unknown=completed.status is ClaimStatus.UNKNOWN,
            )
            duration_ms = (completed.completed_at - completed.claimed_at) // timedelta(
                milliseconds=1
            )
            journey_event = append_journey_event_in_transaction(
                database,
                workspace_token=completed.workspace_token,
                occurrence_key=f"ticket_read:{completed.request_id}",
                event=JourneyEventInput(
                    kind=kind,
                    outcome=outcome,
                    occurred_at=completed.completed_at,
                    duration_ms=duration_ms,
                    cost_cents=cost_cents,
                    cost_status=cost_status,
                    context=JourneyEventContext(
                        operation=ContextOperation.OCR_EXTRACT,
                        build_sha256=identity.build_sha256,
                        config_sha256=identity.config_sha256,
                        reason=(
                            ContextReason.NOT_APPLICABLE
                            if completed.status is ClaimStatus.SUCCESS
                            else ContextReason.READING_REFUSED
                            if completed.status is ClaimStatus.REFUSED
                            else ContextReason.READING_RESULT_UNKNOWN
                        ),
                        fact_route=ContextFactRoute.NOT_APPLICABLE,
                        duration_basis=DurationBasis.SERVER_WALL_ELAPSED,
                        operation_ref=opaque_reference(
                            "op", f"ocr_extract:{completed.intake_id}:{completed.binding_sha256}"
                        ),
                        attempt_ref=opaque_reference(
                            "att", f"ticket_intake:{completed.request_id}"
                        ),
                    ),
                ),
            )
            if ocr_observation is not None:
                record_ocr_observation_in_transaction(
                    database,
                    request_id=completed.request_id,
                    intake_id=completed.intake_id,
                    workspace_token=completed.workspace_token,
                    journey_id=journey_event.journey_id,
                    operation_status=completed.status.value,
                    observation=ocr_observation,
                    recorded_at=journey_event.recorded_at,
                )
        database.execute("COMMIT")
        return completed
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def record_ticket_intake_confirmation(
    database: sqlite3.Connection,
    command_id: str,
    intake_id: str,
    images_sha256: str,
    fields_sha256: str,
    review_request_id: str,
    revision: int,
    generation: int,
    discrepancy_status: DiscrepancyStatus | str,
    now: datetime,
    *,
    workspace_token: str | None = None,
    owner: str | None = None,
    journey_identity: JourneyBuildIdentity | None = None,
) -> CustomerConfirmation:
    """Append one hash-only customer confirmation bound to a completed review result.

    No customer text or reason is accepted. Repeating the same command id with the exact
    same payload returns the first row; any changed payload is refused. Changed photos,
    fields, review, revision or generation are judged by :func:`read_ticket_intake_confirmation`.
    A distinct new command must strictly advance ``generation`` while keeping ``revision``
    non-decreasing; ordering is componentwise, never one lexicographic tuple.
    """
    command = _opaque(command_id, "command id")
    subject = _opaque(intake_id, "intake id")
    images = _digest(images_sha256, "images digest")
    fields = _digest(fields_sha256, "fields digest")
    review_request = _opaque(review_request_id, "review request id")
    revision_value = _revision(revision, "revision")
    generation_value = _revision(generation, "generation")
    discrepancy = _discrepancy(discrepancy_status)
    at = _utc(now)
    supplied_workspace = (
        None if workspace_token is None else _opaque(workspace_token, "workspace token")
    )
    supplied_owner = None if owner is None else _opaque(owner, "owner")
    identity = normalize_journey_identity(journey_identity)
    _begin(database)
    try:
        reservation = _load_reservation(database, "intake_id", subject)
        if reservation is None:
            raise TicketIntakeConflict("intake_not_reserved")
        if supplied_workspace is not None and supplied_workspace != reservation.workspace_token:
            raise TicketIntakeConflict("workspace_mismatch")
        if supplied_owner is not None and supplied_owner != reservation.owner:
            raise TicketIntakeConflict("owner_mismatch")
        review_row = database.execute(
            f"SELECT {_CLAIM_COLUMNS} FROM pb_ticket_intake_operations WHERE request_id=?",
            (review_request,),
        ).fetchone()
        if review_row is None:
            raise TicketIntakeConflict("review_not_found")
        review = _claim(review_row)
        _claim_reservation(database, review)
        if (
            review.intake_id != subject
            or review.operation is not IntakeOperation.REVIEW
            or review.status is not ClaimStatus.SUCCESS
            or review.result_json is None
        ):
            raise TicketIntakeConflict("review_result_not_available")
        if at < review.claimed_at:
            raise TicketIntakeConflict("clock_moved_backwards")
        review_result_sha256 = _digest_text(review.result_json)
        existing = database.execute(
            f"SELECT {_CONFIRMATION_COLUMNS} FROM pb_ticket_intake_confirmations "
            "WHERE command_id=?",
            (command,),
        ).fetchone()
        if existing is not None:
            confirmation = _confirmation(existing)
            if (
                confirmation.intake_id == subject
                and confirmation.workspace_token == reservation.workspace_token
                and confirmation.owner == reservation.owner
                and confirmation.images_sha256 == images
                and confirmation.fields_sha256 == fields
                and confirmation.review_request_id == review_request
                and confirmation.review_result_sha256 == review_result_sha256
                and confirmation.revision == revision_value
                and confirmation.generation == generation_value
                and confirmation.discrepancy_status is discrepancy
            ):
                database.execute("COMMIT")
                return confirmation
            raise TicketIntakeConflict("confirmation_command_reused")
        result_data = json.loads(review.result_json)
        customer_supplied = (
            isinstance(result_data, dict) and result_data.get("customer_supplied") is True
        )
        if review.binding_sha256 != review_confirmation_binding(
            images, fields, customer_supplied=customer_supplied
        ):
            raise TicketIntakeConflict("review_binding_mismatch")
        latest_row = database.execute(
            f"SELECT {_CONFIRMATION_COLUMNS} FROM pb_ticket_intake_confirmations "
            "WHERE intake_id=? ORDER BY rowid DESC LIMIT 1",
            (subject,),
        ).fetchone()
        if latest_row is not None:
            latest = _confirmation(latest_row)
            _validate_confirmation(database, latest)
            # Ordering is componentwise, never one lexicographic tuple: a new generation
            # must strictly advance and the workspace revision may not regress. Reusing
            # the same generation for a distinct command is refused even if identical.
            if generation_value < latest.generation or revision_value < latest.revision:
                raise TicketIntakeConflict("confirmation_revision_stale")
            if generation_value == latest.generation:
                raise TicketIntakeConflict("confirmation_generation_conflict")
        try:
            database.execute(
                "INSERT INTO pb_ticket_intake_confirmations "
                "(command_id, intake_id, workspace_token, owner, images_sha256, fields_sha256, "
                "review_request_id, review_result_sha256, revision, generation, "
                "discrepancy_status, confirmed_at, verified) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    command,
                    subject,
                    reservation.workspace_token,
                    reservation.owner,
                    images,
                    fields,
                    review_request,
                    review_result_sha256,
                    revision_value,
                    generation_value,
                    discrepancy.value,
                    at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError:
            raise TicketIntakeConflict("confirmation_conflict") from None
        row = database.execute(
            f"SELECT {_CONFIRMATION_COLUMNS} FROM pb_ticket_intake_confirmations "
            "WHERE command_id=?",
            (command,),
        ).fetchone()
        if row is None:
            raise StorageCorruption("ticket intake confirmation was not stored")
        confirmation = _confirmation(row)
        append_journey_event_in_transaction(
            database,
            workspace_token=confirmation.workspace_token,
            occurrence_key=f"facts_confirmed:{confirmation.intake_id}:{confirmation.generation}",
            event=JourneyEventInput(
                kind=JourneyEventKind.FACTS_CONFIRMED,
                outcome=JourneyEventOutcome.SUCCEEDED,
                occurred_at=confirmation.confirmed_at,
                cost_status=CostStatus.NOT_APPLICABLE,
                context=JourneyEventContext(
                    operation=ContextOperation.FACT_CONFIRMATION,
                    build_sha256=identity.build_sha256,
                    config_sha256=identity.config_sha256,
                    reason=ContextReason.NOT_APPLICABLE,
                    fact_route=(
                        ContextFactRoute.CUSTOMER_SUPPLIED
                        if customer_supplied
                        else ContextFactRoute.OCR
                    ),
                    duration_basis=DurationBasis.NOT_APPLICABLE,
                    operation_ref=opaque_reference(
                        "op",
                        f"fact_confirmation:{confirmation.intake_id}:{confirmation.generation}",
                    ),
                    attempt_ref=opaque_reference(
                        "att", f"fact_confirmation:{confirmation.command_id}"
                    ),
                ),
            ),
        )
        database.execute("COMMIT")
        return confirmation
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def read_ticket_intake_confirmation(
    database: sqlite3.Connection,
    intake_id: str,
    *,
    images_sha256: str,
    fields_sha256: str,
    revision: int,
    generation: int,
    review_request_id: str | None = None,
    workspace_token: str | None = None,
    owner: str | None = None,
) -> ConfirmationRead:
    """Judge the latest confirmation against the caller's *current* binding.

    A stale result is a report, never adoption: the caller keeps the original confirmation
    but must not use it for the changed photos, fields, review result or revision.
    """
    subject = _opaque(intake_id, "intake id")
    images = _digest(images_sha256, "images digest")
    fields = _digest(fields_sha256, "fields digest")
    revision_value = _revision(revision, "revision")
    generation_value = _revision(generation, "generation")
    supplied_review = (
        None if review_request_id is None else _opaque(review_request_id, "review request id")
    )
    supplied_workspace = (
        None if workspace_token is None else _opaque(workspace_token, "workspace token")
    )
    supplied_owner = None if owner is None else _opaque(owner, "owner")
    confirmation = latest_ticket_intake_confirmation(database, subject)
    if confirmation is None:
        return ConfirmationRead(ConfirmationState.MISSING, None, "no_confirmation")
    reason: str | None = None
    if supplied_workspace is not None and confirmation.workspace_token != supplied_workspace:
        reason = "workspace_changed"
    elif supplied_owner is not None and confirmation.owner != supplied_owner:
        reason = "owner_changed"
    elif confirmation.images_sha256 != images:
        reason = "images_changed"
    elif confirmation.fields_sha256 != fields:
        reason = "fields_changed"
    elif supplied_review is not None and confirmation.review_request_id != supplied_review:
        reason = "review_changed"
    elif confirmation.revision != revision_value:
        reason = "revision_changed"
    elif confirmation.generation != generation_value:
        reason = "generation_changed"
    if reason is not None:
        return ConfirmationRead(ConfirmationState.STALE, confirmation, reason)
    return ConfirmationRead(ConfirmationState.CURRENT, confirmation, None)


def _connection(connection: ConnectionSource) -> sqlite3.Connection:
    if isinstance(connection, sqlite3.Connection):
        return connection
    if not isinstance(connection, _ConnectionOwner):
        raise TypeError("ticket intake needs the authority SQLite connection")
    database = connection._require_open()
    if not isinstance(database, sqlite3.Connection):
        raise TypeError("ticket intake needs the authority SQLite connection")
    return database


class TicketIntakeStore:
    """Thin injectable facade over the module-level functions and one authority connection."""

    def __init__(self, connection: ConnectionSource) -> None:
        self._connection_source = connection

    def _database(self) -> sqlite3.Connection:
        return _connection(self._connection_source)

    def initialize(self) -> None:
        initialize_ticket_intake(self._database())

    def reservation_for(self, workspace_token: str) -> IntakeReservation | None:
        return ticket_intake_reservation(self._database(), workspace_token)

    def reserve_intake(
        self,
        proposed_intake_id: str,
        workspace_token: str,
        owner: str,
        now: datetime,
    ) -> IntakeReservation:
        return reserve_ticket_intake(
            self._database(), proposed_intake_id, workspace_token, owner, now
        )

    def reserve_intake_id(
        self,
        proposed_intake_id: str,
        workspace_token: str,
        owner: str,
        now: datetime,
    ) -> IntakeReservation:
        return self.reserve_intake(proposed_intake_id, workspace_token, owner, now)

    def operation_claim(self, request_id: str) -> OperationClaim | None:
        return ticket_intake_operation_claim(self._database(), request_id)

    def claims_for(self, intake_id: str) -> tuple[OperationClaim, ...]:
        return ticket_intake_claims(self._database(), intake_id)

    def claim_operation(
        self,
        request_id: str,
        intake_id: str,
        operation: IntakeOperation | str,
        binding_sha256: str,
        now: datetime,
        *,
        workspace_token: str | None = None,
        owner: str | None = None,
    ) -> OperationClaim:
        return claim_ticket_intake_operation(
            self._database(),
            request_id,
            intake_id,
            operation,
            binding_sha256,
            now,
            workspace_token=workspace_token,
            owner=owner,
            journey_identity=runtime_journey_identity(self._connection_source),
        )

    def begin_operation(
        self,
        request_id: str,
        intake_id: str,
        operation: IntakeOperation | str,
        binding_sha256: str,
        now: datetime,
        *,
        workspace_token: str | None = None,
        owner: str | None = None,
    ) -> OperationClaim:
        return self.claim_operation(
            request_id,
            intake_id,
            operation,
            binding_sha256,
            now,
            workspace_token=workspace_token,
            owner=owner,
        )

    def complete_operation(
        self,
        request_id: str,
        status: ClaimStatus | str,
        result_json: str | None,
        usage_json: str | None,
        now: datetime,
        *,
        claim_token: str | None = None,
        intake_id: str | None = None,
        operation: IntakeOperation | str | None = None,
        binding_sha256: str | None = None,
        ocr_observation: OcrObservationInput | None = None,
    ) -> OperationClaim:
        return complete_ticket_intake_operation(
            self._database(),
            request_id,
            status,
            result_json,
            usage_json,
            now,
            claim_token=claim_token,
            intake_id=intake_id,
            operation=operation,
            binding_sha256=binding_sha256,
            ocr_observation=ocr_observation,
            journey_identity=runtime_journey_identity(self._connection_source),
        )

    def finish_operation(
        self,
        request_id: str,
        status: ClaimStatus | str,
        result_json: str | None,
        usage_json: str | None,
        now: datetime,
        *,
        claim_token: str | None = None,
        intake_id: str | None = None,
        operation: IntakeOperation | str | None = None,
        binding_sha256: str | None = None,
        ocr_observation: OcrObservationInput | None = None,
    ) -> OperationClaim:
        return self.complete_operation(
            request_id,
            status,
            result_json,
            usage_json,
            now,
            claim_token=claim_token,
            intake_id=intake_id,
            operation=operation,
            binding_sha256=binding_sha256,
            ocr_observation=ocr_observation,
        )

    def record_confirmation(
        self,
        command_id: str,
        intake_id: str,
        images_sha256: str,
        fields_sha256: str,
        review_request_id: str,
        revision: int,
        generation: int,
        discrepancy_status: DiscrepancyStatus | str,
        now: datetime,
        *,
        workspace_token: str | None = None,
        owner: str | None = None,
    ) -> CustomerConfirmation:
        return record_ticket_intake_confirmation(
            self._database(),
            command_id,
            intake_id,
            images_sha256,
            fields_sha256,
            review_request_id,
            revision,
            generation,
            discrepancy_status,
            now,
            workspace_token=workspace_token,
            owner=owner,
            journey_identity=runtime_journey_identity(self._connection_source),
        )

    def read_confirmation(
        self,
        intake_id: str,
        *,
        images_sha256: str,
        fields_sha256: str,
        revision: int,
        generation: int,
        review_request_id: str | None = None,
        workspace_token: str | None = None,
        owner: str | None = None,
    ) -> ConfirmationRead:
        return read_ticket_intake_confirmation(
            self._database(),
            intake_id,
            images_sha256=images_sha256,
            fields_sha256=fields_sha256,
            revision=revision,
            generation=generation,
            review_request_id=review_request_id,
            workspace_token=workspace_token,
            owner=owner,
        )

    def current_confirmation(
        self,
        intake_id: str,
        *,
        images_sha256: str,
        fields_sha256: str,
        revision: int,
        generation: int,
        review_request_id: str | None = None,
        workspace_token: str | None = None,
        owner: str | None = None,
    ) -> ConfirmationRead:
        return self.read_confirmation(
            intake_id,
            images_sha256=images_sha256,
            fields_sha256=fields_sha256,
            revision=revision,
            generation=generation,
            review_request_id=review_request_id,
            workspace_token=workspace_token,
            owner=owner,
        )


class TicketIntakeStoreMixin:
    """Optional mixin for a store exposing ``_require_open``; no wiring is implied."""

    def reserve_ticket_intake(
        self: _ConnectionOwner,
        proposed_intake_id: str,
        workspace_token: str,
        owner: str,
        now: datetime,
    ) -> IntakeReservation:
        return reserve_ticket_intake(
            self._require_open(), proposed_intake_id, workspace_token, owner, now
        )

    def claim_ticket_intake_operation(
        self: _ConnectionOwner,
        request_id: str,
        intake_id: str,
        operation: IntakeOperation | str,
        binding_sha256: str,
        now: datetime,
        *,
        workspace_token: str | None = None,
        owner: str | None = None,
    ) -> OperationClaim:
        return claim_ticket_intake_operation(
            self._require_open(),
            request_id,
            intake_id,
            operation,
            binding_sha256,
            now,
            workspace_token=workspace_token,
            owner=owner,
            journey_identity=runtime_journey_identity(self),
        )

    def complete_ticket_intake_operation(
        self: _ConnectionOwner,
        request_id: str,
        status: ClaimStatus | str,
        result_json: str | None,
        usage_json: str | None,
        now: datetime,
        *,
        claim_token: str | None = None,
        intake_id: str | None = None,
        operation: IntakeOperation | str | None = None,
        binding_sha256: str | None = None,
        ocr_observation: OcrObservationInput | None = None,
    ) -> OperationClaim:
        return complete_ticket_intake_operation(
            self._require_open(),
            request_id,
            status,
            result_json,
            usage_json,
            now,
            claim_token=claim_token,
            intake_id=intake_id,
            operation=operation,
            binding_sha256=binding_sha256,
            ocr_observation=ocr_observation,
            journey_identity=runtime_journey_identity(self),
        )

    def record_ticket_intake_confirmation(
        self: _ConnectionOwner,
        command_id: str,
        intake_id: str,
        images_sha256: str,
        fields_sha256: str,
        review_request_id: str,
        revision: int,
        generation: int,
        discrepancy_status: DiscrepancyStatus | str,
        now: datetime,
        *,
        workspace_token: str | None = None,
        owner: str | None = None,
    ) -> CustomerConfirmation:
        return record_ticket_intake_confirmation(
            self._require_open(),
            command_id,
            intake_id,
            images_sha256,
            fields_sha256,
            review_request_id,
            revision,
            generation,
            discrepancy_status,
            now,
            workspace_token=workspace_token,
            owner=owner,
            journey_identity=runtime_journey_identity(self),
        )

    def read_ticket_intake_confirmation(
        self: _ConnectionOwner,
        intake_id: str,
        *,
        images_sha256: str,
        fields_sha256: str,
        revision: int,
        generation: int,
        review_request_id: str | None = None,
        workspace_token: str | None = None,
        owner: str | None = None,
    ) -> ConfirmationRead:
        return read_ticket_intake_confirmation(
            self._require_open(),
            intake_id,
            images_sha256=images_sha256,
            fields_sha256=fields_sha256,
            revision=revision,
            generation=generation,
            review_request_id=review_request_id,
            workspace_token=workspace_token,
            owner=owner,
        )
