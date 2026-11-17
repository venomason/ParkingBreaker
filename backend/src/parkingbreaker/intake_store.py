"""Pre-hold intake records in the transaction database (Lane C C3).

The API's case id names an intake before any hold exists; the store's case row still
starts at authorization (``start_case``). Intake and pre-hold payloads are stored as the
API's own canonical JSON and are immutable once written; only the chosen tier and the
started case token change, each under a revision check.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol

from parkingbreaker.journey_events import (
    CostStatus,
    JourneyEventInput,
    JourneyEventKind,
    JourneyEventOutcome,
    append_journey_event_in_transaction,
)
from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.payment_records import mutation_key
from parkingbreaker.service_phase import ServicePhase
from parkingbreaker.transaction_records import WorkspaceRecord
from parkingbreaker.workspace import Workspace

_BASE_INTAKE_DDL = (
    """CREATE TABLE pb_intake_owners (
        workspace_token TEXT PRIMARY KEY REFERENCES pb_workspaces(workspace_token),
        owner TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE pb_intake_records (
        intake_id TEXT PRIMARY KEY,
        workspace_token TEXT NOT NULL UNIQUE REFERENCES pb_intake_owners(workspace_token),
        intake_json TEXT NOT NULL,
        prehold_json TEXT NOT NULL,
        agency_json TEXT,
        selected_tier TEXT,
        case_token TEXT UNIQUE,
        created_at TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1)
    )""",
    """CREATE TABLE pb_intake_drafts (
        intake_id TEXT PRIMARY KEY REFERENCES pb_intake_records(intake_id),
        letter_text TEXT NOT NULL,
        spans_json TEXT NOT NULL,
        flags_json TEXT NOT NULL,
        adopted_sha256 TEXT,
        adopted_at TEXT,
        adopted_by TEXT,
        packet_sha256 TEXT,
        updated_at TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1)
    )""",
    """CREATE TABLE pb_intake_research (
        intake_id TEXT PRIMARY KEY REFERENCES pb_intake_records(intake_id),
        research_json TEXT NOT NULL,
        answers_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1)
    )""",
)

_AUTHORIZATION_ATTEMPTS_DDL = """CREATE TABLE pb_intake_authorization_attempts (
    intake_id TEXT NOT NULL REFERENCES pb_intake_records(intake_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    idempotency_key TEXT PRIMARY KEY,
    tier TEXT NOT NULL CHECK (tier IN ('CERTIFIED', 'STANDARD')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    payment_method_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'uncertain', 'declined', 'authorized')),
    authorization_ref TEXT,
    authorized_at TEXT,
    authorization_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    UNIQUE (intake_id, attempt_number),
    CHECK ((state = 'authorized'
            AND authorization_ref IS NOT NULL
            AND authorized_at IS NOT NULL
            AND authorization_expires_at IS NOT NULL)
        OR (state != 'authorized'
            AND authorized_at IS NULL
            AND authorization_expires_at IS NULL))
)"""

_INTAKE_DDL = (*_BASE_INTAKE_DDL, _AUTHORIZATION_ATTEMPTS_DDL)


class IntakeConflict(ValueError):
    """Stable reason key only; never customer content."""


@dataclass(frozen=True)
class IntakeRecord:
    intake_id: str
    workspace_token: str
    owner: str
    intake_json: str
    prehold_json: str
    agency_json: str | None
    selected_tier: str | None
    case_token: str | None
    created_at: datetime
    revision: int


@dataclass(frozen=True)
class SavedResearch:
    research_json: str
    answers_json: str
    updated_at: datetime
    revision: int


@dataclass(frozen=True)
class SavedDraft:
    letter_text: str
    spans_json: str
    flags_json: str
    adopted_sha256: str | None
    adopted_at: datetime | None
    adopted_by: str | None
    packet_sha256: str | None
    updated_at: datetime
    revision: int


@dataclass(frozen=True)
class AuthorizationAttempt:
    intake_id: str
    attempt_number: int
    idempotency_key: str
    tier: str
    amount_cents: int
    payment_method_id: str
    request_json: str
    state: str
    authorization_ref: str | None
    authorized_at: datetime | None
    authorization_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    revision: int


def authorization_dispatch_refusal(
    attempt: AuthorizationAttempt, now: datetime, replay_limit: timedelta
) -> str | None:
    """Decide whether the current attempt can safely reach the provider."""
    if (
        now < attempt.created_at
        or now < attempt.updated_at
        or (attempt.authorization_ref is None and now - attempt.created_at >= replay_limit)
    ):
        return "authorization_outcome_unresolved"
    if attempt.state == "declined":
        return "card_not_authorized"
    return None


def _refuse_beta_payment(database: sqlite3.Connection, intake_id: str) -> None:
    if (
        database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pb_beta_entitlements'"
        ).fetchone()
        and database.execute(
            "SELECT 1 FROM pb_beta_entitlements WHERE intake_id=?", (intake_id,)
        ).fetchone()
    ):
        raise IntakeConflict("beta_no_payment_required")


def _require_tz_aware(dt: datetime) -> None:
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")


def _validate_authorization_identity(
    intake_id: str,
    key: str,
    tier: str,
    amount_cents: int,
    payment_method_id: str,
    request_json: str,
) -> None:
    if not (
        isinstance(intake_id, str)
        and bool(intake_id.strip())
        and isinstance(tier, str)
        and tier in {"CERTIFIED", "STANDARD"}
        and type(amount_cents) is int
        and amount_cents > 0
        and isinstance(payment_method_id, str)
        and payment_method_id.startswith("pm_")
        and len(payment_method_id) > 3
        and isinstance(key, str)
        and key
        == mutation_key("authorization", intake_id, f"{tier}:{amount_cents}", payment_method_id)
        and isinstance(request_json, str)
    ):
        raise ValueError("invalid authorization identity")

    expected = {
        "amount_cents": amount_cents,
        "currency": "usd",
        "metadata": {
            "case": intake_id,
            "tier": tier,
            "service_amount_cents": str(amount_cents),
        },
        "payment_method_id": payment_method_id,
        "tier": tier,
    }
    try:
        request = json.loads(request_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid authorization identity") from exc

    if (
        not isinstance(request, dict)
        or request != expected
        or type(request.get("amount_cents")) is not int
        or request_json
        != json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    ):
        raise ValueError("invalid authorization identity")


def initialize_intake(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='intake'"
        ).fetchone()
        tables = {
            r[0]: r[1]
            for r in database.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND name LIKE 'pb_intake_%'"
            )
        }
        base_expected = {ddl.split()[2]: " ".join(ddl.upper().split()) for ddl in _BASE_INTAKE_DDL}
        expected = {ddl.split()[2]: " ".join(ddl.upper().split()) for ddl in _INTAKE_DDL}
        actual = {name: " ".join(sql.upper().split()) for name, sql in tables.items()}
        if version is None:
            if tables:
                raise StorageCorruption("unversioned intake schema")
            for ddl in _INTAKE_DDL:
                database.execute(ddl)
            database.execute("INSERT INTO pb_store_versions VALUES ('intake', 2)")
        elif type(version[0]) is not int or version[0] not in (1, 2):
            raise StorageCorruption("unsupported intake schema")
        elif version[0] == 1:
            if actual != base_expected:
                raise StorageCorruption("malformed intake schema")
            unlinked_intake = database.execute(
                "SELECT 1 FROM pb_intake_records "
                "WHERE selected_tier IS NOT NULL AND case_token IS NULL LIMIT 1"
            ).fetchone()
            has_cases = database.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pb_cases'"
            ).fetchone()
            unlinked_case = (
                has_cases is not None
                and database.execute(
                    "SELECT 1 FROM pb_cases c WHERE NOT EXISTS "
                    "(SELECT 1 FROM pb_intake_records r WHERE r.case_token=c.case_token) LIMIT 1"
                ).fetchone()
                is not None
            )
            if unlinked_intake is not None or unlinked_case:
                # Legacy cards/keys are unknown: refuse a replacement hold until an
                # operator reconciles the authorization through provider records.
                raise StorageCorruption("legacy authorization unreconciled")
            database.execute(_AUTHORIZATION_ATTEMPTS_DDL)
            database.execute("UPDATE pb_store_versions SET version=2 WHERE component='intake'")
        else:
            if actual != expected:
                raise StorageCorruption("malformed intake schema")
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


_SELECT = (
    "SELECT r.intake_id, r.workspace_token, o.owner, r.intake_json, r.prehold_json, "
    "r.agency_json, r.selected_tier, r.case_token, r.created_at, r.revision "
    "FROM pb_intake_records r "
    "JOIN pb_intake_owners o ON o.workspace_token = r.workspace_token "
)

_AUTHORIZATION_SELECT = (
    "SELECT intake_id, attempt_number, idempotency_key, tier, amount_cents, payment_method_id, "
    "request_json, state, authorization_ref, authorized_at, authorization_expires_at, "
    "created_at, updated_at, revision FROM pb_intake_authorization_attempts "
)


def _record(row: tuple[object, ...]) -> IntakeRecord:
    (intake_id, token, owner, intake, prehold, agency, tier, case, created, revision) = row
    if not (
        isinstance(intake_id, str)
        and isinstance(token, str)
        and isinstance(owner, str)
        and isinstance(intake, str)
        and isinstance(prehold, str)
        and (agency is None or isinstance(agency, str))
        and (tier is None or isinstance(tier, str))
        and (case is None or isinstance(case, str))
        and isinstance(created, str)
        and type(revision) is int
    ):
        raise StorageCorruption("malformed intake record")
    return IntakeRecord(
        intake_id,
        token,
        owner,
        intake,
        prehold,
        agency,
        tier,
        case,
        datetime.fromisoformat(created),
        revision,
    )


def _authorization_attempt(row: tuple[object, ...]) -> AuthorizationAttempt:
    (
        intake_id,
        attempt_number,
        key,
        tier,
        amount_cents,
        payment_method_id,
        request_json,
        state,
        authorization_ref,
        authorized_text,
        expiry_text,
        created_text,
        updated_text,
        revision,
    ) = row
    try:
        if not (
            isinstance(intake_id, str)
            and isinstance(key, str)
            and isinstance(tier, str)
            and type(amount_cents) is int
            and isinstance(payment_method_id, str)
            and isinstance(request_json, str)
            and type(attempt_number) is int
            and attempt_number >= 1
            and isinstance(state, str)
            and state in {"pending", "uncertain", "declined", "authorized"}
            and (authorization_ref is None or isinstance(authorization_ref, str))
            and (authorized_text is None or isinstance(authorized_text, str))
            and (expiry_text is None or isinstance(expiry_text, str))
            and isinstance(created_text, str)
            and isinstance(updated_text, str)
            and type(revision) is int
            and revision >= 1
        ):
            raise ValueError("invalid fields")
        _validate_authorization_identity(
            intake_id,
            key,
            tier,
            amount_cents,
            payment_method_id,
            request_json,
        )
        created_at = datetime.fromisoformat(created_text)
        updated_at = datetime.fromisoformat(updated_text)
        _require_tz_aware(created_at)
        _require_tz_aware(updated_at)
        if updated_at < created_at:
            raise ValueError("attempt timestamp moved backwards")
        if state == "authorized":
            if not authorization_ref or not authorized_text or not expiry_text:
                raise ValueError("incomplete authorized attempt")
            authorized_at = datetime.fromisoformat(authorized_text)
            expires_at = datetime.fromisoformat(expiry_text)
            _require_tz_aware(authorized_at)
            _require_tz_aware(expires_at)
            if authorized_at < created_at:
                raise ValueError("authorized before created")
            if expires_at <= authorized_at:
                raise ValueError("invalid authorization interval")
        else:
            if authorized_text is not None or expiry_text is not None:
                raise ValueError("unexpected authorization interval")
            authorized_at = None
            expires_at = None
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StorageCorruption("malformed authorization attempt") from exc
    return AuthorizationAttempt(
        intake_id,
        attempt_number,
        key,
        tier,
        amount_cents,
        payment_method_id,
        request_json,
        state,
        authorization_ref,
        authorized_at,
        expires_at,
        created_at,
        updated_at,
        revision,
    )


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...

    def _runtime_service_phase_in_transaction(
        self, database: sqlite3.Connection
    ) -> ServicePhase | None: ...

    def _save_workspace_in_transaction(
        self,
        workspace_token: str,
        workspace: Workspace,
        now: datetime,
        expected_revision: int | None,
    ) -> WorkspaceRecord: ...

    def get_workspace(self, workspace_token: str) -> WorkspaceRecord | None: ...


class IntakeStoreMixin:
    """Intake methods of SqliteTransactionStore; uses its single connection."""

    def set_workspace_owner(
        self: _Connected, workspace_token: str, owner: str, now: datetime
    ) -> None:
        if not owner.strip():
            raise ValueError("owner must not be blank")
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT owner FROM pb_intake_owners WHERE workspace_token=?", (workspace_token,)
            ).fetchone()
            if row is None:
                database.execute(
                    "INSERT INTO pb_intake_owners VALUES (?, ?, ?)",
                    (workspace_token, owner, now.isoformat()),
                )
            elif row[0] != owner:
                raise IntakeConflict("workspace_owned_by_another_customer")
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def workspace_owner(self: _Connected, workspace_token: str) -> str | None:
        row = (
            self._require_open()
            .execute(
                "SELECT owner FROM pb_intake_owners WHERE workspace_token=?", (workspace_token,)
            )
            .fetchone()
        )
        return None if row is None else str(row[0])

    def record_intake(
        self: _Connected,
        intake_id: str,
        workspace_token: str,
        intake_json: str,
        prehold_json: str,
        now: datetime,
        agency_json: str | None = None,
    ) -> IntakeRecord:
        """One intake per workspace; an identical replay returns the stored record."""
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            existing = database.execute(
                _SELECT + "WHERE r.workspace_token=?", (workspace_token,)
            ).fetchone()
            if existing is not None:
                record = _record(existing)
                database.execute("COMMIT")
                if record.intake_json != intake_json:
                    raise IntakeConflict("intake_already_submitted_create_new_workspace")
                return record
            database.execute(
                "INSERT INTO pb_intake_records VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, 1)",
                (
                    intake_id,
                    workspace_token,
                    intake_json,
                    prehold_json,
                    agency_json,
                    now.isoformat(),
                ),
            )
            row = database.execute(_SELECT + "WHERE r.intake_id=?", (intake_id,)).fetchone()
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
        return _record(row)

    def record_intake_with_workspace_snapshot(
        self: _Connected,
        intake_id: str,
        workspace_token: str,
        intake_json: str,
        prehold_json: str,
        customer_statement: str,
        photo_refs: tuple[str, ...],
        provision_selections: frozenset[str],
        now: datetime,
        agency_json: str | None = None,
    ) -> IntakeRecord:
        """Commit an intake and its exact customer snapshot as one retryable unit."""
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            owner = database.execute(
                "SELECT owner FROM pb_intake_owners WHERE workspace_token=?", (workspace_token,)
            ).fetchone()
            if owner is None:
                raise IntakeConflict("workspace_not_found")
            existing = database.execute(
                _SELECT + "WHERE r.workspace_token=?", (workspace_token,)
            ).fetchone()
            current_workspace = self.get_workspace(workspace_token)
            if current_workspace is None:
                raise IntakeConflict("workspace_not_found")
            if existing is not None:
                record = _record(existing)
                if record.intake_json != intake_json:
                    raise IntakeConflict("intake_already_submitted_create_new_workspace")
                updated = replace(
                    current_workspace.workspace,
                    position=customer_statement,
                    photo_refs=photo_refs,
                    provision_selections=provision_selections,
                )
                if updated != current_workspace.workspace:
                    self._save_workspace_in_transaction(
                        workspace_token, updated, now, current_workspace.revision
                    )
                database.execute("COMMIT")
                return record

            updated = replace(
                current_workspace.workspace,
                position=customer_statement,
                photo_refs=photo_refs,
                provision_selections=provision_selections,
            )
            self._save_workspace_in_transaction(
                workspace_token, updated, now, current_workspace.revision
            )
            database.execute(
                "INSERT INTO pb_intake_records VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, 1)",
                (
                    intake_id,
                    workspace_token,
                    intake_json,
                    prehold_json,
                    agency_json,
                    now.isoformat(),
                ),
            )
            row = database.execute(_SELECT + "WHERE r.intake_id=?", (intake_id,)).fetchone()
            record = _record(row)
            append_journey_event_in_transaction(
                database,
                workspace_token=record.workspace_token,
                occurrence_key=f"intake_submitted:{record.intake_id}",
                event=JourneyEventInput(
                    kind=JourneyEventKind.INTAKE_SUBMITTED,
                    outcome=JourneyEventOutcome.SUCCEEDED,
                    occurred_at=record.created_at,
                    cost_status=CostStatus.NOT_APPLICABLE,
                ),
            )
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
        return record

    def begin_authorization_attempt(
        self: _Connected,
        intake_id: str,
        idempotency_key: str,
        tier: str,
        amount_cents: int,
        payment_method_id: str,
        request_json: str,
        now: datetime,
        *,
        allow_new_paid_authorization: bool = True,
    ) -> AuthorizationAttempt:
        """Durably reserve one immutable provider request before calling Stripe.

        ``allow_new_paid_authorization`` is the configured fallback for a *new* row. An
        exact existing key with unchanged identity still replays so a pending, uncertain
        or authorized historical intent keeps its original recovery path; a declined row
        likewise does not become a new paid try. A different-key pending/uncertain refusal
        keeps its existing priority. A private runtime override is read inside this write
        transaction and wins over that boolean, so a change committed after the caller
        resolved its phase still blocks a new paid intent. A malformed stored phase raises
        ``StorageCorruption`` instead of permitting a charge. Nothing about claim, record,
        complete, capture, release or reads is gated here.
        """
        _require_tz_aware(now)
        _validate_authorization_identity(
            intake_id,
            idempotency_key,
            tier,
            amount_cents,
            payment_method_id,
            request_json,
        )
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            _refuse_beta_payment(database, intake_id)
            intake_row = database.execute(_SELECT + "WHERE r.intake_id=?", (intake_id,)).fetchone()
            if intake_row is None:
                raise IntakeConflict("intake_not_found")
            record = _record(intake_row)
            if record.case_token is not None:
                raise IntakeConflict("authorization_already_started")
            if record.selected_tier != tier:
                raise IntakeConflict("authorization_tier_changed")
            existing = database.execute(
                _AUTHORIZATION_SELECT + "WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                attempt = _authorization_attempt(existing)
                if (
                    attempt.intake_id != intake_id
                    or attempt.tier != tier
                    or attempt.amount_cents != amount_cents
                    or attempt.payment_method_id != payment_method_id
                    or attempt.request_json != request_json
                ):
                    raise IntakeConflict("authorization_identity_changed")
                database.execute("COMMIT")
                return attempt

            latest = database.execute(
                _AUTHORIZATION_SELECT + "WHERE intake_id=? ORDER BY attempt_number DESC LIMIT 1",
                (intake_id,),
            ).fetchone()
            if latest is not None:
                previous = _authorization_attempt(latest)
                if previous.state in {"pending", "uncertain"}:
                    raise IntakeConflict("authorization_outcome_unresolved")
                if previous.state == "authorized":
                    raise StorageCorruption("authorized attempt has no linked case")
            # Only the creation of a new paid intent is paused; anything above is an
            # existing attempt's identity/recovery decision. Read the committed runtime
            # override here, inside BEGIN IMMEDIATE, so it wins over a stale caller
            # boolean. No override keeps the configured fallback exactly.
            runtime_phase = self._runtime_service_phase_in_transaction(database)
            allow_new_paid = (
                allow_new_paid_authorization
                if runtime_phase is None
                else runtime_phase is ServicePhase.PAID
            )
            if not allow_new_paid:
                raise IntakeConflict("paid_orders_paused")
            attempt_number = int(
                database.execute(
                    "SELECT COALESCE(MAX(attempt_number), 0) + 1 "
                    "FROM pb_intake_authorization_attempts WHERE intake_id=?",
                    (intake_id,),
                ).fetchone()[0]
            )
            database.execute(
                "INSERT INTO pb_intake_authorization_attempts "
                "(intake_id, attempt_number, idempotency_key, tier, amount_cents, "
                "payment_method_id, request_json, state, authorization_ref, authorized_at, "
                "authorization_expires_at, created_at, updated_at, revision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL, ?, ?, 1)",
                (
                    intake_id,
                    attempt_number,
                    idempotency_key,
                    tier,
                    amount_cents,
                    payment_method_id,
                    request_json,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            row = database.execute(
                _AUTHORIZATION_SELECT + "WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
        return _authorization_attempt(row)

    def claim_authorization_dispatch(
        self: _Connected,
        intake_id: str,
        idempotency_key: str,
        now: datetime,
        replay_limit: timedelta,
    ) -> AuthorizationAttempt:
        """Validate the current row and persist a dispatch watermark atomically."""
        _require_tz_aware(now)
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            _refuse_beta_payment(database, intake_id)
            row = database.execute(
                _AUTHORIZATION_SELECT + "WHERE intake_id=? AND idempotency_key=?",
                (intake_id, idempotency_key),
            ).fetchone()
            if row is None:
                raise IntakeConflict("authorization_attempt_not_found")
            current = _authorization_attempt(row)
            if current.state == "authorized":
                database.execute("COMMIT")
                return current
            refusal = authorization_dispatch_refusal(current, now, replay_limit)
            if refusal is not None:
                raise IntakeConflict(refusal)
            cursor = database.execute(
                "UPDATE pb_intake_authorization_attempts SET updated_at=?, revision=revision+1 "
                "WHERE intake_id=? AND idempotency_key=? AND revision=?",
                (now.isoformat(), intake_id, idempotency_key, current.revision),
            )
            if cursor.rowcount != 1:
                raise IntakeConflict("authorization_outcome_unresolved")
            row = database.execute(
                _AUTHORIZATION_SELECT + "WHERE intake_id=? AND idempotency_key=?",
                (intake_id, idempotency_key),
            ).fetchone()
            claimed = _authorization_attempt(row)
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
        return claimed

    def get_authorization_attempt(
        self: _Connected, intake_id: str, idempotency_key: str
    ) -> AuthorizationAttempt | None:
        row = (
            self._require_open()
            .execute(
                _AUTHORIZATION_SELECT + "WHERE intake_id=? AND idempotency_key=?",
                (intake_id, idempotency_key),
            )
            .fetchone()
        )
        return None if row is None else _authorization_attempt(row)

    def has_active_authorization_attempt(self: _Connected, intake_id: str) -> bool:
        """An existing financial intent must retain its original request and recovery path."""
        return (
            self._require_open()
            .execute(
                "SELECT 1 FROM pb_intake_authorization_attempts WHERE intake_id=? "
                "AND state IN ('pending', 'uncertain', 'authorized') LIMIT 1",
                (intake_id,),
            )
            .fetchone()
            is not None
        )

    def record_authorization_outcome(
        self: _Connected,
        intake_id: str,
        idempotency_key: str,
        state: str,
        authorization_ref: str | None,
        now: datetime,
    ) -> AuthorizationAttempt:
        """Record a definite decline or uncertainty without releasing the intent lock."""
        _require_tz_aware(now)
        if state not in {"uncertain", "declined"}:
            raise ValueError("invalid authorization outcome state")
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                _AUTHORIZATION_SELECT + "WHERE intake_id=? AND idempotency_key=?",
                (intake_id, idempotency_key),
            ).fetchone()
            if row is None:
                raise IntakeConflict("authorization_attempt_not_found")
            current = _authorization_attempt(row)
            if current.state in {"authorized", "declined"}:
                database.execute("COMMIT")
                return current
            if now < current.updated_at:
                raise IntakeConflict("authorization_outcome_unresolved")
            if (
                current.authorization_ref is not None
                and authorization_ref is not None
                and current.authorization_ref != authorization_ref
            ):
                raise IntakeConflict("authorization_reference_changed")
            reference = authorization_ref or current.authorization_ref
            database.execute(
                "UPDATE pb_intake_authorization_attempts SET state=?, authorization_ref=?, "
                "updated_at=?, revision=? WHERE intake_id=? AND idempotency_key=? AND revision=?",
                (
                    state,
                    reference,
                    now.isoformat(),
                    current.revision + 1,
                    intake_id,
                    idempotency_key,
                    current.revision,
                ),
            )
            row = database.execute(
                _AUTHORIZATION_SELECT + "WHERE intake_id=? AND idempotency_key=?",
                (intake_id, idempotency_key),
            ).fetchone()
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
        return _authorization_attempt(row)

    def get_intake(self: _Connected, intake_id: str) -> IntakeRecord | None:
        row = self._require_open().execute(_SELECT + "WHERE r.intake_id=?", (intake_id,)).fetchone()
        return None if row is None else _record(row)

    def intake_for_workspace(self: _Connected, workspace_token: str) -> IntakeRecord | None:
        row = (
            self._require_open()
            .execute(_SELECT + "WHERE r.workspace_token=?", (workspace_token,))
            .fetchone()
        )
        return None if row is None else _record(row)

    def update_intake(
        self: _Connected,
        intake_id: str,
        expected_revision: int,
        *,
        selected_tier: str | None,
        case_token: str | None,
    ) -> IntakeRecord:
        """Set the tier and, once, the started case token; both lock after a case starts."""
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(_SELECT + "WHERE r.intake_id=?", (intake_id,)).fetchone()
            if row is None:
                raise IntakeConflict("intake_not_found")
            current = _record(row)
            if current.revision != expected_revision:
                raise IntakeConflict("intake_revision_stale")
            if selected_tier != current.selected_tier:
                active = database.execute(
                    "SELECT 1 FROM pb_intake_authorization_attempts "
                    "WHERE intake_id=? AND state IN ('pending', 'uncertain', 'authorized') "
                    "LIMIT 1",
                    (intake_id,),
                ).fetchone()
                if active is not None:
                    raise IntakeConflict("tier_locked_at_authorization")
            if current.case_token is not None and (
                case_token != current.case_token or selected_tier != current.selected_tier
            ):
                raise IntakeConflict("tier_locked_at_authorization")
            database.execute(
                "UPDATE pb_intake_records SET selected_tier=?, case_token=?, revision=? "
                "WHERE intake_id=? AND revision=?",
                (selected_tier, case_token, current.revision + 1, intake_id, current.revision),
            )
            row = database.execute(_SELECT + "WHERE r.intake_id=?", (intake_id,)).fetchone()
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
        return _record(row)

    def get_research(self: _Connected, intake_id: str) -> SavedResearch | None:
        row = (
            self._require_open()
            .execute(
                "SELECT research_json, answers_json, updated_at, revision "
                "FROM pb_intake_research WHERE intake_id=?",
                (intake_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        research, answers, updated, revision = row
        if not (
            isinstance(research, str)
            and isinstance(answers, str)
            and isinstance(updated, str)
            and type(revision) is int
        ):
            raise StorageCorruption("malformed saved research")
        return SavedResearch(research, answers, datetime.fromisoformat(updated), revision)

    def recent_research_json(
        self: _Connected, since: datetime, exclude_intake_id: str, limit: int
    ) -> tuple[str, ...]:
        """Newest saved research records touched since `since`, for cross-case reuse.

        Only the research record is read, never customer answers, and never the case
        asking. Callers decide whether any record is usable.
        """
        rows = (
            self._require_open()
            .execute(
                "SELECT research_json FROM pb_intake_research WHERE intake_id<>? "
                "AND updated_at>=? ORDER BY updated_at DESC, intake_id LIMIT ?",
                (exclude_intake_id, since.isoformat(), max(0, min(limit, 50))),
            )
            .fetchall()
        )
        if not all(isinstance(row[0], str) for row in rows):
            raise StorageCorruption("malformed saved research")
        return tuple(row[0] for row in rows)

    def save_research(
        self: _Connected,
        intake_id: str,
        research_json: str,
        answers_json: str,
        now: datetime,
        expected_revision: int | None,
    ) -> SavedResearch:
        """Saved pages and findings for one case; later answers resolve against them."""
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT revision FROM pb_intake_research WHERE intake_id=?", (intake_id,)
            ).fetchone()
            current = None if row is None else row[0]
            if current != expected_revision:
                raise IntakeConflict("research_revision_stale")
            revision = 1 if current is None else current + 1
            if current is None:
                database.execute(
                    "INSERT INTO pb_intake_research VALUES (?, ?, ?, ?, ?)",
                    (intake_id, research_json, answers_json, now.isoformat(), revision),
                )
            else:
                database.execute(
                    "UPDATE pb_intake_research SET research_json=?, answers_json=?, "
                    "updated_at=?, revision=? WHERE intake_id=? AND revision=?",
                    (research_json, answers_json, now.isoformat(), revision, intake_id, current),
                )
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
        return SavedResearch(research_json, answers_json, now, revision)

    def get_draft(self: _Connected, intake_id: str) -> SavedDraft | None:
        row = (
            self._require_open()
            .execute(
                "SELECT letter_text, spans_json, flags_json, adopted_sha256, adopted_at, "
                "adopted_by, packet_sha256, updated_at, revision FROM pb_intake_drafts "
                "WHERE intake_id=?",
                (intake_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        text, spans, flags, sha, adopted, owner, packet, updated, revision = row
        if not (
            all(isinstance(v, str) for v in (text, spans, flags, updated))
            and all(v is None or isinstance(v, str) for v in (sha, adopted, owner, packet))
            and type(revision) is int
            and revision >= 1
            and (
                (sha is None and adopted is None and owner is None)
                or (sha is not None and adopted is not None and owner is not None)
            )
        ):
            raise StorageCorruption("malformed saved draft")
        return SavedDraft(
            text,
            spans,
            flags,
            sha,
            None if adopted is None else datetime.fromisoformat(adopted),
            owner,
            packet,
            datetime.fromisoformat(updated),
            revision,
        )

    def save_draft(
        self: _Connected,
        intake_id: str,
        draft: SavedDraft,
        expected_revision: int | None,
        *,
        expected_research_revision: int | None = None,
    ) -> SavedDraft:
        """Replace one draft under CAS; a frozen packet permanently locks its text."""
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            if expected_research_revision is not None:
                research = database.execute(
                    "SELECT revision FROM pb_intake_research WHERE intake_id=?", (intake_id,)
                ).fetchone()
                if research is None or research[0] != expected_research_revision:
                    raise IntakeConflict("research_revision_stale")
            row = database.execute(
                "SELECT revision, packet_sha256 FROM pb_intake_drafts WHERE intake_id=?",
                (intake_id,),
            ).fetchone()
            current = None if row is None else row[0]
            if current != expected_revision:
                raise IntakeConflict("draft_revision_stale")
            if row is not None and row[1] is not None:
                raise IntakeConflict("draft_frozen")
            revision = 1 if current is None else current + 1
            values = (
                draft.letter_text,
                draft.spans_json,
                draft.flags_json,
                draft.adopted_sha256,
                None if draft.adopted_at is None else draft.adopted_at.isoformat(),
                draft.adopted_by,
                draft.packet_sha256,
                draft.updated_at.isoformat(),
                revision,
            )
            if current is None:
                database.execute(
                    "INSERT INTO pb_intake_drafts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (intake_id, *values),
                )
            else:
                database.execute(
                    "UPDATE pb_intake_drafts SET letter_text=?, spans_json=?, flags_json=?, "
                    "adopted_sha256=?, adopted_at=?, adopted_by=?, packet_sha256=?, updated_at=?, "
                    "revision=? WHERE intake_id=? AND revision=?",
                    (*values, intake_id, current),
                )
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
        return replace(draft, revision=revision)

    def intakes_in_states(self: _Connected, states: tuple[str, ...]) -> tuple[str, ...]:
        """Intake ids whose started case is in one of these letter states (worker sweep)."""
        marks = ", ".join("?" for _ in states)
        rows = (
            self._require_open()
            .execute(
                "SELECT r.intake_id FROM pb_intake_records r JOIN pb_cases c "
                f"ON c.case_token = r.case_token WHERE c.letter_state IN ({marks}) "
                "ORDER BY r.created_at",
                states,
            )
            .fetchall()
        )
        return tuple(str(row[0]) for row in rows)
