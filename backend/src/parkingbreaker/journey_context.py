"""Closed, versioned context extending immutable journey facts on the same SQLite writer.

References are pseudonymous and restricted, not anonymous. No raw operation key,
provider/customer identifier, diagnostic narrative or arbitrary metadata is stored.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from parkingbreaker.mail_attempt_store import StorageCorruption

COMPONENT: Final = "journey_event_context"
SCHEMA_VERSION: Final = 1
MAX_CONTEXT_BYTES: Final = 4096
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REFERENCE = re.compile(r"(?:(?:op|att)_[0-9a-f]{64}|evt_[0-9a-f]{32})\Z")
_DDL = """CREATE TABLE pb_journey_event_context (
    event_id TEXT PRIMARY KEY NOT NULL REFERENCES pb_journey_events(event_id),
    context_json TEXT NOT NULL CHECK (length(context_json) <= 4096)
)"""


@dataclass(frozen=True)
class JourneyBuildIdentity:
    """Optional non-secret identity digests; absence means unknown, not a default build."""

    build_sha256: str | None = None
    config_sha256: str | None = None

    def __post_init__(self) -> None:
        for value in (self.build_sha256, self.config_sha256):
            if value is not None and (type(value) is not str or _SHA256.fullmatch(value) is None):
                raise ValueError("journey identity must contain only SHA-256 digests or unknown")


def normalize_journey_identity(identity: JourneyBuildIdentity | None) -> JourneyBuildIdentity:
    if identity is None:
        return JourneyBuildIdentity()
    if type(identity) is not JourneyBuildIdentity:
        raise ValueError("invalid journey build identity")
    return identity


@runtime_checkable
class _RuntimeIdentityOwner(Protocol):
    @property
    def journey_identity(self) -> JourneyBuildIdentity: ...


def runtime_journey_identity(source: object) -> JourneyBuildIdentity:
    """Read this process/store's declared identity, never an old journey's origin."""
    if isinstance(source, _RuntimeIdentityOwner):
        return normalize_journey_identity(source.journey_identity)
    return JourneyBuildIdentity()


class ContextOperation(StrEnum):
    OCR_EXTRACT = "ocr_extract"
    FACT_CONFIRMATION = "fact_confirmation"
    INTAKE_SUBMISSION = "intake_submission"
    PAYMENT_AUTHORIZATION = "payment_authorization"
    PACKET_APPROVAL = "packet_approval"
    UNKNOWN = "unknown"


class ContextReason(StrEnum):
    READING_REFUSED = "reading_refused"
    READING_RESULT_UNKNOWN = "reading_result_unknown"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class ContextFactRoute(StrEnum):
    OCR = "ocr"
    CUSTOMER_SUPPLIED = "customer_supplied"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class ContextFunding(StrEnum):
    PAID = "paid"
    FREE_BETA = "free_beta"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class DurationBasis(StrEnum):
    SERVER_WALL_ELAPSED = "server_wall_elapsed"
    SERVER_MONOTONIC = "server_monotonic"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class ReferenceState(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


def _enum[EnumT: StrEnum](value: object, kind: type[EnumT]) -> EnumT:
    if type(value) is kind:
        return value
    if type(value) is str:
        try:
            return kind(value)
        except ValueError:
            pass
    raise ValueError(f"invalid {kind.__name__}")


@dataclass(frozen=True)
class JourneyReference:
    status: ReferenceState = ReferenceState.UNKNOWN
    opaque_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not ReferenceState:
            raise ValueError("reference status must be ReferenceState")
        if self.status is ReferenceState.KNOWN:
            if type(self.opaque_id) is not str or _REFERENCE.fullmatch(self.opaque_id) is None:
                raise ValueError("known reference requires a bounded opaque identity")
        elif self.opaque_id is not None:
            raise ValueError("unknown/not-applicable reference must have no identity")

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status.value, "id": self.opaque_id}

    @classmethod
    def from_mapping(cls, value: object) -> JourneyReference:
        if not isinstance(value, Mapping) or set(value) != {"status", "id"}:
            raise ValueError("reference must match its closed schema")
        identity = value["id"]
        if identity is not None and type(identity) is not str:
            raise ValueError("invalid opaque reference")
        return cls(_enum(value["status"], ReferenceState), identity)


def opaque_reference(prefix: str, key: str) -> JourneyReference:
    """Domain-separated identity of an internal operation/attempt key, never its value."""
    if prefix not in {"op", "att"}:
        raise ValueError("invalid reference domain")
    if type(key) is not str or not key or len(key.encode("utf-8")) > 1024:
        raise ValueError("invalid internal reference key")
    digest = hashlib.sha256(
        b"pb-journey-reference-v1\x00" + prefix.encode("ascii") + b"\x00" + key.encode("utf-8")
    ).hexdigest()
    return JourneyReference(ReferenceState.KNOWN, f"{prefix}_{digest}")


@dataclass(frozen=True)
class JourneyEventContext:
    operation: ContextOperation = ContextOperation.UNKNOWN
    reason: ContextReason = ContextReason.UNKNOWN
    fact_route: ContextFactRoute = ContextFactRoute.UNKNOWN
    funding: ContextFunding = ContextFunding.UNKNOWN
    duration_basis: DurationBasis = DurationBasis.UNKNOWN
    operation_ref: JourneyReference = field(default_factory=JourneyReference)
    attempt_ref: JourneyReference = field(default_factory=JourneyReference)
    causation_ref: JourneyReference = field(default_factory=JourneyReference)
    build_sha256: str | None = None
    config_sha256: str | None = None
    schema_version: int = SCHEMA_VERSION
    capture_coverage: str = "limited"

    def __post_init__(self) -> None:
        for value, kind in (
            (self.operation, ContextOperation),
            (self.reason, ContextReason),
            (self.fact_route, ContextFactRoute),
            (self.funding, ContextFunding),
            (self.duration_basis, DurationBasis),
        ):
            if type(value) is not kind:
                raise ValueError("event context requires closed enum values")
        for reference, prefix in (
            (self.operation_ref, "op_"),
            (self.attempt_ref, "att_"),
            (self.causation_ref, "evt_"),
        ):
            if type(reference) is not JourneyReference:
                raise ValueError("event context requires typed references")
            if reference.opaque_id is not None and not reference.opaque_id.startswith(prefix):
                raise ValueError("event context reference has wrong domain")
        for identity_digest in (self.build_sha256, self.config_sha256):
            if identity_digest is not None and (
                type(identity_digest) is not str or _SHA256.fullmatch(identity_digest) is None
            ):
                raise ValueError("event identity must be SHA-256 or unknown")
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported journey context version")
        if type(self.capture_coverage) is not str or self.capture_coverage != "limited":
            raise ValueError("current event context cannot assert complete coverage")

    def validate_duration(self, duration_ms: int | None) -> None:
        measured = self.duration_basis in {
            DurationBasis.SERVER_WALL_ELAPSED,
            DurationBasis.SERVER_MONOTONIC,
        }
        if measured != (duration_ms is not None):
            raise ValueError("duration and recorded basis must agree")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "operation": self.operation.value,
            "reason": self.reason.value,
            "fact_route": self.fact_route.value,
            "funding": self.funding.value,
            "duration_basis": self.duration_basis.value,
            "operation_ref": self.operation_ref.to_dict(),
            "attempt_ref": self.attempt_ref.to_dict(),
            "causation_ref": self.causation_ref.to_dict(),
            "build_sha256": self.build_sha256,
            "build_status": "unknown" if self.build_sha256 is None else "known",
            "config_sha256": self.config_sha256,
            "config_status": "unknown" if self.config_sha256 is None else "known",
            "capture_coverage": self.capture_coverage,
        }

    @classmethod
    def from_mapping(cls, data: object) -> JourneyEventContext:
        if not isinstance(data, Mapping) or set(data) != set(cls().to_dict()):
            raise ValueError("event context must match its closed schema")
        result = cls(
            operation=_enum(data["operation"], ContextOperation),
            reason=_enum(data["reason"], ContextReason),
            fact_route=_enum(data["fact_route"], ContextFactRoute),
            funding=_enum(data["funding"], ContextFunding),
            duration_basis=_enum(data["duration_basis"], DurationBasis),
            operation_ref=JourneyReference.from_mapping(data["operation_ref"]),
            attempt_ref=JourneyReference.from_mapping(data["attempt_ref"]),
            causation_ref=JourneyReference.from_mapping(data["causation_ref"]),
            build_sha256=data["build_sha256"],
            config_sha256=data["config_sha256"],
            schema_version=data["schema_version"],
            capture_coverage=data["capture_coverage"],
        )
        if result.to_dict() != dict(data):
            raise ValueError("event context identity status must agree")
        return result

    def to_json(self) -> str:
        encoded = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        if len(encoded.encode("ascii")) > MAX_CONTEXT_BYTES:
            raise ValueError("event context exceeds its bound")
        return encoded


def decode_context(raw: object) -> JourneyEventContext | None:
    if raw is None:
        return None
    try:
        if type(raw) is not str or len(raw.encode("utf-8")) > MAX_CONTEXT_BYTES:
            raise ValueError("invalid stored context")
        return JourneyEventContext.from_mapping(json.loads(raw))
    except (ValueError, TypeError, RecursionError) as exc:
        raise StorageCorruption("malformed journey event context") from exc


def initialize_journey_event_context(database: sqlite3.Connection) -> None:
    """Extend the existing ledger initializer transaction; never backfill old context."""
    if not database.in_transaction:
        raise StorageCorruption("journey context initialization requires active transaction")
    version = database.execute(
        "SELECT version FROM pb_store_versions WHERE component=?", (COMPONENT,)
    ).fetchone()
    table = database.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='pb_journey_event_context'"
    ).fetchone()
    if version is None:
        if table is not None:
            raise StorageCorruption("unversioned journey context schema")
        database.execute(_DDL)
        database.execute("INSERT INTO pb_store_versions VALUES (?, ?)", (COMPONENT, SCHEMA_VERSION))
    elif (
        version != (SCHEMA_VERSION,)
        or table is None
        or (" ".join(table[0].upper().split()) != " ".join(_DDL.upper().split()))
    ):
        raise StorageCorruption("malformed journey context schema")
    for action in ("UPDATE", "DELETE"):
        database.execute(
            f"CREATE TRIGGER IF NOT EXISTS pb_journey_event_context_no_{action.lower()} "
            f"BEFORE {action} ON pb_journey_event_context "
            "BEGIN SELECT RAISE(ABORT, 'journey context is immutable'); END"
        )
