"""Durable beta contact permission and trusted-caller evidence on the existing database.

The store records what a trusted caller observed; it never proves an email was reached,
never sends, and never grants admission or case movement. Permission is an affirmative,
versioned act bound to one exact channel value, case and owner. Verification evidence and
reply attestations are separate append-only observations. Every observation must name the
primary address the trusted caller actually observed; that explicit value is canonicalized,
compared with the owner's current primary email before any write, and bound into the exact
request fingerprint. A changed primary email therefore makes earlier observations stale instead
of silently carrying them forward. A backup permission may be email or phone/SMS, but a phone
number is never treated as a verified email and reply evidence stays primary-email only. Login,
session, ``identity_accounts.verified_at`` and email-provider acceptance are never evidence here.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from parkingbreaker.identity_store import IdentityConflict, _canonical_email
from parkingbreaker.intake_store import IntakeConflict
from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.transaction_records import _reject_constant, _unique_object

BETA_CONTACT_DISCLOSURE_VERSION = 1
BETA_CONTACT_DISCLOSURE_TEXT = (
    "I agree that ParkingBreaker may contact me at this email address or phone number "
    "about this appeal and this case only. This permission is not marketing consent, is "
    "limited to this case and owner, and I can revoke it at any time."
)
BETA_CONTACT_DISCLOSURE_SHA256 = hashlib.sha256(BETA_CONTACT_DISCLOSURE_TEXT.encode()).hexdigest()
BETA_CONTACT_CHANNELS = frozenset({"email", "sms"})
BETA_CONTACT_EVIDENCE_KINDS = frozenset({"challenge_consumed", "human_same_address"})
BETA_CONTACT_REPLY_CHANNEL = "email"
BETA_CONTACT_REPLY_STATUS = "observed"
_DISCLOSURES = {BETA_CONTACT_DISCLOSURE_VERSION: BETA_CONTACT_DISCLOSURE_SHA256}

_TARGETS_DDL = """CREATE TABLE pb_beta_contact_targets (
    case_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    case_token TEXT NOT NULL,
    target_id TEXT NOT NULL UNIQUE,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    channel TEXT NOT NULL CHECK (channel IN ('email', 'sms')),
    contact_value TEXT NOT NULL,
    value_sha256 TEXT NOT NULL,
    disclosure_version INTEGER NOT NULL CHECK (disclosure_version >= 1),
    disclosure_sha256 TEXT NOT NULL,
    permitted INTEGER NOT NULL CHECK (permitted IN (0, 1)),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (case_id, owner)
)"""

_EVIDENCE_DDL = """CREATE TABLE pb_beta_contact_evidence (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    case_token TEXT NOT NULL,
    evidence_id TEXT NOT NULL UNIQUE,
    command_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('challenge_consumed', 'human_same_address')),
    channel TEXT NOT NULL CHECK (channel = 'email'),
    email_sha256 TEXT NOT NULL,
    operator_id TEXT,
    evidence_ref TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    CHECK ((kind = 'human_same_address') = (operator_id IS NOT NULL))
)"""

_REPLIES_DDL = """CREATE TABLE pb_beta_contact_replies (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    case_token TEXT NOT NULL,
    reply_id TEXT NOT NULL UNIQUE,
    command_id TEXT NOT NULL UNIQUE,
    target_id TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel = 'email'),
    status TEXT NOT NULL CHECK (status = 'observed'),
    value_sha256 TEXT NOT NULL,
    email_sha256 TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)"""

_COMMANDS_DDL = """CREATE TABLE pb_beta_contact_commands (
    command_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    command TEXT NOT NULL CHECK (
        command IN ('permission', 'revoke', 'challenge', 'human', 'reply')
    ),
    request_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)"""

_DDL = {
    "pb_beta_contact_targets": _TARGETS_DDL,
    "pb_beta_contact_evidence": _EVIDENCE_DDL,
    "pb_beta_contact_replies": _REPLIES_DDL,
    "pb_beta_contact_commands": _COMMANDS_DDL,
}


def _trigger(name: str, action: str, table: str, message: str) -> str:
    return (
        f"CREATE TRIGGER {name} BEFORE {action} ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{message}'); END"
    )


_TRIGGERS = {
    "pb_beta_contact_targets_no_delete": _trigger(
        "pb_beta_contact_targets_no_delete",
        "DELETE",
        "pb_beta_contact_targets",
        "beta contact targets are kept",
    ),
    "pb_beta_contact_evidence_no_update": _trigger(
        "pb_beta_contact_evidence_no_update",
        "UPDATE",
        "pb_beta_contact_evidence",
        "beta contact evidence is immutable",
    ),
    "pb_beta_contact_evidence_no_delete": _trigger(
        "pb_beta_contact_evidence_no_delete",
        "DELETE",
        "pb_beta_contact_evidence",
        "beta contact evidence is immutable",
    ),
    "pb_beta_contact_replies_no_update": _trigger(
        "pb_beta_contact_replies_no_update",
        "UPDATE",
        "pb_beta_contact_replies",
        "beta contact replies are immutable",
    ),
    "pb_beta_contact_replies_no_delete": _trigger(
        "pb_beta_contact_replies_no_delete",
        "DELETE",
        "pb_beta_contact_replies",
        "beta contact replies are immutable",
    ),
    "pb_beta_contact_commands_no_update": _trigger(
        "pb_beta_contact_commands_no_update",
        "UPDATE",
        "pb_beta_contact_commands",
        "beta contact commands are append-only",
    ),
    "pb_beta_contact_commands_no_delete": _trigger(
        "pb_beta_contact_commands_no_delete",
        "DELETE",
        "pb_beta_contact_commands",
        "beta contact commands are append-only",
    ),
}

_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}\Z")
_SHORT_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_ID32 = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_E164 = re.compile(r"\+[1-9][0-9]{7,14}\Z")


class BetaContactConflict(ValueError):
    """A refused contact command with a fixed key and no contact or evidence content."""


@dataclass(frozen=True, repr=False)
class ContactPermission:
    case_id: str
    owner: str
    target_id: str
    channel: str
    revision: int
    permitted: bool
    recorded_at: datetime

    def __repr__(self) -> str:
        return "ContactPermission(<redacted>)"


@dataclass(frozen=True, repr=False)
class ContactEvidence:
    case_id: str
    owner: str
    evidence_id: str
    kind: str
    recorded_at: datetime

    def __repr__(self) -> str:
        return "ContactEvidence(<redacted>)"


@dataclass(frozen=True, repr=False)
class ContactReply:
    case_id: str
    owner: str
    reply_id: str
    target_id: str
    recorded_at: datetime

    def __repr__(self) -> str:
        return "ContactReply(<redacted>)"


@dataclass(frozen=True, repr=False)
class ContactReadiness:
    permission: str
    permission_revision: int
    target_id: str | None
    verification_evidence: str
    reply_observation: str
    reason: str

    def __repr__(self) -> str:
        return "ContactReadiness(<redacted>)"


@dataclass(frozen=True)
class _Target:
    case_id: str
    owner: str
    case_token: str
    target_id: str
    revision: int
    channel: str
    contact_value: str
    value_sha256: str
    disclosure_version: int
    disclosure_sha256: str
    permitted: bool
    recorded_at: datetime


@dataclass(frozen=True)
class _Evidence:
    case_id: str
    owner: str
    case_token: str
    evidence_id: str
    kind: str
    email_sha256: str
    operator_id: str | None
    evidence_ref: str
    observed_at: datetime
    recorded_at: datetime


@dataclass(frozen=True)
class _Reply:
    case_id: str
    owner: str
    case_token: str
    reply_id: str
    target_id: str
    value_sha256: str
    email_sha256: str
    operator_id: str
    evidence_ref: str
    observed_at: datetime
    recorded_at: datetime


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


def _normalized(sql: object) -> str:
    return " ".join(sql.upper().split()) if isinstance(sql, str) else ""


def initialize_beta_contact(database: sqlite3.Connection) -> None:
    """Create or strictly validate only the beta contact component and its version row."""
    if database.in_transaction:
        raise StorageCorruption("beta contact initializer needs its own transaction")
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='beta_contact'"
        ).fetchone()
        tables = dict(
            database.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'pb_beta_contact_%'"
            ).fetchall()
        )
        triggers = dict(
            database.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'pb_beta_contact_%'"
            ).fetchall()
        )
        if version is None:
            if tables or triggers:
                raise StorageCorruption("unversioned beta contact schema")
            for ddl in _DDL.values():
                database.execute(ddl)
            for trigger in _TRIGGERS.values():
                database.execute(trigger)
            database.execute("INSERT INTO pb_store_versions VALUES ('beta_contact', 1)")
        elif version != (1,) or {name: _normalized(sql) for name, sql in tables.items()} != {
            name: _normalized(ddl) for name, ddl in _DDL.items()
        }:
            raise StorageCorruption("malformed beta contact schema")
        else:
            expected = {name: _normalized(ddl) for name, ddl in _TRIGGERS.items()}
            if {name: _normalized(sql) for name, sql in triggers.items()} != expected:
                raise StorageCorruption("malformed beta contact trigger")
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def _key(value: object, label: str) -> str:
    if not isinstance(value, str) or _KEY.fullmatch(value) is None:
        raise ValueError(f"invalid beta contact {label}")
    return value


def _short_key(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHORT_KEY.fullmatch(value) is None:
        raise ValueError(f"invalid beta contact {label}")
    return value


def _id32(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID32.fullmatch(value) is None:
        raise ValueError(f"invalid beta contact {label}")
    return value


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValueError(f"invalid beta contact {label}")
    return value


def _revision(value: object, label: str = "revision", *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"invalid beta contact {label}")
    return value


def _channel(value: object) -> str:
    if not isinstance(value, str) or value not in BETA_CONTACT_CHANNELS:
        raise ValueError("invalid beta contact channel")
    return value


def _channel_value(channel: object, value: object) -> tuple[str, str]:
    selected = _channel(channel)
    if not isinstance(value, str):
        raise ValueError("invalid beta contact contact value")
    if selected == "email":
        try:
            canonical = _canonical_email(value)
        except IdentityConflict:
            raise ValueError("invalid beta contact contact value") from None
    else:
        if _E164.fullmatch(value) is None:
            raise ValueError("invalid beta contact contact value")
        canonical = value
    return selected, canonical


def _value_digest(channel: str, canonical: str) -> str:
    return hashlib.sha256(f"{channel}\x00{canonical}".encode()).hexdigest()


def _email_digest(email: str) -> str:
    return hashlib.sha256(email.encode()).hexdigest()


def _now(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"invalid beta contact {label}")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"invalid beta contact {label}") from exc


def _observed(value: object, now: datetime, label: str) -> datetime:
    observed = _now(value, label)
    if observed > now:
        raise BetaContactConflict("beta_contact_future_timestamp")
    return observed


def _stored_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid stored beta contact timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid stored beta contact timestamp")
    normalized = parsed.astimezone(UTC)
    if normalized.isoformat() != value:
        raise ValueError("invalid stored beta contact timestamp")
    return normalized


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _request_sha(command: str, case_id: str, owner: str, fields: dict[str, object]) -> str:
    payload = {"command": command, "case_id": case_id, "owner": owner, **fields}
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def _load_result(value: str) -> dict[str, object]:
    try:
        loaded = json.loads(
            value, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise StorageCorruption("invalid beta contact command result") from exc
    if not isinstance(loaded, dict):
        raise StorageCorruption("invalid beta contact command result")
    return loaded


def _owned_case(database: sqlite3.Connection, case_id: str, owner: str) -> str:
    row = database.execute(
        "SELECT r.case_token, r.workspace_token, o.owner, c.workspace_token "
        "FROM pb_intake_records r "
        "JOIN pb_intake_owners o ON o.workspace_token=r.workspace_token "
        "LEFT JOIN pb_cases c ON c.case_token=r.case_token "
        "WHERE r.intake_id=? AND o.owner=?",
        (case_id, owner),
    ).fetchone()
    if row is None:
        raise IntakeConflict("not_found")
    case_token, intake_workspace, intake_owner, case_workspace = row
    if case_token is None:
        raise BetaContactConflict("beta_contact_case_required")
    if (
        not isinstance(case_token, str)
        or _ID32.fullmatch(case_token) is None
        or not isinstance(intake_workspace, str)
        or not isinstance(case_workspace, str)
        or intake_workspace != case_workspace
        or intake_owner != owner
    ):
        raise StorageCorruption("beta contact case binding mismatch")
    return case_token


def _primary_email_or_none(database: sqlite3.Connection, owner: str) -> str | None:
    row = database.execute(
        "SELECT email FROM pb_identity_accounts WHERE customer_id=?", (owner,)
    ).fetchone()
    if row is None:
        return None
    email = row[0]
    if not isinstance(email, str):
        raise StorageCorruption("malformed beta contact primary email")
    if email == "":
        return None
    try:
        canonical = _canonical_email(email)
    except IdentityConflict:
        raise StorageCorruption("malformed beta contact primary email") from None
    if canonical != email:
        raise StorageCorruption("malformed beta contact primary email")
    return canonical


def _primary_email(database: sqlite3.Connection, owner: str) -> str:
    email = _primary_email_or_none(database, owner)
    if email is None:
        raise BetaContactConflict("beta_contact_primary_email_required")
    return email


def _observed_email(value: object) -> str:
    """Canonicalize the address a trusted caller actually observed; never looked up."""
    if not isinstance(value, str):
        raise ValueError("invalid beta contact observed primary email")
    try:
        return _canonical_email(value)
    except IdentityConflict:
        raise ValueError("invalid beta contact observed primary email") from None


def _require_observed_primary_email(
    database: sqlite3.Connection, owner: str, observed: object
) -> str:
    """Refuse unless the explicit observation equals the owner's current exact address."""
    email = _observed_email(observed)
    if email != _primary_email(database, owner):
        raise BetaContactConflict("beta_contact_observed_email_mismatch")
    return email


def _target(database: sqlite3.Connection, case_id: str, owner: str) -> _Target | None:
    row = database.execute(
        "SELECT case_id, owner, case_token, target_id, revision, channel, contact_value, "
        "value_sha256, disclosure_version, disclosure_sha256, permitted, recorded_at "
        "FROM pb_beta_contact_targets WHERE case_id=? AND owner=?",
        (case_id, owner),
    ).fetchone()
    return None if row is None else _target_record(row)


def _target_record(row: tuple[object, ...]) -> _Target:
    try:
        (
            case_id,
            owner,
            case_token,
            target_id,
            revision,
            channel,
            contact_value,
            value_sha256,
            disclosure_version,
            disclosure_sha256,
            permitted,
            recorded_at,
        ) = row
        case_id = _key(case_id, "case id")
        owner = _key(owner, "owner")
        case_token = _id32(case_token, "case token")
        target_id = _id32(target_id, "target id")
        revision = _revision(revision, minimum=1)
        selected, canonical = _channel_value(channel, contact_value)
        if canonical != contact_value:
            raise ValueError("stored contact value is not canonical")
        digest = _hash(value_sha256, "contact digest")
        if _value_digest(selected, canonical) != digest:
            raise ValueError("stored contact digest mismatch")
        version = _revision(disclosure_version, "disclosure version", minimum=1)
        disclosure = _hash(disclosure_sha256, "disclosure hash")
        if _DISCLOSURES.get(version) != disclosure:
            raise ValueError("stored disclosure mismatch")
        if permitted not in (0, 1):
            raise ValueError("invalid stored permission")
        return _Target(
            case_id,
            owner,
            case_token,
            target_id,
            revision,
            selected,
            canonical,
            digest,
            version,
            disclosure,
            bool(permitted),
            _stored_time(recorded_at),
        )
    except StorageCorruption:
        raise
    except (TypeError, ValueError, KeyError, OverflowError, RecursionError) as exc:
        raise StorageCorruption("invalid beta contact target record") from exc


def _latest_evidence(database: sqlite3.Connection, case_id: str, owner: str) -> _Evidence | None:
    row = database.execute(
        "SELECT case_id, owner, case_token, evidence_id, kind, channel, email_sha256, "
        "operator_id, evidence_ref, observed_at, recorded_at FROM pb_beta_contact_evidence "
        "WHERE case_id=? AND owner=? ORDER BY sequence DESC LIMIT 1",
        (case_id, owner),
    ).fetchone()
    return None if row is None else _evidence_record(row)


def _evidence_record(row: tuple[object, ...]) -> _Evidence:
    try:
        (
            case_id,
            owner,
            case_token,
            evidence_id,
            kind,
            channel,
            email_sha256,
            operator_id,
            evidence_ref,
            observed_at,
            recorded_at,
        ) = row
        case_id = _key(case_id, "case id")
        owner = _key(owner, "owner")
        case_token = _id32(case_token, "case token")
        evidence_id = _id32(evidence_id, "evidence id")
        if not isinstance(kind, str) or kind not in BETA_CONTACT_EVIDENCE_KINDS:
            raise ValueError("invalid stored evidence kind")
        if channel != "email":
            raise ValueError("invalid stored evidence channel")
        email_sha256 = _hash(email_sha256, "email digest")
        if kind == "human_same_address":
            operator = _short_key(operator_id, "operator id")
        elif operator_id is not None:
            raise ValueError("challenge evidence cannot carry an operator")
        else:
            operator = None
        evidence_ref = _key(evidence_ref, "evidence reference")
        observed = _stored_time(observed_at)
        recorded = _stored_time(recorded_at)
        if observed > recorded:
            raise ValueError("stored evidence is in the future")
        return _Evidence(
            case_id,
            owner,
            case_token,
            evidence_id,
            kind,
            email_sha256,
            operator,
            evidence_ref,
            observed,
            recorded,
        )
    except StorageCorruption:
        raise
    except (TypeError, ValueError, KeyError, OverflowError, RecursionError) as exc:
        raise StorageCorruption("invalid beta contact evidence record") from exc


def _latest_reply(database: sqlite3.Connection, case_id: str, owner: str) -> _Reply | None:
    row = database.execute(
        "SELECT case_id, owner, case_token, reply_id, target_id, value_sha256, email_sha256, "
        "operator_id, evidence_ref, observed_at, recorded_at, channel, status "
        "FROM pb_beta_contact_replies "
        "WHERE case_id=? AND owner=? ORDER BY sequence DESC LIMIT 1",
        (case_id, owner),
    ).fetchone()
    return None if row is None else _reply_record(row)


def _reply_record(row: tuple[object, ...]) -> _Reply:
    try:
        (
            case_id,
            owner,
            case_token,
            reply_id,
            target_id,
            value_sha256,
            email_sha256,
            operator_id,
            evidence_ref,
            observed_at,
            recorded_at,
            channel,
            status,
        ) = row
        case_id = _key(case_id, "case id")
        owner = _key(owner, "owner")
        case_token = _id32(case_token, "case token")
        reply_id = _id32(reply_id, "reply id")
        if channel != BETA_CONTACT_REPLY_CHANNEL or status != BETA_CONTACT_REPLY_STATUS:
            raise ValueError("invalid stored reply channel or status")
        target_id = _id32(target_id, "target id")
        value_sha256 = _hash(value_sha256, "contact digest")
        email_sha256 = _hash(email_sha256, "email digest")
        operator_id = _short_key(operator_id, "operator id")
        evidence_ref = _key(evidence_ref, "evidence reference")
        observed = _stored_time(observed_at)
        recorded = _stored_time(recorded_at)
        if observed > recorded:
            raise ValueError("stored reply is in the future")
        return _Reply(
            case_id,
            owner,
            case_token,
            reply_id,
            target_id,
            value_sha256,
            email_sha256,
            operator_id,
            evidence_ref,
            observed,
            recorded,
        )
    except StorageCorruption:
        raise
    except (TypeError, ValueError, KeyError, OverflowError, RecursionError) as exc:
        raise StorageCorruption("invalid beta contact reply record") from exc


def _replay(
    database: sqlite3.Connection,
    command_id: str,
    case_id: str,
    owner: str,
    command: str,
    request_sha256: str,
) -> str | None:
    row = database.execute(
        "SELECT case_id, owner, command, request_sha256, result_json, recorded_at "
        "FROM pb_beta_contact_commands WHERE command_id=?",
        (command_id,),
    ).fetchone()
    if row is None:
        return None
    if row[0] != case_id or row[1] != owner or row[2] != command or row[3] != request_sha256:
        raise BetaContactConflict("beta_contact_command_reused")
    if not isinstance(row[4], str):
        raise StorageCorruption("invalid beta contact command result")
    _stored_time(row[5])
    return row[4]


def _permission_result(value: str, case_id: str, owner: str) -> ContactPermission:
    loaded = _load_result(value)
    if set(loaded) != {"target_id", "channel", "revision", "permitted", "recorded_at"}:
        raise StorageCorruption("invalid beta contact command result")
    try:
        permitted = loaded["permitted"]
        if type(permitted) is not bool:
            raise ValueError("invalid stored permission")
        return ContactPermission(
            case_id,
            owner,
            _id32(loaded["target_id"], "target id"),
            _channel(loaded["channel"]),
            _revision(loaded["revision"], minimum=1),
            permitted,
            _stored_time(loaded["recorded_at"]),
        )
    except (TypeError, ValueError, KeyError, OverflowError, RecursionError) as exc:
        raise StorageCorruption("invalid beta contact command result") from exc


def _evidence_result(value: str, case_id: str, owner: str) -> ContactEvidence:
    loaded = _load_result(value)
    if set(loaded) != {"evidence_id", "kind", "recorded_at"}:
        raise StorageCorruption("invalid beta contact command result")
    try:
        kind = loaded["kind"]
        if not isinstance(kind, str) or kind not in BETA_CONTACT_EVIDENCE_KINDS:
            raise ValueError("invalid stored evidence kind")
        return ContactEvidence(
            case_id,
            owner,
            _id32(loaded["evidence_id"], "evidence id"),
            kind,
            _stored_time(loaded["recorded_at"]),
        )
    except (TypeError, ValueError, KeyError, OverflowError, RecursionError) as exc:
        raise StorageCorruption("invalid beta contact command result") from exc


def _reply_result(value: str, case_id: str, owner: str) -> ContactReply:
    loaded = _load_result(value)
    if set(loaded) != {"reply_id", "target_id", "recorded_at"}:
        raise StorageCorruption("invalid beta contact command result")
    try:
        return ContactReply(
            case_id,
            owner,
            _id32(loaded["reply_id"], "reply id"),
            _id32(loaded["target_id"], "target id"),
            _stored_time(loaded["recorded_at"]),
        )
    except (TypeError, ValueError, KeyError, OverflowError, RecursionError) as exc:
        raise StorageCorruption("invalid beta contact command result") from exc


def _log_command(
    database: sqlite3.Connection,
    command_id: str,
    case_id: str,
    owner: str,
    command: str,
    request_sha256: str,
    result_json: str,
    recorded_at: datetime,
) -> None:
    database.execute(
        "INSERT INTO pb_beta_contact_commands VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            command_id,
            case_id,
            owner,
            command,
            request_sha256,
            result_json,
            recorded_at.isoformat(),
        ),
    )


def _begin(database: sqlite3.Connection) -> None:
    if database.in_transaction:
        raise StorageCorruption("beta contact command needs its own transaction")
    database.execute("BEGIN IMMEDIATE")


def _authorize(operator_id: object, authorized_operators: object) -> str:
    """Default-deny: only an exact bounded ID inside an exact frozenset[str] authorizes."""
    operator = _short_key(operator_id, "operator id")
    if type(authorized_operators) is not frozenset or not authorized_operators:
        raise BetaContactConflict("beta_contact_operator_not_authorized")
    for member in authorized_operators:
        if not isinstance(member, str) or _SHORT_KEY.fullmatch(member) is None:
            raise BetaContactConflict("beta_contact_operator_not_authorized")
    if operator not in authorized_operators:
        raise BetaContactConflict("beta_contact_operator_not_authorized")
    return operator


class BetaContactStoreMixin:
    """Record permission, verification evidence and replies on the existing connection."""

    def record_beta_contact_permission(
        self: _Connected,
        case_id: str,
        owner: str,
        command_id: str,
        channel: str,
        value: str,
        now: datetime,
        *,
        disclosure_version: int,
        disclosure_sha256: str,
        expected_revision: int,
    ) -> ContactPermission:
        """Record an affirmative permission bound to one exact value and revision."""
        case_id = _key(case_id, "case id")
        owner = _key(owner, "owner")
        command_id = _short_key(command_id, "command id")
        selected, canonical = _channel_value(channel, value)
        digest = _value_digest(selected, canonical)
        version = _revision(disclosure_version, "disclosure version", minimum=1)
        disclosure = _hash(disclosure_sha256, "disclosure hash")
        if _DISCLOSURES.get(version) != disclosure:
            raise BetaContactConflict("beta_contact_disclosure_changed")
        expected = _revision(expected_revision, "expected revision")
        recorded = _now(now, "timestamp")
        request = _request_sha(
            "permission",
            case_id,
            owner,
            {
                "channel": selected,
                "value_sha256": digest,
                "disclosure_version": version,
                "disclosure_sha256": disclosure,
                "expected_revision": expected,
            },
        )
        database = self._require_open()
        _begin(database)
        try:
            case_token = _owned_case(database, case_id, owner)
            replayed = _replay(database, command_id, case_id, owner, "permission", request)
            if replayed is not None:
                result = _permission_result(replayed, case_id, owner)
                database.execute("COMMIT")
                return result
            current = _target(database, case_id, owner)
            if current is not None and current.case_token != case_token:
                raise StorageCorruption("beta contact case binding mismatch")
            current_revision = 0 if current is None else current.revision
            if expected != current_revision:
                raise BetaContactConflict("beta_contact_stale_revision")
            revision = current_revision + 1
            target_id = uuid4().hex
            database.execute(
                "INSERT INTO pb_beta_contact_targets "
                "(case_id, owner, case_token, target_id, revision, channel, contact_value, "
                "value_sha256, disclosure_version, disclosure_sha256, permitted, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?) "
                "ON CONFLICT (case_id, owner) DO UPDATE SET case_token=excluded.case_token, "
                "target_id=excluded.target_id, revision=excluded.revision, "
                "channel=excluded.channel, contact_value=excluded.contact_value, "
                "value_sha256=excluded.value_sha256, "
                "disclosure_version=excluded.disclosure_version, "
                "disclosure_sha256=excluded.disclosure_sha256, permitted=1, "
                "recorded_at=excluded.recorded_at",
                (
                    case_id,
                    owner,
                    case_token,
                    target_id,
                    revision,
                    selected,
                    canonical,
                    digest,
                    version,
                    disclosure,
                    recorded.isoformat(),
                ),
            )
            result = ContactPermission(
                case_id, owner, target_id, selected, revision, True, recorded
            )
            _log_command(
                database,
                command_id,
                case_id,
                owner,
                "permission",
                request,
                _canonical(
                    {
                        "target_id": result.target_id,
                        "channel": result.channel,
                        "revision": result.revision,
                        "permitted": result.permitted,
                        "recorded_at": result.recorded_at.isoformat(),
                    }
                ),
                recorded,
            )
            database.execute("COMMIT")
            return result
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def revoke_beta_contact_permission(
        self: _Connected,
        case_id: str,
        owner: str,
        command_id: str,
        now: datetime,
        *,
        expected_revision: int,
    ) -> ContactPermission:
        """Explicitly revoke the current permission without deleting its history."""
        case_id = _key(case_id, "case id")
        owner = _key(owner, "owner")
        command_id = _short_key(command_id, "command id")
        expected = _revision(expected_revision, "expected revision")
        recorded = _now(now, "timestamp")
        request = _request_sha("revoke", case_id, owner, {"expected_revision": expected})
        database = self._require_open()
        _begin(database)
        try:
            case_token = _owned_case(database, case_id, owner)
            replayed = _replay(database, command_id, case_id, owner, "revoke", request)
            if replayed is not None:
                result = _permission_result(replayed, case_id, owner)
                database.execute("COMMIT")
                return result
            current = _target(database, case_id, owner)
            if current is None:
                raise BetaContactConflict("beta_contact_permission_missing")
            if current.case_token != case_token:
                raise StorageCorruption("beta contact case binding mismatch")
            if expected != current.revision:
                raise BetaContactConflict("beta_contact_stale_revision")
            if not current.permitted:
                raise BetaContactConflict("beta_contact_permission_revoked")
            revision = current.revision + 1
            database.execute(
                "UPDATE pb_beta_contact_targets SET revision=?, permitted=0, recorded_at=? "
                "WHERE case_id=? AND owner=?",
                (revision, recorded.isoformat(), case_id, owner),
            )
            result = ContactPermission(
                case_id, owner, current.target_id, current.channel, revision, False, recorded
            )
            _log_command(
                database,
                command_id,
                case_id,
                owner,
                "revoke",
                request,
                _canonical(
                    {
                        "target_id": result.target_id,
                        "channel": result.channel,
                        "revision": result.revision,
                        "permitted": result.permitted,
                        "recorded_at": result.recorded_at.isoformat(),
                    }
                ),
                recorded,
            )
            database.execute("COMMIT")
            return result
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def record_beta_contact_challenge(
        self: _Connected,
        case_id: str,
        owner: str,
        command_id: str,
        challenge_ref: str,
        consumed_at: datetime,
        now: datetime,
        *,
        observed_primary_email: str,
    ) -> ContactEvidence:
        """Record trusted-caller challenge evidence for the explicitly observed address."""
        case_id = _key(case_id, "case id")
        owner = _key(owner, "owner")
        command_id = _short_key(command_id, "command id")
        challenge_ref = _key(challenge_ref, "evidence reference")
        observed_email = _observed_email(observed_primary_email)
        observed = _observed(consumed_at, _now(now, "timestamp"), "consumed at")
        recorded = _now(now, "timestamp")
        request = _request_sha(
            "challenge",
            case_id,
            owner,
            {
                "kind": "challenge_consumed",
                "observed_primary_email_sha256": _email_digest(observed_email),
                "evidence_ref": challenge_ref,
                "observed_at": observed.isoformat(),
            },
        )
        database = self._require_open()
        _begin(database)
        try:
            case_token = _owned_case(database, case_id, owner)
            replayed = _replay(database, command_id, case_id, owner, "challenge", request)
            if replayed is not None:
                result = _evidence_result(replayed, case_id, owner)
                database.execute("COMMIT")
                return result
            email_sha256 = _email_digest(
                _require_observed_primary_email(database, owner, observed_email)
            )
            evidence_id = uuid4().hex
            database.execute(
                "INSERT INTO pb_beta_contact_evidence "
                "(case_id, owner, case_token, evidence_id, command_id, kind, channel, "
                "email_sha256, operator_id, evidence_ref, observed_at, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, 'challenge_consumed', 'email', ?, NULL, ?, ?, ?)",
                (
                    case_id,
                    owner,
                    case_token,
                    evidence_id,
                    command_id,
                    email_sha256,
                    challenge_ref,
                    observed.isoformat(),
                    recorded.isoformat(),
                ),
            )
            result = ContactEvidence(case_id, owner, evidence_id, "challenge_consumed", recorded)
            _log_command(
                database,
                command_id,
                case_id,
                owner,
                "challenge",
                request,
                _canonical(
                    {
                        "evidence_id": result.evidence_id,
                        "kind": result.kind,
                        "recorded_at": result.recorded_at.isoformat(),
                    }
                ),
                recorded,
            )
            database.execute("COMMIT")
            return result
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def record_beta_contact_human_verification(
        self: _Connected,
        case_id: str,
        owner: str,
        command_id: str,
        operator_id: str,
        verification_ref: str,
        observed_at: datetime,
        now: datetime,
        *,
        authorized_operators: frozenset[str] | None = None,
        observed_primary_email: str,
    ) -> ContactEvidence:
        """Record restricted-human same-address verification; default-deny allowlist."""
        case_id = _key(case_id, "case id")
        owner = _key(owner, "owner")
        command_id = _short_key(command_id, "command id")
        operator = _authorize(operator_id, authorized_operators)
        observed_email = _observed_email(observed_primary_email)
        observed = _observed(observed_at, _now(now, "timestamp"), "observed at")
        recorded = _now(now, "timestamp")
        evidence_ref = _key(verification_ref, "evidence reference")
        request = _request_sha(
            "human",
            case_id,
            owner,
            {
                "kind": "human_same_address",
                "operator_id": operator,
                "observed_primary_email_sha256": _email_digest(observed_email),
                "evidence_ref": evidence_ref,
                "observed_at": observed.isoformat(),
            },
        )
        database = self._require_open()
        _begin(database)
        try:
            case_token = _owned_case(database, case_id, owner)
            replayed = _replay(database, command_id, case_id, owner, "human", request)
            if replayed is not None:
                result = _evidence_result(replayed, case_id, owner)
                database.execute("COMMIT")
                return result
            email_sha256 = _email_digest(
                _require_observed_primary_email(database, owner, observed_email)
            )
            evidence_id = uuid4().hex
            database.execute(
                "INSERT INTO pb_beta_contact_evidence "
                "(case_id, owner, case_token, evidence_id, command_id, kind, channel, "
                "email_sha256, operator_id, evidence_ref, observed_at, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, 'human_same_address', 'email', ?, ?, ?, ?, ?)",
                (
                    case_id,
                    owner,
                    case_token,
                    evidence_id,
                    command_id,
                    email_sha256,
                    operator,
                    evidence_ref,
                    observed.isoformat(),
                    recorded.isoformat(),
                ),
            )
            result = ContactEvidence(case_id, owner, evidence_id, "human_same_address", recorded)
            _log_command(
                database,
                command_id,
                case_id,
                owner,
                "human",
                request,
                _canonical(
                    {
                        "evidence_id": result.evidence_id,
                        "kind": result.kind,
                        "recorded_at": result.recorded_at.isoformat(),
                    }
                ),
                recorded,
            )
            database.execute("COMMIT")
            return result
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def record_beta_contact_reply(
        self: _Connected,
        case_id: str,
        owner: str,
        command_id: str,
        target_id: str,
        operator_id: str,
        evidence_ref: str,
        observed_at: datetime,
        now: datetime,
        *,
        authorized_operators: frozenset[str] | None = None,
        observed_primary_email: str,
    ) -> ContactReply:
        """Record one observed email reply bound to the exact target and observed address.

        The backup permission channel is independent: an ``sms`` target may coexist with an
        actual primary-email reply. A phone/SMS value is never treated as a verified email.
        """
        case_id = _key(case_id, "case id")
        owner = _key(owner, "owner")
        command_id = _short_key(command_id, "command id")
        target_id = _id32(target_id, "target id")
        operator = _authorize(operator_id, authorized_operators)
        evidence_ref = _key(evidence_ref, "evidence reference")
        observed_email = _observed_email(observed_primary_email)
        observed = _observed(observed_at, _now(now, "timestamp"), "observed at")
        recorded = _now(now, "timestamp")
        request = _request_sha(
            "reply",
            case_id,
            owner,
            {
                "target_id": target_id,
                "operator_id": operator,
                "observed_primary_email_sha256": _email_digest(observed_email),
                "evidence_ref": evidence_ref,
                "observed_at": observed.isoformat(),
            },
        )
        database = self._require_open()
        _begin(database)
        try:
            case_token = _owned_case(database, case_id, owner)
            replayed = _replay(database, command_id, case_id, owner, "reply", request)
            if replayed is not None:
                result = _reply_result(replayed, case_id, owner)
                database.execute("COMMIT")
                return result
            current = _target(database, case_id, owner)
            if current is None:
                raise BetaContactConflict("beta_contact_target_missing")
            if current.case_token != case_token:
                raise StorageCorruption("beta contact case binding mismatch")
            if not current.permitted:
                raise BetaContactConflict("beta_contact_permission_revoked")
            if current.target_id != target_id:
                raise BetaContactConflict("beta_contact_target_changed")
            email_sha256 = _email_digest(
                _require_observed_primary_email(database, owner, observed_email)
            )
            reply_id = uuid4().hex
            database.execute(
                "INSERT INTO pb_beta_contact_replies "
                "(case_id, owner, case_token, reply_id, command_id, target_id, channel, "
                "status, value_sha256, email_sha256, operator_id, evidence_ref, "
                "observed_at, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'email', 'observed', ?, ?, ?, ?, ?, ?)",
                (
                    case_id,
                    owner,
                    case_token,
                    reply_id,
                    command_id,
                    target_id,
                    current.value_sha256,
                    email_sha256,
                    operator,
                    evidence_ref,
                    observed.isoformat(),
                    recorded.isoformat(),
                ),
            )
            result = ContactReply(case_id, owner, reply_id, target_id, recorded)
            _log_command(
                database,
                command_id,
                case_id,
                owner,
                "reply",
                request,
                _canonical(
                    {
                        "reply_id": result.reply_id,
                        "target_id": result.target_id,
                        "recorded_at": result.recorded_at.isoformat(),
                    }
                ),
                recorded,
            )
            database.execute("COMMIT")
            return result
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def beta_contact_readiness(self: _Connected, case_id: str, owner: str) -> ContactReadiness:
        """Return minimal fixed reasons; never sends, admits or interprets a deadline."""
        case_id = _key(case_id, "case id")
        owner = _key(owner, "owner")
        database = self._require_open()
        case_token = _owned_case(database, case_id, owner)
        email = _primary_email_or_none(database, owner)
        email_sha256 = None if email is None else _email_digest(email)
        target = _target(database, case_id, owner)
        if target is not None and target.case_token != case_token:
            raise StorageCorruption("beta contact case binding mismatch")
        evidence = _latest_evidence(database, case_id, owner)
        if evidence is not None and evidence.case_token != case_token:
            raise StorageCorruption("beta contact case binding mismatch")
        reply = _latest_reply(database, case_id, owner)
        if reply is not None and reply.case_token != case_token:
            raise StorageCorruption("beta contact case binding mismatch")
        if target is None:
            permission = "missing"
            permission_revision = 0
        elif target.permitted:
            permission = "permitted"
            permission_revision = target.revision
        else:
            permission = "revoked"
            permission_revision = target.revision
        if evidence is None:
            verification = "missing"
        elif email_sha256 is None or evidence.email_sha256 != email_sha256:
            verification = "stale"
        else:
            verification = "supplied"
        if reply is None:
            reply_state = "missing"
        elif (
            target is None
            or not target.permitted
            or reply.target_id != target.target_id
            or reply.value_sha256 != target.value_sha256
            or email_sha256 is None
            or reply.email_sha256 != email_sha256
        ):
            reply_state = "stale"
        else:
            reply_state = "supplied"
        if target is None:
            reason = "beta_contact_permission_missing"
        elif not target.permitted:
            reason = "beta_contact_permission_revoked"
        elif verification == "missing":
            reason = "beta_contact_verification_missing"
        elif verification == "stale":
            reason = "beta_contact_verification_stale"
        elif reply_state == "missing":
            reason = "beta_contact_reply_missing"
        elif reply_state == "stale":
            reason = "beta_contact_reply_stale"
        else:
            reason = "beta_contact_ready"
        return ContactReadiness(
            permission,
            permission_revision,
            None if target is None else target.target_id,
            verification,
            reply_state,
            reason,
        )
