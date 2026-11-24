"""Append-only municipal mail-route evidence on the transaction store.

Trusted operators supply official evidence. The registry persists their scoped decisions
and binds the observed revision to a frozen mail attempt; it never enrolls from research.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from parkingbreaker.agencies import SourceScope
from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.mail_config import MailClass

_COMMANDS = "pb_municipal_route_commands"
_REVISIONS = "pb_municipal_route_revisions"
_BINDINGS = "pb_mail_route_bindings"
_TABLES = {
    _COMMANDS: """CREATE TABLE pb_municipal_route_commands (
        command_id TEXT PRIMARY KEY NOT NULL,
        issuer TEXT NOT NULL,
        stage TEXT NOT NULL,
        mail_class TEXT NOT NULL CHECK (mail_class IN ('certified', 'first_class')),
        request_sha256 TEXT NOT NULL,
        expected_revision INTEGER NOT NULL CHECK (expected_revision >= 0),
        observed_revision INTEGER NOT NULL CHECK (observed_revision >= 0),
        attempted_at TEXT NOT NULL
    )""",
    _REVISIONS: """CREATE TABLE pb_municipal_route_revisions (
        issuer TEXT NOT NULL,
        stage TEXT NOT NULL,
        mail_class TEXT NOT NULL CHECK (mail_class IN ('certified', 'first_class')),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        command_id TEXT NOT NULL UNIQUE REFERENCES pb_municipal_route_commands(command_id),
        status TEXT NOT NULL CHECK (status IN ('verified', 'suspended', 'revoked')),
        address_fingerprint TEXT,
        source_url TEXT,
        source_quote TEXT,
        evidence_sha256 TEXT,
        valid_until TEXT,
        source_text_sha256 TEXT,
        CHECK (status <> 'verified' OR (address_fingerprint IS NOT NULL
            AND source_url IS NOT NULL AND source_quote IS NOT NULL
            AND evidence_sha256 IS NOT NULL AND valid_until IS NOT NULL)),
        PRIMARY KEY (issuer, stage, mail_class, revision)
    )""",
    _BINDINGS: """CREATE TABLE pb_mail_route_bindings (
        case_token TEXT NOT NULL,
        attempt INTEGER NOT NULL CHECK (attempt IN (1, 2)),
        issuer TEXT NOT NULL,
        stage TEXT NOT NULL,
        mail_class TEXT NOT NULL CHECK (mail_class IN ('certified', 'first_class')),
        route_revision INTEGER CHECK (route_revision >= 1),
        PRIMARY KEY (case_token, attempt),
        FOREIGN KEY (case_token, attempt) REFERENCES pb_mail_attempts(case_token, attempt),
        FOREIGN KEY (issuer, stage, mail_class, route_revision)
            REFERENCES pb_municipal_route_revisions(issuer, stage, mail_class, revision)
    )""",
}
# Reject REPLACE too, even on connections that omit recursive delete triggers.
_INSERT_GUARDS = {
    _COMMANDS: "EXISTS (SELECT 1 FROM pb_municipal_route_commands WHERE command_id=NEW.command_id)",
    _REVISIONS: "EXISTS (SELECT 1 FROM pb_municipal_route_revisions "
    "WHERE command_id=NEW.command_id) OR NEW.revision <> COALESCE("
    "(SELECT MAX(revision) FROM pb_municipal_route_revisions "
    "WHERE issuer=NEW.issuer AND stage=NEW.stage AND mail_class=NEW.mail_class), 0) + 1",
    _BINDINGS: "EXISTS (SELECT 1 FROM pb_mail_route_bindings "
    "WHERE case_token=NEW.case_token AND attempt=NEW.attempt)",
}
_SCHEMA = {
    **_TABLES,
    **{
        f"{table}_no_{action.lower()}": f"CREATE TRIGGER {table}_no_{action.lower()} "
        f"BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'route history is immutable'); END"
        for table in _TABLES
        for action in ("UPDATE", "DELETE")
    },
    **{
        f"{table}_append": f"CREATE TRIGGER {table}_append BEFORE INSERT ON {table} "
        f"WHEN {condition} BEGIN SELECT RAISE(ABORT, 'route history must append'); END"
        for table, condition in _INSERT_GUARDS.items()
    },
    "pb_municipal_route_verified_address": (
        "CREATE INDEX pb_municipal_route_verified_address ON pb_municipal_route_revisions "
        "(address_fingerprint, issuer, stage, mail_class) WHERE status='verified'"
    ),
}

_SELECT_REVISION = """SELECT r.issuer, r.stage, r.mail_class, r.revision, r.status,
    r.address_fingerprint, r.source_url, r.source_quote, r.evidence_sha256, r.valid_until,
    c.attempted_at, r.command_id, r.source_text_sha256, c.request_sha256,
    c.expected_revision, c.observed_revision
    FROM pb_municipal_route_revisions r
    LEFT JOIN pb_municipal_route_commands c ON r.command_id=c.command_id """
_SCOPE_QUERY = "r.issuer=? AND r.stage=? AND r.mail_class=?"
_LATEST_QUERY = """r.revision=(SELECT MAX(latest.revision) FROM pb_municipal_route_revisions latest
    WHERE latest.issuer=r.issuer AND latest.stage=r.stage AND latest.mail_class=r.mail_class)"""
_FIRST_STAGE = frozenset(
    {
        "initial review",
        "administrative review",
        "first level review",
        "first stage review",
        "initial protest",
        "first level protest",
        "first stage protest",
    }
)
_STAGE_DASHES = str.maketrans("\u2010\u2011\u2012\u2013\u2014\u2212", "------")
_HEARING_STAGE = frozenset({"administrative hearing", "hearing request", "second level review"})
_SHA256 = re.compile(r"[0-9a-f]{64}")


class RouteConflict(ValueError):
    """A route command conflicts with an observed revision or a frozen binding."""


def _key(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or not value.isprintable():
        raise ValueError(f"invalid route {field_name}")
    return value


def _stage(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid route stage")
    normalized = " ".join(unicodedata.normalize("NFKC", value).translate(_STAGE_DASHES).split())
    normalized = _key(normalized.casefold(), "stage")
    words = " ".join(normalized.replace("-", " ").split())
    if words in _FIRST_STAGE:
        return "first-level review"
    return "administrative hearing" if words in _HEARING_STAGE else normalized


def _time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("route timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _load_time(value: str) -> datetime:
    converted = _time(datetime.fromisoformat(value))
    if converted.isoformat() != value:
        raise ValueError("invalid stored route timestamp")
    return converted


def _revision_number(value: object, *, minimum: int) -> int:
    if type(value) is not int or not minimum <= value < (1 << 63):
        raise ValueError("invalid route revision number")
    return value


def _check_sha256(value: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError("invalid route SHA-256")


def _check_source_url(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or any(not char.isprintable() or char.isspace() or char == "\\" for char in value)
    ):
        raise ValueError("invalid route source URL")
    parts = urlsplit(value)
    if parts.scheme != "https" or "@" in parts.netloc or parts.port not in (None, 443):
        raise ValueError("route source URL must use HTTPS without credentials")
    SourceScope((parts.hostname or "").removesuffix("."))


@dataclass(frozen=True)
class RouteScope:
    issuer: str
    stage: str
    mail_class: str

    def __post_init__(self) -> None:
        _key(self.issuer, "issuer")
        object.__setattr__(self, "stage", _stage(self.stage))
        object.__setattr__(self, "mail_class", MailClass(self.mail_class).value)


@dataclass(frozen=True)
class RouteRevision:
    scope: RouteScope
    revision: int
    status: str
    address_fingerprint: str | None
    source_url: str | None
    source_quote: str | None = field(repr=False)
    evidence_sha256: str | None
    valid_until: datetime | None
    changed_at: datetime
    command_id: str
    source_text_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, RouteScope):
            raise ValueError("invalid route scope")
        _revision_number(self.revision, minimum=1)
        _key(self.command_id, "command ID")
        if self.status not in ("verified", "suspended", "revoked"):
            raise ValueError("invalid route status")
        for digest in (self.address_fingerprint, self.evidence_sha256, self.source_text_sha256):
            if digest is not None:
                _check_sha256(digest)
        if self.source_url is not None:
            _check_source_url(self.source_url)
        if self.source_quote is not None and (
            not isinstance(self.source_quote, str)
            or not self.source_quote.strip()
            or "\x00" in self.source_quote
        ):
            raise ValueError("invalid route source quote")
        object.__setattr__(self, "changed_at", _time(self.changed_at))
        if self.valid_until is not None:
            object.__setattr__(self, "valid_until", _time(self.valid_until))
        if self.status == "verified" and None in (
            self.address_fingerprint,
            self.source_url,
            self.source_quote,
            self.evidence_sha256,
            self.valid_until,
        ):
            raise ValueError("verified route requires source evidence, address and expiry")

    def verified_at(self, now: datetime) -> bool:
        """Whether this revision's verification is in force at the supplied instant."""
        now = _time(now)
        return (
            self.status == "verified"
            and self.valid_until is not None
            and self.changed_at <= now < self.valid_until
        )


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...
    def submission_ownership(self) -> AbstractContextManager[None]: ...
    def route_check_current(self, route: RouteRevision, now: datetime) -> bool: ...


def initialize_route_registry(database: sqlite3.Connection) -> None:
    """Install the independent route component with existing mail attempts unbound."""
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='municipal_routes'"
        ).fetchone()
        objects = {
            name: " ".join(sql.upper().split())
            for name, sql in database.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE tbl_name IN (?, ?, ?) AND sql IS NOT NULL",
                tuple(_TABLES),
            )
        }
        expected = {name: " ".join(sql.upper().split()) for name, sql in _SCHEMA.items()}
        if version is None:
            if objects:
                raise StorageCorruption("unversioned municipal route schema")
            for ddl in _SCHEMA.values():
                database.execute(ddl)
            database.execute("INSERT INTO pb_store_versions VALUES ('municipal_routes', 1)")
        elif version != (1,) or objects != expected:
            raise StorageCorruption("malformed municipal route schema")
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def _command_fingerprint(revision: RouteRevision) -> str:
    payload = asdict(revision)
    del payload["changed_at"]
    payload["valid_until"] = (
        None if revision.valid_until is None else revision.valid_until.isoformat()
    )
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_revision(row: tuple[Any, ...]) -> RouteRevision:
    try:
        (
            issuer,
            stage,
            mail_class,
            revision,
            status,
            address,
            url,
            quote,
            evidence,
            expiry,
            attempted_at,
            command_id,
            source_text,
            digest,
            expected,
            observed,
        ) = row
        route = RouteRevision(
            RouteScope(issuer, stage, mail_class),
            revision,
            status,
            address,
            url,
            quote,
            evidence,
            None if expiry is None else _load_time(expiry),
            _load_time(attempted_at),
            command_id,
            source_text,
        )
        if (
            route.scope.stage != stage
            or (expected, observed) != (route.revision - 1, route.revision - 1)
            or digest != _command_fingerprint(route)
            or (route.status == "verified" and not route.verified_at(route.changed_at))
        ):
            raise ValueError("route revision does not match its command receipt")
        return route
    except (TypeError, ValueError, OverflowError) as exc:
        raise StorageCorruption("invalid stored municipal route revision") from exc


def _scope_key(scope: RouteScope) -> tuple[str, str, str]:
    if not isinstance(scope, RouteScope):
        raise ValueError("invalid route scope")
    return scope.issuer, scope.stage, scope.mail_class


def _current(database: sqlite3.Connection, scope: RouteScope) -> RouteRevision | None:
    row = database.execute(
        _SELECT_REVISION + f"WHERE {_SCOPE_QUERY} ORDER BY r.revision DESC LIMIT 1",
        _scope_key(scope),
    ).fetchone()
    return None if row is None else _read_revision(row)


def _apply_command(
    database: sqlite3.Connection, candidate: RouteRevision
) -> tuple[RouteRevision | None, int]:
    expected = candidate.revision - 1
    digest = _command_fingerprint(candidate)
    receipt = database.execute(
        "SELECT request_sha256, expected_revision, observed_revision "
        "FROM pb_municipal_route_commands WHERE command_id=?",
        (candidate.command_id,),
    ).fetchone()
    if receipt is not None:
        recorded_digest, recorded_expected, recorded_observed = receipt
        try:
            _check_sha256(recorded_digest)
            observed = _revision_number(recorded_observed, minimum=0)
            _revision_number(recorded_expected, minimum=0)
        except ValueError as exc:
            raise StorageCorruption("invalid municipal route command receipt") from exc
        if recorded_digest != digest:
            raise RouteConflict("route command ID is already in use")
        if recorded_expected != expected:
            raise StorageCorruption("route command disagrees with its recorded revision")
        if observed != expected:
            return None, observed
        row = database.execute(
            _SELECT_REVISION + "WHERE r.command_id=?", (candidate.command_id,)
        ).fetchone()
        if row is None:
            raise StorageCorruption("route command receipt is missing its revision")
        return _read_revision(row), observed

    current = _current(database, candidate.scope)
    observed = 0 if current is None else current.revision
    if observed == expected:
        if current is not None and candidate.changed_at < current.changed_at:
            raise ValueError("route change time precedes the current revision")
        if candidate.status == "verified" and not candidate.verified_at(candidate.changed_at):
            raise ValueError("route verification expiry must follow the change time")
    database.execute(
        "INSERT INTO pb_municipal_route_commands VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            candidate.command_id,
            *_scope_key(candidate.scope),
            digest,
            expected,
            observed,
            candidate.changed_at.isoformat(),
        ),
    )
    if observed != expected:
        return None, observed
    database.execute(
        "INSERT INTO pb_municipal_route_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            *_scope_key(candidate.scope),
            candidate.revision,
            candidate.command_id,
            candidate.status,
            candidate.address_fingerprint,
            candidate.source_url,
            candidate.source_quote,
            candidate.evidence_sha256,
            None if candidate.valid_until is None else candidate.valid_until.isoformat(),
            candidate.source_text_sha256,
        ),
    )
    return candidate, observed


def _attempt_key(case_token: str, attempt: int) -> tuple[str, int]:
    if type(attempt) is not int or attempt not in (1, 2):
        raise ValueError("invalid mail attempt number")
    return _key(case_token, "case token"), attempt


def _bound(
    database: sqlite3.Connection, case_key: tuple[str, int]
) -> tuple[RouteScope, int] | None:
    row = database.execute(
        "SELECT b.issuer, b.stage, b.mail_class, b.route_revision, r.revision, a.case_token "
        "FROM pb_mail_route_bindings b "
        "LEFT JOIN pb_municipal_route_revisions r "
        "ON (b.issuer, b.stage, b.mail_class, b.route_revision)="
        "(r.issuer, r.stage, r.mail_class, r.revision) "
        "LEFT JOIN pb_mail_attempts a ON (b.case_token, b.attempt)=(a.case_token, a.attempt) "
        "WHERE b.case_token=? AND b.attempt=?",
        case_key,
    ).fetchone()
    if row is None:
        return None
    issuer, stage, mail_class, stored_revision, known_revision, known_case = row
    try:
        scope = RouteScope(issuer, stage, mail_class)
        if stage != scope.stage or stored_revision != known_revision or known_case != case_key[0]:
            raise ValueError("route binding referents are invalid")
        revision = 0 if stored_revision is None else _revision_number(stored_revision, minimum=1)
        return scope, revision
    except (TypeError, ValueError, OverflowError) as exc:
        raise StorageCorruption("invalid frozen route binding") from exc


class RouteRegistryMixin:
    def current_route(self: _Connected, scope: RouteScope) -> RouteRevision | None:
        return _current(self._require_open(), scope)

    def monitored_routes(self: _Connected) -> tuple[RouteRevision, ...]:
        rows = (
            self._require_open()
            .execute(
                _SELECT_REVISION + f"WHERE {_LATEST_QUERY} ORDER BY r.issuer, r.stage, r.mail_class"
            )
            .fetchall()
        )
        return tuple(_read_revision(row) for row in rows)

    def withdrawn_address(self: _Connected, fingerprint: str, now: datetime) -> bool:
        """Whether a previously verified fingerprint lost authority in any enrolled scope."""
        _check_sha256(fingerprint)
        now = _time(now)
        rows = self._require_open().execute(
            _SELECT_REVISION
            + f"""WHERE (r.issuer, r.stage, r.mail_class) IN (
                SELECT issuer, stage, mail_class FROM pb_municipal_route_revisions
                WHERE address_fingerprint=? AND status='verified'
            ) AND {_LATEST_QUERY}""",
            (fingerprint,),
        )
        return any(
            route.address_fingerprint != fingerprint or not self.route_check_current(route, now)
            for route in map(_read_revision, rows)
        )

    def change_route(
        self: _Connected,
        *,
        scope: RouteScope,
        expected_revision: int,
        command_id: str,
        status: str,
        address_fingerprint: str | None,
        source_url: str | None,
        source_quote: str | None,
        evidence_sha256: str | None,
        valid_until: datetime | None,
        now: datetime,
        source_text_sha256: str | None = None,
    ) -> RouteRevision:
        """Apply a scoped command with an optimistic comparison and durable replay."""
        candidate = RouteRevision(
            scope,
            _revision_number(expected_revision, minimum=0) + 1,
            status,
            address_fingerprint,
            source_url,
            source_quote,
            evidence_sha256,
            valid_until,
            now,
            command_id,
            source_text_sha256,
        )
        database = self._require_open()
        if database.in_transaction:
            raise StorageCorruption("route commands need their own transaction")
        with self.submission_ownership():
            database.execute("BEGIN IMMEDIATE")
            try:
                route, observed = _apply_command(database, candidate)
                database.execute("COMMIT")
            except Exception:
                if database.in_transaction:
                    rollback_preserving_error(database)
                raise
        if route is None:
            raise RouteConflict(
                f"route revision changed: expected {expected_revision}, saw {observed}"
            )
        return route

    def bind_route(
        self: _Connected, case_token: str, attempt: int, scope: RouteScope, revision: int
    ) -> None:
        """Record the observed revision in the same transaction that freezes the attempt."""
        key = _attempt_key(case_token, attempt)
        scope_key = _scope_key(scope)
        _revision_number(revision, minimum=0)
        database = self._require_open()
        if not database.in_transaction:
            raise StorageCorruption("route bindings need the mail freeze transaction")
        existing = _bound(database, key)
        if existing is not None:
            if existing != (scope, revision):
                raise RouteConflict("mail attempt is bound to a different route revision")
            return
        current = _current(database, scope)
        known_revision = 0 if current is None else current.revision
        if revision != known_revision:
            raise RouteConflict("route changed before the mail attempt froze")
        database.execute(
            "INSERT INTO pb_mail_route_bindings VALUES (?, ?, ?, ?, ?, ?)",
            (*key, *scope_key, revision or None),
        )

    def bound_route(
        self: _Connected, case_token: str, attempt: int
    ) -> tuple[RouteScope, int] | None:
        return _bound(self._require_open(), _attempt_key(case_token, attempt))
