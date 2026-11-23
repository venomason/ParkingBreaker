"""Durable identity-link and browser-session records on the transaction SQLite connection."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error

_IDENTITY_DDL = (
    """CREATE TABLE pb_identity_accounts (
        customer_id TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        created_at TEXT NOT NULL,
        verified_at TEXT
    )""",
    """CREATE TABLE pb_identity_links (
        token_digest TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL REFERENCES pb_identity_accounts(customer_id),
        issued_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        CHECK (expires_at > issued_at),
        CHECK (consumed_at IS NULL OR consumed_at >= issued_at)
    )""",
    """CREATE TABLE pb_identity_sessions (
        session_digest TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL REFERENCES pb_identity_accounts(customer_id),
        csrf_digest TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT,
        CHECK (expires_at > created_at),
        CHECK (revoked_at IS NULL OR revoked_at >= created_at)
    )""",
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class IdentityConflict(ValueError):
    """A refused identity operation; never carries an email, ID, or token."""


@dataclass(frozen=True)
class IdentityAccount:
    customer_id: str
    email: str
    created_at: datetime
    verified_at: datetime | None


@dataclass(frozen=True)
class IdentitySession:
    session_digest: str
    customer_id: str
    csrf_digest: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


def initialize_identity(database: sqlite3.Connection) -> None:
    """Validate identity schema and migrate v1 email accounts to v2 appeal scopes."""
    # Rebuild only the parent under SQLite's documented table-rebuild procedure.
    # Foreign keys are restored even on failure; all old DDL is verified first.
    foreign_keys = database.execute("PRAGMA foreign_keys").fetchone()[0]
    database.execute("PRAGMA foreign_keys=OFF")
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='identity'"
        ).fetchone()
        tables = {
            row[0]: row[1]
            for row in database.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
            if row[0].startswith("pb_identity_")
        }
        if version is None:
            if tables:
                raise StorageCorruption("unversioned identity schema")
            for ddl in _IDENTITY_DDL:
                database.execute(ddl)
            database.execute("INSERT INTO pb_store_versions VALUES ('identity', 2)")
        elif type(version[0]) is not int or version[0] not in {1, 2}:
            raise StorageCorruption("unsupported identity schema")
        else:
            expected = {ddl.split()[2]: " ".join(ddl.upper().split()) for ddl in _IDENTITY_DDL}
            actual = {
                name: " ".join(sql.upper().split()).replace(
                    'TABLE "PB_IDENTITY_ACCOUNTS"', "TABLE PB_IDENTITY_ACCOUNTS"
                )
                for name, sql in tables.items()
            }
            if version[0] == 1:
                expected["pb_identity_accounts"] = expected["pb_identity_accounts"].replace(
                    "EMAIL TEXT NOT NULL,", "EMAIL TEXT NOT NULL UNIQUE,"
                )
            if actual != expected:
                raise StorageCorruption("malformed identity schema")
            if version[0] == 1:
                database.execute(
                    _IDENTITY_DDL[0].replace("pb_identity_accounts", "pb_identity_accounts_new", 1)
                )
                database.execute(
                    "INSERT INTO pb_identity_accounts_new SELECT * FROM pb_identity_accounts"
                )
                database.execute("DROP TABLE pb_identity_accounts")
                database.execute(
                    "ALTER TABLE pb_identity_accounts_new RENAME TO pb_identity_accounts"
                )
                database.execute(
                    "UPDATE pb_store_versions SET version=2 WHERE component='identity'"
                )
        if database.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StorageCorruption("identity migration foreign key failure")
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise
    finally:
        database.execute(f"PRAGMA foreign_keys={int(foreign_keys)}")


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


class IdentityStoreMixin:
    """Identity operations using the same SQLite connection as workspaces and cases."""

    def issue_appeal_start(
        self: _Connected,
        *,
        scope_id: str,
        link_digest: str,
        now: datetime,
        expires_at: datetime,
        pricing_digest: str | None = None,
    ) -> None:
        scope_id, link_digest = _customer_id(scope_id), _digest(link_digest)
        now, expires_at = _utc(now), _utc(expires_at)
        if expires_at <= now:
            raise IdentityConflict("invalid identity input")
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                "INSERT INTO pb_identity_accounts VALUES (?, '', ?, NULL)", (scope_id, _stamp(now))
            )
            database.execute(
                "INSERT INTO pb_identity_links VALUES (?, ?, ?, ?, NULL)",
                (link_digest, scope_id, _stamp(now), _stamp(expires_at)),
            )
            if pricing_digest is not None:
                from parkingbreaker.pricing_store import bind_scope_pricing

                bind_scope_pricing(database, scope_id, pricing_digest)
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def issue_identity_link(
        self: _Connected,
        *,
        customer_id: str,
        email: str,
        token_digest: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> IdentityAccount:
        customer_id = _customer_id(customer_id)
        email = _canonical_email(email)
        token_digest = _digest(token_digest)
        issued_at = _utc(issued_at)
        expires_at = _utc(expires_at)
        if expires_at <= issued_at:
            raise IdentityConflict("invalid identity input")

        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            account = _insert_identity_link_rows(
                database,
                customer_id=customer_id,
                email=email,
                token_digest=token_digest,
                issued_at=issued_at,
                expires_at=expires_at,
            )
            database.execute("COMMIT")
            return account
        except sqlite3.IntegrityError as exc:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise IdentityConflict("identity conflict") from exc
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def consume_identity_link(
        self: _Connected,
        token_digest: str,
        *,
        session_digest: str,
        csrf_digest: str,
        now: datetime,
        session_expires_at: datetime,
    ) -> IdentitySession | None:
        token_digest = _digest(token_digest)
        session_digest = _digest(session_digest)
        csrf_digest = _digest(csrf_digest)
        now = _utc(now)
        session_expires_at = _utc(session_expires_at)
        if session_expires_at <= now:
            raise IdentityConflict("invalid identity input")

        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT token_digest, customer_id, issued_at, expires_at, consumed_at "
                "FROM pb_identity_links WHERE token_digest=?",
                (token_digest,),
            ).fetchone()
            if row is None:
                database.execute("COMMIT")
                return None
            link = _link(row)
            if link[4] is not None or now < link[2] or now >= link[3]:
                database.execute("COMMIT")
                return None
            if unlineaged_workspace_roots(database, link[1]) > 1:
                # Never consume a historical account-wide credential or broaden it.
                database.execute("COMMIT")
                return None
            account_row = database.execute(
                "SELECT customer_id, email, created_at, verified_at "
                "FROM pb_identity_accounts WHERE customer_id=?",
                (link[1],),
            ).fetchone()
            if account_row is None:
                raise StorageCorruption("identity link account is missing")
            account = _account(account_row)
            if link[2] < account.created_at or now < account.created_at:
                raise StorageCorruption("identity link predates its account")
            if account.verified_at is not None and account.verified_at > now:
                raise StorageCorruption("identity account verification is in the future")
            database.execute(
                "UPDATE pb_identity_accounts SET verified_at=COALESCE(verified_at, ?) "
                "WHERE customer_id=?",
                (_stamp(now), account.customer_id),
            )
            database.execute(
                "INSERT INTO pb_identity_sessions VALUES (?, ?, ?, ?, ?, NULL)",
                (
                    session_digest,
                    account.customer_id,
                    csrf_digest,
                    _stamp(now),
                    _stamp(session_expires_at),
                ),
            )
            changed = database.execute(
                "UPDATE pb_identity_links SET consumed_at=? "
                "WHERE token_digest=? AND consumed_at IS NULL",
                (_stamp(now), token_digest),
            ).rowcount
            if changed != 1:
                raise StorageCorruption("identity link consumption was not atomic")
            database.execute("COMMIT")
            return IdentitySession(
                session_digest,
                account.customer_id,
                csrf_digest,
                now,
                session_expires_at,
                None,
            )
        except sqlite3.IntegrityError as exc:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise IdentityConflict("identity conflict") from exc
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def identity_session(
        self: _Connected, session_digest: str, *, now: datetime
    ) -> IdentitySession | None:
        session_digest = _digest(session_digest)
        now = _utc(now)
        database = self._require_open()
        row = database.execute(
            "SELECT s.session_digest, s.customer_id, s.csrf_digest, s.created_at, "
            "s.expires_at, s.revoked_at, a.customer_id, a.email, a.created_at, a.verified_at "
            "FROM pb_identity_sessions s JOIN pb_identity_accounts a "
            "ON a.customer_id=s.customer_id WHERE s.session_digest=?",
            (session_digest,),
        ).fetchone()
        if row is None:
            return None
        session = _session(row[:6])
        account = _account(row[6:10])
        if (
            account.customer_id != session.customer_id
            or account.verified_at is None
            or account.verified_at < account.created_at
            or account.verified_at > session.created_at
            or session.created_at < account.created_at
            or now < session.created_at
            or session.revoked_at is not None
            or session.expires_at <= now
        ):
            return None
        # A restart chain keeps exactly one unlineaged root, so a legitimate
        # restart does not invalidate the session.
        if unlineaged_workspace_roots(database, session.customer_id) > 1:
            return None
        return session

    def revoke_identity_session(self: _Connected, session_digest: str, *, now: datetime) -> None:
        session_digest = _digest(session_digest)
        now = _utc(now)
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT session_digest, customer_id, csrf_digest, created_at, expires_at, "
                "revoked_at "
                "FROM pb_identity_sessions WHERE session_digest=?",
                (session_digest,),
            ).fetchone()
            if row is not None:
                session = _session(row)
                if now < session.created_at:
                    raise IdentityConflict("invalid identity input")
                if session.revoked_at is None:
                    database.execute(
                        "UPDATE pb_identity_sessions SET revoked_at=? "
                        "WHERE session_digest=? AND revoked_at IS NULL",
                        (_stamp(now), session_digest),
                    )
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise


def _insert_identity_link_rows(
    database: sqlite3.Connection,
    *,
    customer_id: str,
    email: str,
    token_digest: str,
    issued_at: datetime,
    expires_at: datetime,
) -> IdentityAccount:
    """Insert an appeal scope or reissue its link; email never selects ownership."""
    by_id = database.execute(
        "SELECT customer_id, email, created_at, verified_at "
        "FROM pb_identity_accounts WHERE customer_id=?",
        (customer_id,),
    ).fetchone()
    if by_id is not None and by_id[1] == "":
        database.execute(
            "UPDATE pb_identity_accounts SET email=? WHERE customer_id=?", (email, customer_id)
        )
        by_id = (by_id[0], email, by_id[2], by_id[3])
    if by_id is not None and by_id[1] != email:
        raise IdentityConflict("identity conflict")
    if by_id is None:
        database.execute(
            "INSERT INTO pb_identity_accounts VALUES (?, ?, ?, NULL)",
            (customer_id, email, _stamp(issued_at)),
        )
        account_row: tuple[object, ...] = (customer_id, email, _stamp(issued_at), None)
    else:
        account_row = by_id
    account = _account(account_row)
    if issued_at < account.created_at:
        raise IdentityConflict("invalid identity input")
    database.execute(
        "INSERT INTO pb_identity_links VALUES (?, ?, ?, ?, NULL)",
        (token_digest, account.customer_id, _stamp(issued_at), _stamp(expires_at)),
    )
    return account


def _customer_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 128
        or any(unicodedata.category(char) == "Cc" for char in value)
    ):
        raise IdentityConflict("invalid identity input")
    return value


def _canonical_email(value: str) -> str:
    if not isinstance(value, str):
        raise IdentityConflict("invalid identity input")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise IdentityConflict("invalid identity input")
    email = value.strip()
    if (
        not email
        or len(email) > 254
        or any(unicodedata.category(char) == "Cc" or char.isspace() for char in email)
        or email.count("@") != 1
    ):
        raise IdentityConflict("invalid identity input")
    local, domain = email.split("@")
    labels = domain.split(".")
    if (
        not local
        or len(local) > 64
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or len(labels) < 2
        or any(not label or label.startswith("-") or label.endswith("-") for label in labels)
    ):
        raise IdentityConflict("invalid identity input")
    return f"{local}@{domain.lower()}"


def _digest(value: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise IdentityConflict("invalid identity input")
    return value


def unlineaged_workspace_roots(database: sqlite3.Connection, owner: str) -> int:
    """Count this owner's workspaces that no restart lineage points at.

    Legacy accounts could own several unlinked workspaces before identity
    existed; that ambiguity must never be consumed or expanded. A legitimate
    assisted restart records parent lineage, so a restart chain still has
    exactly one root and the account is not ambiguous.
    """
    row = database.execute(
        "SELECT count(*) FROM pb_intake_owners o WHERE o.owner=? "
        "AND NOT EXISTS (SELECT 1 FROM pb_workspace_lineage l "
        "JOIN pb_intake_owners parent ON parent.workspace_token=l.parent_workspace "
        "WHERE l.child_workspace=o.workspace_token AND parent.owner=o.owner)",
        (owner,),
    ).fetchone()
    return 0 if row is None else int(row[0])


def _utc(value: datetime) -> datetime:
    try:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise IdentityConflict("invalid identity input")
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise IdentityConflict("invalid identity input") from exc


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _stored_time(value: object, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise StorageCorruption("malformed identity timestamp")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        normalized = parsed.astimezone(UTC)
        if _stamp(normalized) != value:
            raise ValueError
        return normalized
    except (ValueError, OverflowError) as exc:
        raise StorageCorruption("malformed identity timestamp") from exc


def _account(row: tuple[object, ...]) -> IdentityAccount:
    customer_id, email, created_at, verified_at = row
    if not isinstance(customer_id, str) or not isinstance(email, str):
        raise StorageCorruption("malformed identity account")
    if _customer_id_from_storage(customer_id) is None or (
        email != "" and _canonical_email_from_storage(email) is None
    ):
        raise StorageCorruption("malformed identity account")
    created = _stored_time(created_at)
    verified = _stored_time(verified_at, optional=True)
    assert created is not None
    if verified is not None and verified < created:
        raise StorageCorruption("malformed identity account")
    return IdentityAccount(customer_id, email, created, verified)


def _customer_id_from_storage(value: str) -> str | None:
    try:
        return _customer_id(value)
    except IdentityConflict:
        return None


def _canonical_email_from_storage(value: str) -> str | None:
    try:
        return value if _canonical_email(value) == value else None
    except IdentityConflict:
        return None


def _link(row: tuple[object, ...]) -> tuple[str, str, datetime, datetime, datetime | None]:
    token_digest, customer_id, issued_at, expires_at, consumed_at = row
    if not isinstance(token_digest, str) or _DIGEST.fullmatch(token_digest) is None:
        raise StorageCorruption("malformed identity link")
    if not isinstance(customer_id, str) or _customer_id_from_storage(customer_id) is None:
        raise StorageCorruption("malformed identity link")
    issued = _stored_time(issued_at)
    expires = _stored_time(expires_at)
    consumed = _stored_time(consumed_at, optional=True)
    assert issued is not None
    assert expires is not None
    if expires <= issued or (consumed is not None and consumed < issued):
        raise StorageCorruption("malformed identity link")
    return token_digest, customer_id, issued, expires, consumed


def _session(row: tuple[object, ...]) -> IdentitySession:
    session_digest, customer_id, csrf_digest, created_at, expires_at, revoked_at = row
    if (
        not isinstance(session_digest, str)
        or _DIGEST.fullmatch(session_digest) is None
        or not isinstance(customer_id, str)
        or _customer_id_from_storage(customer_id) is None
        or not isinstance(csrf_digest, str)
        or _DIGEST.fullmatch(csrf_digest) is None
    ):
        raise StorageCorruption("malformed identity session")
    created = _stored_time(created_at)
    expires = _stored_time(expires_at)
    revoked = _stored_time(revoked_at, optional=True)
    assert created is not None
    assert expires is not None
    if expires <= created or (revoked is not None and revoked < created):
        raise StorageCorruption("malformed identity session")
    return IdentitySession(session_digest, customer_id, csrf_digest, created, expires, revoked)
