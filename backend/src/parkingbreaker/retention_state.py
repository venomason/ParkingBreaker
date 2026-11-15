"""Durable explicit retention policy, case closure and preservation-hold state (T36).

This component stores only facts a caller supplies explicitly: immutable policy versions,
one append-only closure or abandonment anchor per case generation, and identified
scope-specific preservation holds that never expire by themselves. It binds every record to
an existing ``pb_cases.case_token`` on the caller's own database connection and never opens a
sidecar database.

Every mutation takes a caller-chosen command id. Repeating the identical request returns the
saved outcome; reusing the id for any different field, timestamp, case or expected generation
is refused. A ``BEGIN IMMEDIATE`` transaction makes the claim, the state change and the
append-only command log atomic, and a caller-active transaction is refused instead of being
committed. Immutable rows and log rows are protected by triggers. A hold identity is
``(case_token, hold_id)`` and keeps the scope of its first assertion: a different scope needs a
different hold id, and an omitted expected generation only ever creates a never-seen hold.

Evaluation stays pure: snapshots carry the stored policy revision, closure anchor and holds,
but never runtime blockers. A later evaluator must recompute those from live state through
``parkingbreaker.retention_policy.evaluate_service_retention``. Nothing here adopts a policy,
closes a case, releases a hold, schedules work, deletes data or contacts a provider.
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from parkingbreaker.mail_attempt_store import (
    StorageCorruption,
    check_versions_schema,
    rollback_preserving_error,
)
from parkingbreaker.retention_policy import (
    PROPOSED_RETENTION_YEARS,
    ClosureAnchor,
    ClosureKind,
    DataScope,
    PolicyAdoption,
    PreservationHold,
    RetentionPolicy,
)

COMPONENT: Final = "retention_state"
SCHEMA_VERSION: Final = 1

_MAX_COMMAND_ID: Final = 128
_MAX_HOLD_ID: Final = 128
_MAX_CASE_TOKEN: Final = 256
_MAX_REF: Final = 256
_MAX_TIMEZONE_KEY: Final = 128

_OPAQUE_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+=~-]{0,127}")
_OPAQUE_REF: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+=@~#-]{0,255}")
_TIMEZONE_KEY: Final = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)*")


class RetentionStateRefusal(enum.StrEnum):
    """Stable fail-closed codes. Values never carry stored content or an exception message."""

    INVALID_COMMAND_ID = "invalid_command_id"
    INVALID_CASE_TOKEN = "invalid_case_token"
    INVALID_HOLD_ID = "invalid_hold_id"
    INVALID_ADOPTION = "invalid_adoption"
    INVALID_EVIDENCE_REF = "invalid_evidence_ref"
    INVALID_TIMEZONE = "invalid_timezone"
    INVALID_RETENTION_YEARS = "invalid_retention_years"
    INVALID_CLOSURE_KIND = "invalid_closure_kind"
    INVALID_SCOPE = "invalid_scope"
    INVALID_SOURCE_REF = "invalid_source_ref"
    INVALID_TIMESTAMP = "invalid_timestamp"
    NAIVE_TIMESTAMP = "naive_timestamp"
    INVALID_EXPECTED_GENERATION = "invalid_expected_generation"
    INVALID_EXPECTED_VERSION = "invalid_expected_version"
    COMMAND_ID_REUSED = "command_id_reused"
    HOLD_SCOPE_CONFLICT = "hold_scope_conflict"
    UNKNOWN_CASE = "unknown_case"
    CALLER_TRANSACTION_ACTIVE = "caller_transaction_active"
    COMPONENT_MISSING = "component_missing"
    VERSION_UNSUPPORTED = "version_unsupported"
    SCHEMA_MALFORMED = "schema_malformed"
    ROW_MALFORMED = "row_malformed"


class RetentionStateInputError(ValueError):
    """A caller-supplied value cannot be accepted; only a fixed reason is exposed."""

    def __init__(self, reason: RetentionStateRefusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


class RetentionStateCorrupted(StorageCorruption):
    """Stored schema, version or row failed validation; only a fixed reason is exposed."""

    def __init__(self, reason: RetentionStateRefusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


class RetentionTransactionRefused(RuntimeError):
    """The caller already owns a transaction, so this command refuses to commit their work."""

    def __init__(self) -> None:
        reason = RetentionStateRefusal.CALLER_TRANSACTION_ACTIVE
        super().__init__(reason.value)
        self.reason = reason


class CommandIdReused(RetentionStateInputError):
    """A command id was repeated for a different request."""

    def __init__(self) -> None:
        super().__init__(RetentionStateRefusal.COMMAND_ID_REUSED)


class UnknownCase(RetentionStateInputError):
    """The case token is not present in the existing ``pb_cases`` table."""

    def __init__(self) -> None:
        super().__init__(RetentionStateRefusal.UNKNOWN_CASE)


class HoldScopeConflict(RetentionStateInputError):
    """A hold identity already has a different scope; a new scope needs a new hold id."""

    def __init__(self) -> None:
        super().__init__(RetentionStateRefusal.HOLD_SCOPE_CONFLICT)


class CommandOutcome(enum.StrEnum):
    """The saved result of one command. ``stale`` changes nothing."""

    APPLIED = "applied"
    STALE = "stale"
    REJECTED = "rejected"


class HoldAction(enum.StrEnum):
    """The two explicit hold commands; neither has an implicit expiry."""

    HOLD = "hold"
    RELEASE = "release"


_POLICIES_DDL: Final = """CREATE TABLE pb_retention_policies (
    command_id TEXT NOT NULL UNIQUE,
    version INTEGER PRIMARY KEY CHECK (version >= 1),
    adoption TEXT NOT NULL CHECK (adoption IN ('proposed', 'adopted')),
    timezone TEXT NOT NULL,
    retention_years INTEGER NOT NULL CHECK (retention_years = 1),
    adoption_evidence_ref TEXT,
    recorded_at TEXT NOT NULL,
    CHECK ((adoption = 'adopted') = (adoption_evidence_ref IS NOT NULL))
)"""

_CLOSURES_DDL: Final = """CREATE TABLE pb_retention_case_closures (
    case_token TEXT NOT NULL REFERENCES pb_cases(case_token),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    command_id TEXT NOT NULL UNIQUE,
    closure_kind TEXT NOT NULL CHECK (closure_kind IN ('closed', 'abandoned')),
    anchor_at TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (case_token, generation)
)"""

_HOLDS_DDL: Final = """CREATE TABLE pb_retention_holds (
    case_token TEXT NOT NULL REFERENCES pb_cases(case_token),
    hold_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    scope TEXT NOT NULL CHECK (scope IN (
        'service_data', 'financial_provider_data', 'unknown_protected_data')),
    source_ref TEXT NOT NULL,
    held_at TEXT NOT NULL,
    released_at TEXT,
    PRIMARY KEY (case_token, hold_id),
    CHECK ((active = 1) = (released_at IS NULL))
)"""

_POLICY_COMMANDS_DDL: Final = """CREATE TABLE pb_retention_policy_commands (
    command_id TEXT PRIMARY KEY,
    adoption TEXT NOT NULL CHECK (adoption IN ('proposed', 'adopted')),
    timezone TEXT NOT NULL,
    retention_years INTEGER NOT NULL CHECK (retention_years = 1),
    adoption_evidence_ref TEXT,
    expected_version INTEGER CHECK (expected_version IS NULL OR expected_version >= 0),
    outcome TEXT NOT NULL CHECK (outcome IN ('applied', 'stale', 'rejected')),
    version INTEGER NOT NULL CHECK (version >= 0),
    at TEXT NOT NULL,
    CHECK ((adoption = 'adopted') = (adoption_evidence_ref IS NOT NULL)),
    CHECK (outcome != 'applied' OR version >= 1)
)"""

_CLOSURE_COMMANDS_DDL: Final = """CREATE TABLE pb_retention_closure_commands (
    command_id TEXT PRIMARY KEY,
    case_token TEXT NOT NULL,
    closure_kind TEXT NOT NULL CHECK (closure_kind IN ('closed', 'abandoned')),
    anchor_at TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    expected_generation INTEGER CHECK (expected_generation IS NULL OR expected_generation >= 0),
    outcome TEXT NOT NULL CHECK (outcome IN ('applied', 'stale', 'rejected')),
    generation INTEGER NOT NULL CHECK (generation >= 0),
    at TEXT NOT NULL,
    CHECK (outcome != 'applied' OR generation >= 1)
)"""

_HOLD_COMMANDS_DDL: Final = """CREATE TABLE pb_retention_hold_commands (
    command_id TEXT PRIMARY KEY,
    case_token TEXT NOT NULL,
    hold_id TEXT NOT NULL,
    command TEXT NOT NULL CHECK (command IN ('hold', 'release')),
    scope TEXT CHECK (scope IS NULL OR scope IN (
        'service_data', 'financial_provider_data', 'unknown_protected_data')),
    source_ref TEXT,
    expected_generation INTEGER CHECK (expected_generation IS NULL OR expected_generation >= 0),
    outcome TEXT NOT NULL CHECK (outcome IN ('applied', 'stale', 'rejected')),
    generation INTEGER NOT NULL CHECK (generation >= 0),
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    post_scope TEXT CHECK (post_scope IS NULL OR post_scope IN (
        'service_data', 'financial_provider_data', 'unknown_protected_data')),
    post_source_ref TEXT,
    post_held_at TEXT,
    post_released_at TEXT,
    at TEXT NOT NULL,
    CHECK (command = 'hold' OR (scope IS NULL AND source_ref IS NULL)),
    CHECK (command = 'release' OR (scope IS NOT NULL AND source_ref IS NOT NULL)),
    CHECK (outcome != 'applied' OR generation >= 1),
    CHECK (generation != 0 OR (active = 0 AND post_scope IS NULL AND post_source_ref IS NULL
        AND post_held_at IS NULL AND post_released_at IS NULL)),
    CHECK (generation = 0 OR (post_scope IS NOT NULL AND post_source_ref IS NOT NULL
        AND post_held_at IS NOT NULL)),
    CHECK (generation = 0 OR (active = 1) = (post_released_at IS NULL))
)"""

_TABLE_DDL: Final[dict[str, str]] = {
    "pb_retention_policies": _POLICIES_DDL,
    "pb_retention_case_closures": _CLOSURES_DDL,
    "pb_retention_holds": _HOLDS_DDL,
    "pb_retention_policy_commands": _POLICY_COMMANDS_DDL,
    "pb_retention_closure_commands": _CLOSURE_COMMANDS_DDL,
    "pb_retention_hold_commands": _HOLD_COMMANDS_DDL,
}

_TRIGGER_DDL: Final[dict[str, str]] = {
    "pb_retention_policies_no_update": (
        "CREATE TRIGGER pb_retention_policies_no_update BEFORE UPDATE ON pb_retention_policies "
        "BEGIN SELECT RAISE(ABORT, 'retention policies are immutable'); END"
    ),
    "pb_retention_policies_no_delete": (
        "CREATE TRIGGER pb_retention_policies_no_delete BEFORE DELETE ON pb_retention_policies "
        "BEGIN SELECT RAISE(ABORT, 'retention policies are immutable'); END"
    ),
    "pb_retention_case_closures_no_update": (
        "CREATE TRIGGER pb_retention_case_closures_no_update BEFORE UPDATE ON "
        "pb_retention_case_closures "
        "BEGIN SELECT RAISE(ABORT, 'retention closures are immutable'); END"
    ),
    "pb_retention_case_closures_no_delete": (
        "CREATE TRIGGER pb_retention_case_closures_no_delete BEFORE DELETE ON "
        "pb_retention_case_closures "
        "BEGIN SELECT RAISE(ABORT, 'retention closures are immutable'); END"
    ),
    "pb_retention_holds_no_delete": (
        "CREATE TRIGGER pb_retention_holds_no_delete BEFORE DELETE ON pb_retention_holds "
        "BEGIN SELECT RAISE(ABORT, 'retention holds are kept'); END"
    ),
    "pb_retention_policy_commands_no_update": (
        "CREATE TRIGGER pb_retention_policy_commands_no_update BEFORE UPDATE ON "
        "pb_retention_policy_commands "
        "BEGIN SELECT RAISE(ABORT, 'retention policy commands are append-only'); END"
    ),
    "pb_retention_policy_commands_no_delete": (
        "CREATE TRIGGER pb_retention_policy_commands_no_delete BEFORE DELETE ON "
        "pb_retention_policy_commands "
        "BEGIN SELECT RAISE(ABORT, 'retention policy commands are append-only'); END"
    ),
    "pb_retention_closure_commands_no_update": (
        "CREATE TRIGGER pb_retention_closure_commands_no_update BEFORE UPDATE ON "
        "pb_retention_closure_commands "
        "BEGIN SELECT RAISE(ABORT, 'retention closure commands are append-only'); END"
    ),
    "pb_retention_closure_commands_no_delete": (
        "CREATE TRIGGER pb_retention_closure_commands_no_delete BEFORE DELETE ON "
        "pb_retention_closure_commands "
        "BEGIN SELECT RAISE(ABORT, 'retention closure commands are append-only'); END"
    ),
    "pb_retention_hold_commands_no_update": (
        "CREATE TRIGGER pb_retention_hold_commands_no_update BEFORE UPDATE ON "
        "pb_retention_hold_commands "
        "BEGIN SELECT RAISE(ABORT, 'retention hold commands are append-only'); END"
    ),
    "pb_retention_hold_commands_no_delete": (
        "CREATE TRIGGER pb_retention_hold_commands_no_delete BEFORE DELETE ON "
        "pb_retention_hold_commands "
        "BEGIN SELECT RAISE(ABORT, 'retention hold commands are append-only'); END"
    ),
}


@dataclasses.dataclass(frozen=True)
class RetentionPolicyRevision:
    """One immutable policy version, ready for a pure evaluator."""

    version: int
    adoption: PolicyAdoption
    timezone: ZoneInfo
    retention_years: int
    adoption_evidence_ref: str | None
    recorded_at: datetime

    def to_policy(self) -> RetentionPolicy:
        return RetentionPolicy(
            adoption=self.adoption,
            timezone=self.timezone,
            retention_years=self.retention_years,
        )


@dataclasses.dataclass(frozen=True)
class ClosureRevision:
    """One explicit closure or abandonment anchor at a known generation."""

    case_token: str
    generation: int
    kind: ClosureKind
    anchor_at: datetime
    source_ref: str
    recorded_at: datetime

    def to_anchor(self) -> ClosureAnchor:
        return ClosureAnchor(kind=self.kind, timestamp=self.anchor_at)


@dataclasses.dataclass(frozen=True)
class HoldRevision:
    """One identified, scope-specific hold. It is active until an explicit release."""

    case_token: str
    hold_id: str
    generation: int
    scope: DataScope
    active: bool
    source_ref: str
    held_at: datetime
    released_at: datetime | None

    def to_hold(self) -> PreservationHold:
        return PreservationHold(scope=self.scope, released=not self.active)


@dataclasses.dataclass(frozen=True)
class PolicyCommandResult:
    """The saved outcome of one policy command; an exact replay returns the same values."""

    outcome: CommandOutcome
    version: int
    revision: RetentionPolicyRevision | None


@dataclasses.dataclass(frozen=True)
class ClosureCommandResult:
    """The saved outcome of one closure command."""

    outcome: CommandOutcome
    generation: int
    revision: ClosureRevision | None


@dataclasses.dataclass(frozen=True)
class HoldCommandResult:
    """The saved outcome of one hold or release command."""

    outcome: CommandOutcome
    generation: int
    revision: HoldRevision | None


@dataclasses.dataclass(frozen=True)
class RetentionEvaluationInputs:
    """Stored facts for one case. Runtime blockers are deliberately not included."""

    case_token: str
    policy: RetentionPolicy | None
    anchors: tuple[ClosureAnchor, ...]
    holds: tuple[PreservationHold, ...]


@dataclasses.dataclass(frozen=True)
class CaseRetentionSnapshot:
    """A consistent read of one existing case's stored retention facts."""

    case_token: str
    policy: RetentionPolicyRevision | None
    closure: ClosureRevision | None
    holds: tuple[HoldRevision, ...]

    def to_evaluation_inputs(self) -> RetentionEvaluationInputs:
        policy = None if self.policy is None else self.policy.to_policy()
        anchors = () if self.closure is None else (self.closure.to_anchor(),)
        return RetentionEvaluationInputs(
            case_token=self.case_token,
            policy=policy,
            anchors=anchors,
            holds=tuple(hold.to_hold() for hold in self.holds),
        )


@dataclasses.dataclass(frozen=True)
class _PolicyCommand:
    command_id: str
    adoption: PolicyAdoption
    timezone: ZoneInfo
    retention_years: int
    adoption_evidence_ref: str | None
    expected_version: int | None
    outcome: CommandOutcome
    version: int
    at: datetime


@dataclasses.dataclass(frozen=True)
class _ClosureCommand:
    command_id: str
    case_token: str
    kind: ClosureKind
    anchor_at: datetime
    source_ref: str
    expected_generation: int | None
    outcome: CommandOutcome
    generation: int
    at: datetime


@dataclasses.dataclass(frozen=True)
class _HoldCommand:
    command_id: str
    case_token: str
    hold_id: str
    action: HoldAction
    request_scope: DataScope | None
    request_source_ref: str | None
    expected_generation: int | None
    outcome: CommandOutcome
    generation: int
    active: bool
    post_scope: DataScope | None
    post_source_ref: str | None
    post_held_at: datetime | None
    post_released_at: datetime | None
    at: datetime


_POLICY_COLUMNS: Final = (
    "command_id, version, adoption, timezone, retention_years, adoption_evidence_ref, recorded_at"
)
_POLICY_COMMAND_COLUMNS: Final = (
    "command_id, adoption, timezone, retention_years, adoption_evidence_ref, "
    "expected_version, outcome, version, at"
)
_CLOSURE_COLUMNS: Final = (
    "command_id, case_token, generation, closure_kind, anchor_at, source_ref, recorded_at"
)
_CLOSURE_COMMAND_COLUMNS: Final = (
    "command_id, case_token, closure_kind, anchor_at, source_ref, expected_generation, "
    "outcome, generation, at"
)
_HOLD_COLUMNS: Final = (
    "case_token, hold_id, generation, active, scope, source_ref, held_at, released_at"
)
_HOLD_COMMAND_COLUMNS: Final = (
    "command_id, case_token, hold_id, command, scope, source_ref, expected_generation, "
    "outcome, generation, active, post_scope, post_source_ref, post_held_at, "
    "post_released_at, at"
)


def _input(reason: RetentionStateRefusal) -> RetentionStateInputError:
    return RetentionStateInputError(reason)


def _corrupted(reason: RetentionStateRefusal) -> RetentionStateCorrupted:
    return RetentionStateCorrupted(reason)


def _check_opaque(
    value: object,
    pattern: re.Pattern[str],
    maximum: int,
    reason: RetentionStateRefusal,
) -> str:
    if type(value) is not str or len(value) > maximum or pattern.fullmatch(value) is None:
        raise _input(reason)
    return value


def _check_command_id(value: object) -> str:
    return _check_opaque(
        value, _OPAQUE_ID, _MAX_COMMAND_ID, RetentionStateRefusal.INVALID_COMMAND_ID
    )


def _check_hold_id(value: object) -> str:
    return _check_opaque(value, _OPAQUE_ID, _MAX_HOLD_ID, RetentionStateRefusal.INVALID_HOLD_ID)


def _check_source_ref(value: object) -> str:
    return _check_opaque(value, _OPAQUE_REF, _MAX_REF, RetentionStateRefusal.INVALID_SOURCE_REF)


def _check_evidence_ref(value: object) -> str:
    return _check_opaque(value, _OPAQUE_REF, _MAX_REF, RetentionStateRefusal.INVALID_EVIDENCE_REF)


def _check_case_token(value: object) -> str:
    if type(value) is not str or not value or len(value) > _MAX_CASE_TOKEN:
        raise _input(RetentionStateRefusal.INVALID_CASE_TOKEN)
    if value != value.strip():
        raise _input(RetentionStateRefusal.INVALID_CASE_TOKEN)
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise _input(RetentionStateRefusal.INVALID_CASE_TOKEN)
    return value


def _check_timestamp(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise _input(RetentionStateRefusal.INVALID_TIMESTAMP)
    if value.tzinfo is None:
        raise _input(RetentionStateRefusal.NAIVE_TIMESTAMP)
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError):
        raise _input(RetentionStateRefusal.INVALID_TIMESTAMP) from None
    if offset is None:
        raise _input(RetentionStateRefusal.NAIVE_TIMESTAMP)
    try:
        return value.astimezone(UTC)
    except (OSError, OverflowError, ValueError):
        raise _input(RetentionStateRefusal.INVALID_TIMESTAMP) from None


def _check_optional_generation(value: object, *, minimum: int) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < minimum:
        raise _input(RetentionStateRefusal.INVALID_EXPECTED_GENERATION)
    return value


def _check_required_generation(value: object, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise _input(RetentionStateRefusal.INVALID_EXPECTED_GENERATION)
    return value


def _check_optional_version(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise _input(RetentionStateRefusal.INVALID_EXPECTED_VERSION)
    return value


def _check_retention_years(value: object) -> int:
    if type(value) is not int or value != PROPOSED_RETENTION_YEARS:
        raise _input(RetentionStateRefusal.INVALID_RETENTION_YEARS)
    return value


def _check_timezone(value: object) -> ZoneInfo:
    if not isinstance(value, ZoneInfo):
        raise _input(RetentionStateRefusal.INVALID_TIMEZONE)
    key = value.key
    if type(key) is not str or len(key) > _MAX_TIMEZONE_KEY or _TIMEZONE_KEY.fullmatch(key) is None:
        raise _input(RetentionStateRefusal.INVALID_TIMEZONE)
    try:
        return ZoneInfo(key)
    except (KeyError, OSError, ValueError):
        raise _input(RetentionStateRefusal.INVALID_TIMEZONE) from None


def _check_adoption(value: object) -> PolicyAdoption:
    if not isinstance(value, PolicyAdoption):
        raise _input(RetentionStateRefusal.INVALID_ADOPTION)
    return value


def _check_closure_kind(value: object) -> ClosureKind:
    if not isinstance(value, ClosureKind):
        raise _input(RetentionStateRefusal.INVALID_CLOSURE_KIND)
    return value


def _check_scope(value: object) -> DataScope:
    if not isinstance(value, DataScope):
        raise _input(RetentionStateRefusal.INVALID_SCOPE)
    return value


def _check_evidence(adoption: PolicyAdoption, evidence: object) -> str | None:
    if adoption is PolicyAdoption.ADOPTED:
        if evidence is None:
            raise _input(RetentionStateRefusal.INVALID_EVIDENCE_REF)
        return _check_evidence_ref(evidence)
    if evidence is not None:
        raise _input(RetentionStateRefusal.INVALID_EVIDENCE_REF)
    return None


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: object) -> datetime:
    if type(value) is not str:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED) from None
    if parsed.tzinfo is None:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    try:
        offset = parsed.utcoffset()
    except (OverflowError, ValueError):
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED) from None
    if offset != timedelta(0):
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    try:
        return parsed.astimezone(UTC)
    except (OSError, OverflowError, ValueError):
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED) from None


def _parse_optional_timestamp(value: object) -> datetime | None:
    return None if value is None else _parse_timestamp(value)


def _parse_int(value: object, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    return value


def _parse_id(value: object) -> str:
    if type(value) is not str or _OPAQUE_ID.fullmatch(value) is None:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    return value


def _parse_case_token(value: object) -> str:
    if type(value) is not str or not value or len(value) > _MAX_CASE_TOKEN:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    if value != value.strip() or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    return value


def _parse_ref(value: object) -> str:
    if type(value) is not str or _OPAQUE_REF.fullmatch(value) is None:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    return value


def _parse_timezone(value: object) -> ZoneInfo:
    if (
        type(value) is not str
        or len(value) > _MAX_TIMEZONE_KEY
        or _TIMEZONE_KEY.fullmatch(value) is None
    ):
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    try:
        return ZoneInfo(value)
    except (KeyError, OSError, ValueError):
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED) from None


def _parse_adoption(value: object) -> PolicyAdoption:
    if type(value) is not str:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    try:
        return PolicyAdoption(value)
    except ValueError:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED) from None


def _parse_closure_kind(value: object) -> ClosureKind:
    if type(value) is not str:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    try:
        return ClosureKind(value)
    except ValueError:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED) from None


def _parse_scope(value: object) -> DataScope:
    if type(value) is not str:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    try:
        return DataScope(value)
    except ValueError:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED) from None


def _parse_action(value: object) -> HoldAction:
    if type(value) is not str:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    try:
        return HoldAction(value)
    except ValueError:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED) from None


def _parse_outcome(value: object) -> CommandOutcome:
    if type(value) is not str:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    try:
        return CommandOutcome(value)
    except ValueError:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED) from None


def _parse_bool(value: object) -> bool:
    if type(value) is not int or value not in (0, 1):
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    return value == 1


def _parse_evidence(adoption: PolicyAdoption, value: object) -> str | None:
    if adoption is PolicyAdoption.ADOPTED:
        return _parse_ref(value)
    if value is not None:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    return None


def _normalise_sql(sql: object) -> str:
    if type(sql) is not str:
        raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED)
    return " ".join(sql.upper().split())


def _retention_objects(database: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    try:
        rows = database.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name GLOB 'pb_retention_*'"
        ).fetchall()
    except sqlite3.DatabaseError:
        raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED) from None
    objects: dict[str, tuple[str, str]] = {}
    for kind, name, sql in rows:
        if type(kind) is not str or type(name) is not str or type(sql) is not str:
            raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED)
        objects[name] = (kind, sql)
    return objects


def _attached_retention_triggers(database: sqlite3.Connection) -> tuple[str, ...]:
    """Every trigger attached to a retention table, whatever its own name is."""

    try:
        rows = database.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name GLOB 'pb_retention_*'"
        ).fetchall()
    except sqlite3.DatabaseError:
        raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED) from None
    names: list[str] = []
    for row in rows:
        if type(row[0]) is not str:
            raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED)
        names.append(row[0])
    return tuple(names)


def _validate_schema_objects(
    database: sqlite3.Connection, *, allow_missing_triggers: bool
) -> tuple[str, ...]:
    objects = _retention_objects(database)
    for name, ddl in _TABLE_DDL.items():
        entry = objects.get(name)
        if entry is None or entry[0] != "table" or _normalise_sql(entry[1]) != _normalise_sql(ddl):
            raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED)
    missing: list[str] = []
    for name, ddl in _TRIGGER_DDL.items():
        entry = objects.get(name)
        if entry is None:
            if not allow_missing_triggers:
                raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED)
            missing.append(name)
            continue
        if entry[0] != "trigger" or _normalise_sql(entry[1]) != _normalise_sql(ddl):
            raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED)
    if set(objects) - (set(_TABLE_DDL) | set(_TRIGGER_DDL)):
        raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED)
    if set(_attached_retention_triggers(database)) - set(_TRIGGER_DDL):
        raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED)
    return tuple(missing)


def _check_versions_registry(database: sqlite3.Connection) -> None:
    try:
        entry = database.execute(
            "SELECT type FROM sqlite_master WHERE name='pb_store_versions'"
        ).fetchone()
    except sqlite3.DatabaseError:
        raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED) from None
    if entry is None or entry[0] != "table":
        raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED)
    try:
        check_versions_schema(database)
    except StorageCorruption:
        raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED) from None
    except sqlite3.DatabaseError:
        raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED) from None


def _check_cases_table(database: sqlite3.Connection) -> None:
    try:
        entry = database.execute("SELECT type FROM sqlite_master WHERE name='pb_cases'").fetchone()
        info = database.execute("PRAGMA table_info(pb_cases)").fetchall()
    except sqlite3.DatabaseError:
        raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED) from None
    if entry is None or entry[0] != "table":
        raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED)
    columns = {row[1]: row[2] for row in info}
    if "case_token" not in columns:
        raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED)
    if type(columns["case_token"]) is not str or columns["case_token"].upper() != "TEXT":
        raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED)


def _version_row(database: sqlite3.Connection) -> tuple[object, ...] | None:
    try:
        row = database.execute(
            "SELECT version FROM pb_store_versions WHERE component=?", (COMPONENT,)
        ).fetchone()
    except sqlite3.DatabaseError:
        raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED) from None
    return None if row is None else tuple(row)


def _require_ready(database: sqlite3.Connection) -> None:
    _check_versions_registry(database)
    _check_cases_table(database)
    row = _version_row(database)
    if row is None:
        raise _corrupted(RetentionStateRefusal.COMPONENT_MISSING)
    version = row[0]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise _corrupted(RetentionStateRefusal.VERSION_UNSUPPORTED)
    _validate_schema_objects(database, allow_missing_triggers=False)


@contextlib.contextmanager
def _write_transaction(database: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    if database.in_transaction:
        raise RetentionTransactionRefused()
    database.execute("BEGIN IMMEDIATE")
    try:
        yield database
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


@contextlib.contextmanager
def _read_transaction(database: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    if database.in_transaction:
        yield database
        return
    database.execute("BEGIN")
    try:
        yield database
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def _case_exists(database: sqlite3.Connection, case_token: str) -> bool:
    try:
        return (
            database.execute("SELECT 1 FROM pb_cases WHERE case_token=?", (case_token,)).fetchone()
            is not None
        )
    except sqlite3.DatabaseError:
        raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED) from None


def _policy_revision_from_row(row: tuple[object, ...]) -> RetentionPolicyRevision:
    command_id, version, adoption, timezone, years, evidence, recorded_at = row
    parsed_adoption = _parse_adoption(adoption)
    parsed_years = _parse_int(years, 1)
    if parsed_years != PROPOSED_RETENTION_YEARS:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    _parse_id(command_id)
    return RetentionPolicyRevision(
        version=_parse_int(version, 1),
        adoption=parsed_adoption,
        timezone=_parse_timezone(timezone),
        retention_years=parsed_years,
        adoption_evidence_ref=_parse_evidence(parsed_adoption, evidence),
        recorded_at=_parse_timestamp(recorded_at),
    )


def _closure_revision_from_row(row: tuple[object, ...]) -> ClosureRevision:
    command_id, case_token, generation, kind, anchor_at, source_ref, recorded_at = row
    _parse_id(command_id)
    return ClosureRevision(
        case_token=_parse_case_token(case_token),
        generation=_parse_int(generation, 1),
        kind=_parse_closure_kind(kind),
        anchor_at=_parse_timestamp(anchor_at),
        source_ref=_parse_ref(source_ref),
        recorded_at=_parse_timestamp(recorded_at),
    )


def _hold_revision_from_row(row: tuple[object, ...]) -> HoldRevision:
    case_token, hold_id, generation, active, scope, source_ref, held_at, released_at = row
    parsed_active = _parse_bool(active)
    parsed_released_at = _parse_optional_timestamp(released_at)
    if parsed_active != (parsed_released_at is None):
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    return HoldRevision(
        case_token=_parse_case_token(case_token),
        hold_id=_parse_id(hold_id),
        generation=_parse_int(generation, 1),
        scope=_parse_scope(scope),
        active=parsed_active,
        source_ref=_parse_ref(source_ref),
        held_at=_parse_timestamp(held_at),
        released_at=parsed_released_at,
    )


def _policy_command_from_row(row: tuple[object, ...]) -> _PolicyCommand:
    (
        command_id,
        adoption,
        timezone,
        years,
        evidence,
        expected_version,
        outcome,
        version,
        at,
    ) = row
    parsed_adoption = _parse_adoption(adoption)
    parsed_years = _parse_int(years, 1)
    if parsed_years != PROPOSED_RETENTION_YEARS:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    parsed_outcome = _parse_outcome(outcome)
    parsed_version = _parse_int(version, 0)
    parsed_expected = None if expected_version is None else _parse_int(expected_version, 0)
    if parsed_outcome is CommandOutcome.APPLIED and parsed_version < 1:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    if parsed_outcome is CommandOutcome.APPLIED:
        # An omitted expected version may only have created the very first policy.
        if parsed_expected is None:
            if parsed_version != 1:
                raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
        elif parsed_expected != parsed_version - 1:
            raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    elif parsed_outcome is CommandOutcome.STALE:
        # A stale command never appended a policy: an omitted expected version is only
        # stale against an already recorded policy, and a matching one would have applied.
        if parsed_expected is None:
            if parsed_version < 1:
                raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
        elif parsed_expected == parsed_version:
            raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    return _PolicyCommand(
        command_id=_parse_id(command_id),
        adoption=parsed_adoption,
        timezone=_parse_timezone(timezone),
        retention_years=parsed_years,
        adoption_evidence_ref=_parse_evidence(parsed_adoption, evidence),
        expected_version=parsed_expected,
        outcome=parsed_outcome,
        version=parsed_version,
        at=_parse_timestamp(at),
    )


def _closure_command_from_row(row: tuple[object, ...]) -> _ClosureCommand:
    (
        command_id,
        case_token,
        kind,
        anchor_at,
        source_ref,
        expected_generation,
        outcome,
        generation,
        at,
    ) = row
    parsed_outcome = _parse_outcome(outcome)
    parsed_generation = _parse_int(generation, 0)
    if parsed_outcome is CommandOutcome.APPLIED and parsed_generation < 1:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    return _ClosureCommand(
        command_id=_parse_id(command_id),
        case_token=_parse_case_token(case_token),
        kind=_parse_closure_kind(kind),
        anchor_at=_parse_timestamp(anchor_at),
        source_ref=_parse_ref(source_ref),
        expected_generation=(
            None if expected_generation is None else _parse_int(expected_generation, 0)
        ),
        outcome=parsed_outcome,
        generation=parsed_generation,
        at=_parse_timestamp(at),
    )


def _hold_command_from_row(row: tuple[object, ...]) -> _HoldCommand:
    (
        command_id,
        case_token,
        hold_id,
        action,
        scope,
        source_ref,
        expected_generation,
        outcome,
        generation,
        active,
        post_scope,
        post_source_ref,
        post_held_at,
        post_released_at,
        at,
    ) = row
    parsed_action = _parse_action(action)
    parsed_scope = _parse_scope(scope) if scope is not None else None
    parsed_source_ref = _parse_ref(source_ref) if source_ref is not None else None
    if parsed_action is HoldAction.HOLD:
        if parsed_scope is None or parsed_source_ref is None:
            raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    elif parsed_scope is not None or parsed_source_ref is not None:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    parsed_outcome = _parse_outcome(outcome)
    parsed_expected = None if expected_generation is None else _parse_int(expected_generation, 0)
    parsed_generation = _parse_int(generation, 0)
    parsed_active = _parse_bool(active)
    parsed_post_scope = None if post_scope is None else _parse_scope(post_scope)
    parsed_post_source = None if post_source_ref is None else _parse_ref(post_source_ref)
    parsed_post_held_at = _parse_optional_timestamp(post_held_at)
    parsed_post_released_at = _parse_optional_timestamp(post_released_at)
    if parsed_outcome is CommandOutcome.APPLIED and parsed_generation < 1:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    if parsed_outcome is CommandOutcome.APPLIED:
        # A generated identity is only ever created at generation 1; every later applied
        # assertion or release observed the immediately preceding generation exactly, and an
        # omitted expected generation is valid only for that first creation.
        if parsed_generation == 1:
            if parsed_action is HoldAction.RELEASE or parsed_expected not in (None, 0):
                raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
        elif parsed_expected != parsed_generation - 1:
            raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    elif parsed_outcome is CommandOutcome.STALE and parsed_action is HoldAction.HOLD:
        # A stale assertion either omitted its generation against an existing identity (which
        # can never implicitly create or replace one) or named a different generation.
        if parsed_expected is None:
            if parsed_generation < 1:
                raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
        elif parsed_expected == parsed_generation:
            raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    if parsed_generation == 0:
        if (
            parsed_active
            or parsed_post_scope is not None
            or parsed_post_source is not None
            or parsed_post_held_at is not None
            or parsed_post_released_at is not None
        ):
            raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    elif (
        parsed_post_scope is None
        or parsed_post_source is None
        or parsed_post_held_at is None
        or parsed_active != (parsed_post_released_at is None)
    ):
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    return _HoldCommand(
        command_id=_parse_id(command_id),
        case_token=_parse_case_token(case_token),
        hold_id=_parse_id(hold_id),
        action=parsed_action,
        request_scope=parsed_scope,
        request_source_ref=parsed_source_ref,
        expected_generation=parsed_expected,
        outcome=parsed_outcome,
        generation=parsed_generation,
        active=parsed_active,
        post_scope=parsed_post_scope,
        post_source_ref=parsed_post_source,
        post_held_at=parsed_post_held_at,
        post_released_at=parsed_post_released_at,
        at=_parse_timestamp(at),
    )


def _policy_revisions_in_db(
    database: sqlite3.Connection,
) -> tuple[RetentionPolicyRevision, ...]:
    rows = database.execute(
        f"SELECT {_POLICY_COLUMNS} FROM pb_retention_policies ORDER BY version"
    ).fetchall()
    revisions: list[RetentionPolicyRevision] = []
    expected = 1
    for row in rows:
        revision = _policy_revision_from_row(row)
        if revision.version != expected:
            raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
        expected += 1
        revisions.append(revision)
    return tuple(revisions)


def _policy_snapshot_in_db(database: sqlite3.Connection) -> RetentionPolicyRevision | None:
    revisions = _policy_revisions_in_db(database)
    return revisions[-1] if revisions else None


def _closure_revisions_in_db(
    database: sqlite3.Connection, case_token: str
) -> tuple[ClosureRevision, ...]:
    rows = database.execute(
        f"SELECT {_CLOSURE_COLUMNS} FROM pb_retention_case_closures "
        "WHERE case_token=? ORDER BY generation",
        (case_token,),
    ).fetchall()
    revisions: list[ClosureRevision] = []
    expected = 1
    for row in rows:
        revision = _closure_revision_from_row(row)
        if revision.case_token != case_token or revision.generation != expected:
            raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
        expected += 1
        revisions.append(revision)
    return tuple(revisions)


def _closure_snapshot_in_db(
    database: sqlite3.Connection, case_token: str
) -> ClosureRevision | None:
    revisions = _closure_revisions_in_db(database, case_token)
    return revisions[-1] if revisions else None


def _holds_in_db(database: sqlite3.Connection, case_token: str) -> tuple[HoldRevision, ...]:
    rows = database.execute(
        f"SELECT {_HOLD_COLUMNS} FROM pb_retention_holds WHERE case_token=? ORDER BY hold_id",
        (case_token,),
    ).fetchall()
    holds: list[HoldRevision] = []
    for row in rows:
        hold = _hold_revision_from_row(row)
        if hold.case_token != case_token:
            raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
        holds.append(hold)
    return tuple(holds)


def _hold_row_in_db(
    database: sqlite3.Connection, case_token: str, hold_id: str
) -> HoldRevision | None:
    row = database.execute(
        f"SELECT {_HOLD_COLUMNS} FROM pb_retention_holds WHERE case_token=? AND hold_id=?",
        (case_token, hold_id),
    ).fetchone()
    if row is None:
        return None
    hold = _hold_revision_from_row(row)
    if hold.case_token != case_token or hold.hold_id != hold_id:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    return hold


def _case_snapshot_in_db(database: sqlite3.Connection, case_token: str) -> CaseRetentionSnapshot:
    if not _case_exists(database, case_token):
        raise UnknownCase()
    return CaseRetentionSnapshot(
        case_token=case_token,
        policy=_policy_snapshot_in_db(database),
        closure=_closure_snapshot_in_db(database, case_token),
        holds=_holds_in_db(database, case_token),
    )


def _policy_command_by_id(database: sqlite3.Connection, command_id: str) -> _PolicyCommand | None:
    row = database.execute(
        f"SELECT {_POLICY_COMMAND_COLUMNS} FROM pb_retention_policy_commands WHERE command_id=?",
        (command_id,),
    ).fetchone()
    return None if row is None else _policy_command_from_row(row)


def _closure_command_by_id(database: sqlite3.Connection, command_id: str) -> _ClosureCommand | None:
    row = database.execute(
        f"SELECT {_CLOSURE_COMMAND_COLUMNS} FROM pb_retention_closure_commands WHERE command_id=?",
        (command_id,),
    ).fetchone()
    return None if row is None else _closure_command_from_row(row)


def _hold_command_by_id(database: sqlite3.Connection, command_id: str) -> _HoldCommand | None:
    row = database.execute(
        f"SELECT {_HOLD_COMMAND_COLUMNS} FROM pb_retention_hold_commands WHERE command_id=?",
        (command_id,),
    ).fetchone()
    return None if row is None else _hold_command_from_row(row)


def _policy_result(command: _PolicyCommand) -> PolicyCommandResult:
    revision = None
    if command.outcome is CommandOutcome.APPLIED:
        revision = RetentionPolicyRevision(
            version=command.version,
            adoption=command.adoption,
            timezone=command.timezone,
            retention_years=command.retention_years,
            adoption_evidence_ref=command.adoption_evidence_ref,
            recorded_at=command.at,
        )
    return PolicyCommandResult(
        outcome=command.outcome,
        version=command.version,
        revision=revision,
    )


def _closure_result(command: _ClosureCommand) -> ClosureCommandResult:
    revision = None
    if command.outcome is CommandOutcome.APPLIED:
        revision = ClosureRevision(
            case_token=command.case_token,
            generation=command.generation,
            kind=command.kind,
            anchor_at=command.anchor_at,
            source_ref=command.source_ref,
            recorded_at=command.at,
        )
    return ClosureCommandResult(
        outcome=command.outcome,
        generation=command.generation,
        revision=revision,
    )


def _hold_result(command: _HoldCommand) -> HoldCommandResult:
    revision = None
    if command.outcome is CommandOutcome.APPLIED:
        if (
            command.post_scope is None
            or command.post_source_ref is None
            or command.post_held_at is None
        ):
            raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
        revision = HoldRevision(
            case_token=command.case_token,
            hold_id=command.hold_id,
            generation=command.generation,
            scope=command.post_scope,
            active=command.active,
            source_ref=command.post_source_ref,
            held_at=command.post_held_at,
            released_at=command.post_released_at,
        )
    return HoldCommandResult(
        outcome=command.outcome,
        generation=command.generation,
        revision=revision,
    )


def _verify_applied_policy(database: sqlite3.Connection, command: _PolicyCommand) -> None:
    if command.outcome is not CommandOutcome.APPLIED:
        return
    row = database.execute(
        f"SELECT {_POLICY_COLUMNS} FROM pb_retention_policies WHERE command_id=?",
        (command.command_id,),
    ).fetchone()
    if row is None:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    stored = _policy_revision_from_row(tuple(row))
    if (
        stored.version != command.version
        or stored.adoption is not command.adoption
        or stored.timezone.key != command.timezone.key
        or stored.retention_years != command.retention_years
        or stored.adoption_evidence_ref != command.adoption_evidence_ref
        or stored.recorded_at != command.at
    ):
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)


def _verify_applied_closure(database: sqlite3.Connection, command: _ClosureCommand) -> None:
    if command.outcome is not CommandOutcome.APPLIED:
        return
    row = database.execute(
        f"SELECT {_CLOSURE_COLUMNS} FROM pb_retention_case_closures WHERE command_id=?",
        (command.command_id,),
    ).fetchone()
    if row is None:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    stored = _closure_revision_from_row(tuple(row))
    if (
        stored.case_token != command.case_token
        or stored.generation != command.generation
        or stored.kind is not command.kind
        or stored.anchor_at != command.anchor_at
        or stored.source_ref != command.source_ref
        or stored.recorded_at != command.at
    ):
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)


def _verify_applied_hold(database: sqlite3.Connection, command: _HoldCommand) -> None:
    if command.outcome is not CommandOutcome.APPLIED:
        return
    stored = _hold_row_in_db(database, command.case_token, command.hold_id)
    if stored is None or stored.generation < command.generation:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    # One hold identity keeps the scope it was first asserted with, at every generation.
    if stored.scope is not command.post_scope:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)


def _command_result(database: sqlite3.Connection, command_id: str) -> PolicyCommandResult:
    command = _policy_command_by_id(database, command_id)
    if command is None:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    _verify_applied_policy(database, command)
    return _policy_result(command)


def _closure_command_result(database: sqlite3.Connection, command_id: str) -> ClosureCommandResult:
    command = _closure_command_by_id(database, command_id)
    if command is None:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    _verify_applied_closure(database, command)
    return _closure_result(command)


def _hold_command_result(database: sqlite3.Connection, command_id: str) -> HoldCommandResult:
    command = _hold_command_by_id(database, command_id)
    if command is None:
        raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
    _verify_applied_hold(database, command)
    return _hold_result(command)


def _case_tokens(database: sqlite3.Connection) -> set[str]:
    try:
        rows = database.execute("SELECT case_token FROM pb_cases").fetchall()
    except sqlite3.DatabaseError:
        raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED) from None
    tokens: set[str] = set()
    for row in rows:
        if type(row[0]) is not str:
            raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
        tokens.add(row[0])
    return tokens


def _validate_all_rows(database: sqlite3.Connection) -> None:
    cases = _case_tokens(database)

    policies = database.execute(
        f"SELECT {_POLICY_COLUMNS} FROM pb_retention_policies ORDER BY version"
    ).fetchall()
    expected_version = 1
    for row in policies:
        policy_revision = _policy_revision_from_row(tuple(row))
        if policy_revision.version != expected_version:
            raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
        expected_version += 1

    for row in database.execute(
        f"SELECT {_POLICY_COMMAND_COLUMNS} FROM pb_retention_policy_commands"
    ).fetchall():
        _policy_command_from_row(tuple(row))

    closure_generations: dict[str, int] = {}
    for row in database.execute(
        f"SELECT {_CLOSURE_COLUMNS} FROM pb_retention_case_closures ORDER BY case_token, generation"
    ).fetchall():
        closure_revision = _closure_revision_from_row(tuple(row))
        if closure_revision.case_token not in cases:
            raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
        expected = closure_generations.get(closure_revision.case_token, 0) + 1
        if closure_revision.generation != expected:
            raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
        closure_generations[closure_revision.case_token] = closure_revision.generation

    for row in database.execute(
        f"SELECT {_CLOSURE_COMMAND_COLUMNS} FROM pb_retention_closure_commands"
    ).fetchall():
        closure_command = _closure_command_from_row(tuple(row))
        if closure_command.case_token not in cases:
            raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)

    hold_scopes: dict[tuple[str, str], DataScope] = {}
    for row in database.execute(
        f"SELECT {_HOLD_COLUMNS} FROM pb_retention_holds ORDER BY case_token, hold_id"
    ).fetchall():
        hold = _hold_revision_from_row(tuple(row))
        if hold.case_token not in cases:
            raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
        hold_scopes[(hold.case_token, hold.hold_id)] = hold.scope

    for row in database.execute(
        f"SELECT {_HOLD_COMMAND_COLUMNS} FROM pb_retention_hold_commands"
    ).fetchall():
        hold_command = _hold_command_from_row(tuple(row))
        if hold_command.case_token not in cases:
            raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
        if hold_command.outcome is CommandOutcome.APPLIED:
            stored_scope = hold_scopes.get((hold_command.case_token, hold_command.hold_id))
            if stored_scope is None or stored_scope is not hold_command.post_scope:
                raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)


def initialize_retention_state(database: sqlite3.Connection) -> None:
    """Create or validate the versioned retention tables without adopting or closing anything.

    The connection must already own the shared ``pb_store_versions`` registry and a
    ``pb_cases`` table with a ``case_token`` column. Only ``pb_retention_*`` objects and this
    component's version row are created. Repeating the call is idempotent; existing rows are
    validated but never changed.
    """

    with _write_transaction(database):
        _check_versions_registry(database)
        _check_cases_table(database)
        objects = _retention_objects(database)
        row = _version_row(database)
        if row is None:
            if objects:
                raise _corrupted(RetentionStateRefusal.SCHEMA_MALFORMED)
            for ddl in _TABLE_DDL.values():
                database.execute(ddl)
            for ddl in _TRIGGER_DDL.values():
                database.execute(ddl)
            database.execute(
                "INSERT INTO pb_store_versions(component, version) VALUES (?, ?)",
                (COMPONENT, SCHEMA_VERSION),
            )
        else:
            version = row[0]
            if type(version) is not int or version != SCHEMA_VERSION:
                raise _corrupted(RetentionStateRefusal.VERSION_UNSUPPORTED)
            missing = _validate_schema_objects(database, allow_missing_triggers=True)
            for name in missing:
                database.execute(_TRIGGER_DDL[name])
            _validate_schema_objects(database, allow_missing_triggers=False)
            _validate_all_rows(database)


def policy_revisions(database: sqlite3.Connection) -> tuple[RetentionPolicyRevision, ...]:
    """Return every immutable policy version, oldest first."""

    with _read_transaction(database) as connection:
        _require_ready(connection)
        return _policy_revisions_in_db(connection)


def policy_snapshot(database: sqlite3.Connection) -> RetentionPolicyRevision | None:
    """Return the current (highest) policy version, or None when none was ever recorded."""

    with _read_transaction(database) as connection:
        _require_ready(connection)
        return _policy_snapshot_in_db(connection)


def closure_revisions(database: sqlite3.Connection, case_token: str) -> tuple[ClosureRevision, ...]:
    """Return one existing case's append-only closure anchors, oldest generation first."""

    case = _check_case_token(case_token)
    with _read_transaction(database) as connection:
        _require_ready(connection)
        if not _case_exists(connection, case):
            raise UnknownCase()
        return _closure_revisions_in_db(connection, case)


def closure_snapshot(database: sqlite3.Connection, case_token: str) -> ClosureRevision | None:
    """Return the current closure anchor for an existing case, or None when unclosed."""

    case = _check_case_token(case_token)
    with _read_transaction(database) as connection:
        _require_ready(connection)
        if not _case_exists(connection, case):
            raise UnknownCase()
        return _closure_snapshot_in_db(connection, case)


def holds_snapshot(database: sqlite3.Connection, case_token: str) -> tuple[HoldRevision, ...]:
    """Return every stored hold for an existing case. Active holds never expire on read."""

    case = _check_case_token(case_token)
    with _read_transaction(database) as connection:
        _require_ready(connection)
        if not _case_exists(connection, case):
            raise UnknownCase()
        return _holds_in_db(connection, case)


def hold_snapshot(
    database: sqlite3.Connection, case_token: str, hold_id: str
) -> HoldRevision | None:
    """Return one identified hold for an existing case, or None when it was never asserted."""

    case = _check_case_token(case_token)
    hold = _check_hold_id(hold_id)
    with _read_transaction(database) as connection:
        _require_ready(connection)
        if not _case_exists(connection, case):
            raise UnknownCase()
        return _hold_row_in_db(connection, case, hold)


def case_snapshot(database: sqlite3.Connection, case_token: str) -> CaseRetentionSnapshot:
    """Return one consistent frozen snapshot of an existing case's retention facts."""

    case = _check_case_token(case_token)
    with _read_transaction(database) as connection:
        _require_ready(connection)
        return _case_snapshot_in_db(connection, case)


def evaluation_inputs(database: sqlite3.Connection, case_token: str) -> RetentionEvaluationInputs:
    """Return stored facts for a later pure evaluation, with no inferred blockers."""

    return case_snapshot(database, case_token).to_evaluation_inputs()


def record_policy(
    database: sqlite3.Connection,
    *,
    command_id: str,
    adoption: PolicyAdoption,
    timezone: ZoneInfo,
    at: datetime,
    retention_years: int = PROPOSED_RETENTION_YEARS,
    adoption_evidence_ref: str | None = None,
    expected_version: int | None = None,
) -> PolicyCommandResult:
    """Append one immutable policy version atomically with its command log row.

    ``expected_version`` may be omitted only to record the very first policy. Once any policy
    exists, an omitted or mismatched expected version is stale: the stored policy is never
    replaced implicitly.
    """

    command = _check_command_id(command_id)
    checked_adoption = _check_adoption(adoption)
    checked_timezone = _check_timezone(timezone)
    checked_years = _check_retention_years(retention_years)
    checked_evidence = _check_evidence(checked_adoption, adoption_evidence_ref)
    checked_at = _check_timestamp(at)
    checked_expected = _check_optional_version(expected_version)

    with _write_transaction(database) as connection:
        _require_ready(connection)
        seen = _policy_command_by_id(connection, command)
        if seen is not None:
            if (
                seen.adoption is not checked_adoption
                or seen.timezone.key != checked_timezone.key
                or seen.retention_years != checked_years
                or seen.adoption_evidence_ref != checked_evidence
                or seen.expected_version != checked_expected
                or seen.at != checked_at
            ):
                raise CommandIdReused()
            return _command_result(connection, command)

        current = len(_policy_revisions_in_db(connection))
        # An omitted expected version may only establish the first policy; afterwards the
        # exact current version is required, so no policy is replaced implicitly.
        applied = (checked_expected is None and current == 0) or checked_expected == current
        outcome = CommandOutcome.APPLIED if applied else CommandOutcome.STALE
        version = current
        if outcome is CommandOutcome.APPLIED:
            version = current + 1
            connection.execute(
                f"INSERT INTO pb_retention_policies ({_POLICY_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    command,
                    version,
                    checked_adoption.value,
                    checked_timezone.key,
                    checked_years,
                    checked_evidence,
                    _utc_text(checked_at),
                ),
            )
        connection.execute(
            f"INSERT INTO pb_retention_policy_commands ({_POLICY_COMMAND_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                command,
                checked_adoption.value,
                checked_timezone.key,
                checked_years,
                checked_evidence,
                checked_expected,
                outcome.value,
                version,
                _utc_text(checked_at),
            ),
        )
        return _command_result(connection, command)


def record_closure(
    database: sqlite3.Connection,
    *,
    command_id: str,
    case_token: str,
    kind: ClosureKind,
    anchor_at: datetime,
    source_ref: str,
    at: datetime,
    expected_generation: int | None = None,
) -> ClosureCommandResult:
    """Append one explicit closure anchor only when the expected generation matches.

    With no earlier anchor, ``expected_generation`` may be None (or 0). Once an anchor
    exists, it must be supplied exactly: None never silently replaces an earlier anchor.
    """

    command = _check_command_id(command_id)
    case = _check_case_token(case_token)
    checked_kind = _check_closure_kind(kind)
    checked_anchor = _check_timestamp(anchor_at)
    checked_source = _check_source_ref(source_ref)
    checked_at = _check_timestamp(at)
    checked_expected = _check_optional_generation(expected_generation, minimum=0)

    with _write_transaction(database) as connection:
        _require_ready(connection)
        seen = _closure_command_by_id(connection, command)
        if seen is not None:
            if (
                seen.case_token != case
                or seen.kind is not checked_kind
                or seen.anchor_at != checked_anchor
                or seen.source_ref != checked_source
                or seen.expected_generation != checked_expected
                or seen.at != checked_at
            ):
                raise CommandIdReused()
            return _closure_command_result(connection, command)

        if not _case_exists(connection, case):
            raise UnknownCase()

        current = len(_closure_revisions_in_db(connection, case))
        applied = (checked_expected is None and current == 0) or checked_expected == current
        outcome = CommandOutcome.APPLIED if applied else CommandOutcome.STALE
        generation = current
        if outcome is CommandOutcome.APPLIED:
            generation = current + 1
            connection.execute(
                f"INSERT INTO pb_retention_case_closures ({_CLOSURE_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    command,
                    case,
                    generation,
                    checked_kind.value,
                    _utc_text(checked_anchor),
                    checked_source,
                    _utc_text(checked_at),
                ),
            )
        connection.execute(
            f"INSERT INTO pb_retention_closure_commands ({_CLOSURE_COMMAND_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                command,
                case,
                checked_kind.value,
                _utc_text(checked_anchor),
                checked_source,
                checked_expected,
                outcome.value,
                generation,
                _utc_text(checked_at),
            ),
        )
        return _closure_command_result(connection, command)


def record_hold(
    database: sqlite3.Connection,
    *,
    command_id: str,
    case_token: str,
    hold_id: str,
    scope: DataScope,
    source_ref: str,
    at: datetime,
    expected_generation: int | None = None,
) -> HoldCommandResult:
    """Assert one identified scope-specific hold. Nothing expires it automatically.

    A hold identity is ``(case_token, hold_id)`` and its scope is fixed the first time it is
    asserted; a different scope is refused and needs a different hold id, even after a
    release. ``expected_generation`` may be omitted only to create a never-seen hold; any
    later re-assertion must name the exact current generation, and an omitted one is stale.
    """

    command = _check_command_id(command_id)
    case = _check_case_token(case_token)
    hold = _check_hold_id(hold_id)
    checked_scope = _check_scope(scope)
    checked_source = _check_source_ref(source_ref)
    checked_at = _check_timestamp(at)
    checked_expected = _check_optional_generation(expected_generation, minimum=0)

    with _write_transaction(database) as connection:
        _require_ready(connection)
        seen = _hold_command_by_id(connection, command)
        if seen is not None:
            if (
                seen.case_token != case
                or seen.hold_id != hold
                or seen.action is not HoldAction.HOLD
                or seen.request_scope is not checked_scope
                or seen.request_source_ref != checked_source
                or seen.expected_generation != checked_expected
                or seen.at != checked_at
            ):
                raise CommandIdReused()
            return _hold_command_result(connection, command)

        if not _case_exists(connection, case):
            raise UnknownCase()

        current = _hold_row_in_db(connection, case, hold)
        if current is not None and current.scope is not checked_scope:
            # The identity already owns a different scope: refuse before any state change
            # instead of quietly reclassifying an existing preservation hold.
            raise HoldScopeConflict()
        current_generation = 0 if current is None else current.generation
        # An omitted generation may only create a never-seen hold; re-asserting an existing
        # one always requires the exact current generation.
        applied = (checked_expected is None and current is None) or (
            checked_expected == current_generation
        )
        outcome = CommandOutcome.APPLIED if applied else CommandOutcome.STALE
        if outcome is CommandOutcome.APPLIED:
            generation = current_generation + 1
            connection.execute(
                "INSERT INTO pb_retention_holds "
                f"({_HOLD_COLUMNS}) VALUES (?, ?, ?, 1, ?, ?, ?, NULL) "
                "ON CONFLICT (case_token, hold_id) DO UPDATE SET "
                "generation=excluded.generation, active=1, scope=excluded.scope, "
                "source_ref=excluded.source_ref, held_at=excluded.held_at, released_at=NULL",
                (
                    case,
                    hold,
                    generation,
                    checked_scope.value,
                    checked_source,
                    _utc_text(checked_at),
                ),
            )
            post_scope: DataScope | None = checked_scope
            post_source: str | None = checked_source
            post_held_at: str | None = _utc_text(checked_at)
            post_released_at: str | None = None
            active = 1
        else:
            generation = current_generation
            post_scope = None if current is None else current.scope
            post_source = None if current is None else current.source_ref
            post_held_at = None if current is None else _utc_text(current.held_at)
            post_released_at = (
                None
                if current is None or current.released_at is None
                else _utc_text(current.released_at)
            )
            active = 0 if current is None or not current.active else 1
        connection.execute(
            f"INSERT INTO pb_retention_hold_commands ({_HOLD_COMMAND_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                command,
                case,
                hold,
                HoldAction.HOLD.value,
                checked_scope.value,
                checked_source,
                checked_expected,
                outcome.value,
                generation,
                active,
                None if post_scope is None else post_scope.value,
                post_source,
                post_held_at,
                post_released_at,
                _utc_text(checked_at),
            ),
        )
        return _hold_command_result(connection, command)


def release_hold(
    database: sqlite3.Connection,
    *,
    command_id: str,
    case_token: str,
    hold_id: str,
    at: datetime,
    expected_generation: int,
) -> HoldCommandResult:
    """Release one hold only at the exact generation the caller last observed.

    A stale expected generation changes nothing, so a stale release can never clear a
    newer hold.
    """

    command = _check_command_id(command_id)
    case = _check_case_token(case_token)
    hold = _check_hold_id(hold_id)
    checked_at = _check_timestamp(at)
    checked_expected = _check_required_generation(expected_generation, minimum=1)

    with _write_transaction(database) as connection:
        _require_ready(connection)
        seen = _hold_command_by_id(connection, command)
        if seen is not None:
            if (
                seen.case_token != case
                or seen.hold_id != hold
                or seen.action is not HoldAction.RELEASE
                or seen.request_scope is not None
                or seen.request_source_ref is not None
                or seen.expected_generation != checked_expected
                or seen.at != checked_at
            ):
                raise CommandIdReused()
            return _hold_command_result(connection, command)

        if not _case_exists(connection, case):
            raise UnknownCase()

        current = _hold_row_in_db(connection, case, hold)
        if current is not None and current.active and current.generation == checked_expected:
            outcome = CommandOutcome.APPLIED
            generation = current.generation + 1
            cursor = connection.execute(
                "UPDATE pb_retention_holds SET generation=?, active=0, released_at=? "
                "WHERE case_token=? AND hold_id=? AND generation=? AND active=1",
                (generation, _utc_text(checked_at), case, hold, checked_expected),
            )
            if cursor.rowcount != 1:
                raise _corrupted(RetentionStateRefusal.ROW_MALFORMED)
            post_scope: DataScope | None = current.scope
            post_source: str | None = current.source_ref
            post_held_at: str | None = _utc_text(current.held_at)
            post_released_at: str | None = _utc_text(checked_at)
            active = 0
        else:
            outcome = CommandOutcome.STALE
            generation = 0 if current is None else current.generation
            post_scope = None if current is None else current.scope
            post_source = None if current is None else current.source_ref
            post_held_at = None if current is None else _utc_text(current.held_at)
            post_released_at = (
                None
                if current is None or current.released_at is None
                else _utc_text(current.released_at)
            )
            active = 0 if current is None or not current.active else 1
        connection.execute(
            f"INSERT INTO pb_retention_hold_commands ({_HOLD_COMMAND_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                command,
                case,
                hold,
                HoldAction.RELEASE.value,
                None,
                None,
                checked_expected,
                outcome.value,
                generation,
                active,
                None if post_scope is None else post_scope.value,
                post_source,
                post_held_at,
                post_released_at,
                _utc_text(checked_at),
            ),
        )
        return _hold_command_result(connection, command)


__all__ = [
    "COMPONENT",
    "SCHEMA_VERSION",
    "CaseRetentionSnapshot",
    "ClosureCommandResult",
    "ClosureRevision",
    "CommandIdReused",
    "CommandOutcome",
    "HoldCommandResult",
    "HoldRevision",
    "HoldScopeConflict",
    "PolicyCommandResult",
    "RetentionEvaluationInputs",
    "RetentionPolicyRevision",
    "RetentionStateCorrupted",
    "RetentionStateInputError",
    "RetentionStateRefusal",
    "RetentionTransactionRefused",
    "UnknownCase",
    "case_snapshot",
    "closure_revisions",
    "closure_snapshot",
    "evaluation_inputs",
    "hold_snapshot",
    "holds_snapshot",
    "initialize_retention_state",
    "policy_revisions",
    "policy_snapshot",
    "record_closure",
    "record_hold",
    "record_policy",
    "release_hold",
]
