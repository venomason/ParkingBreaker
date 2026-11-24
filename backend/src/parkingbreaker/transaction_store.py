"""Durable workspaces, case lifecycles and an atomic effect outbox. No provider calls."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from parkingbreaker import prices
from parkingbreaker.access_email_store import AccessEmailStoreMixin, initialize_access_email
from parkingbreaker.address_policy import (
    AddressProvenance,
    DestinationDecision,
    Responsibility,
    ResponsibilityDecision,
)
from parkingbreaker.address_store import AddressStoreMixin, initialize_address_store
from parkingbreaker.address_store import _decisions as destination_decisions_snapshot
from parkingbreaker.ai_journal_store import AiJournalStoreMixin, initialize_ai_journal
from parkingbreaker.ai_spend_store import AiSpendStoreMixin, initialize_ai_spend
from parkingbreaker.approval_manifest import (
    ApprovalManifest,
    ApprovalManifestStoreMixin,
    initialize_approval_manifests,
    verify_dispatch_integrity,
    verify_remail_approval,
)
from parkingbreaker.beta_feedback_store import (
    BetaFeedbackStoreMixin,
    initialize_beta_feedback,
)
from parkingbreaker.beta_store import (
    BetaConflict,
    BetaStoreMixin,
    initialize_beta,
)
from parkingbreaker.case_hold_store import CaseHoldStoreMixin, initialize_case_holds
from parkingbreaker.case_state import (
    AuthorizationExpired,
    CaseState,
    ConfirmedNotSent,
    DestinationRefused,
    Effect,
    Event,
    HardStop,
    LetterNotSubmittable,
    RemailRequested,
    ResearchComplete,
    ReturnClosed,
    ReturnConfirmed,
    StopFamily,
    Stopped,
    StopReason,
    SubmitConfirmed,
    SubmitRejected,
    TimingRefused,
    WindowExpired,
    next_attempt,
    stop_family,
    transition,
)
from parkingbreaker.case_status import payment_event_for
from parkingbreaker.disputes import DisputeFact
from parkingbreaker.draft_ownership_store import (
    DraftOwnershipStoreMixin,
    initialize_draft_ownership,
)
from parkingbreaker.identity_store import IdentityStoreMixin, initialize_identity
from parkingbreaker.intake_pause_store import (
    IntakePauseStoreMixin,
    initialize_runtime_intake_pause,
)
from parkingbreaker.intake_store import IntakeConflict, IntakeStoreMixin, initialize_intake
from parkingbreaker.journey_context import (
    ContextFunding,
    ContextOperation,
    ContextReason,
    DurationBasis,
    JourneyEventContext,
    JourneyReference,
    opaque_reference,
)
from parkingbreaker.journey_events import (
    CaptureStartBasis,
    CostStatus,
    JourneyBuildIdentity,
    JourneyEventInput,
    JourneyEventKind,
    JourneyEventOutcome,
    JourneyEventsStoreMixin,
    append_journey_event_in_transaction,
    ensure_journey_identity_in_transaction,
    initialize_journey_events,
    prior_journey_event_reference_in_transaction,
)
from parkingbreaker.journey_timeline import JourneyTimelineStoreMixin
from parkingbreaker.lob_submission import (
    FALLBACK_LEAD,
    LOCAL_REFUSAL_REASON,
    TIMING_REFUSAL_REASON,
    VERIFIED_IDEMPOTENCY_WINDOW,
    Accepted,
    Canceled,
    LetterRequest,
    LookupByMetadata,
    NotSent,
    Rejected,
    SubmissionOutcome,
    Uncertain,
    UspsScan,
    create_allowed,
    next_action,
    pending_evidence,
)
from parkingbreaker.lob_webhook import has_returned_scan, scan_proof
from parkingbreaker.mail_attempt_store import (
    PreparationConflict,
    PreparedMailAttempt,
    SqliteMailAttemptStore,
    StorageCorruption,
    check_versions_schema,
    rollback_preserving_error,
)
from parkingbreaker.mail_request import request_fingerprint
from parkingbreaker.notification_store import (
    NotificationStoreMixin,
    initialize_notification_access_links,
)
from parkingbreaker.oplog.counters import CounterKey, counter_keys, decode_counter_key
from parkingbreaker.oplog.digest import CounterMilestone, DigestOutcome, LogDigest
from parkingbreaker.outcome_follow_up_store import (
    OutcomeFollowUpStoreMixin,
    initialize_outcome_follow_up,
)
from parkingbreaker.overseer_feed import OverseerFeedStoreMixin, initialize_ops_ids
from parkingbreaker.payment_records import (
    PaymentCursor,
    PaymentOutcome,
    cursor_json,
    decode_cursor,
    mutation_key,
)
from parkingbreaker.payment_records import (
    outcome_json as payment_outcome_json,
)
from parkingbreaker.payment_state import (
    CAPTURE_RECONCILIATION_EXHAUSTED,
    CAPTURE_RECONCILIATION_GRACE,
    EXPIRY_MARGIN,
    AuthorizationLapsed,
    CaptureFailed,
    CaptureSucceeded,
    CardDeclined,
    PaymentEffect,
    PaymentEvent,
    PaymentState,
    ReconciliationExhausted,
    RefundIssued,
    RetryCapture,
    RetryDecision,
    ReturnedMailRefundRequested,
    UnexpectedCaptureConfirmed,
    next_capture_retry,
    payment_transition,
)
from parkingbreaker.prices import CERTIFIED_CENTS
from parkingbreaker.pricing_store import PricingStoreMixin, initialize_pricing
from parkingbreaker.required_copy_store import (
    RequiredCopyStoreMixin,
    initialize_required_copy_store,
)
from parkingbreaker.returned_mail import Remedy, RemedyKind
from parkingbreaker.review_store import ReviewStoreMixin, initialize_review
from parkingbreaker.route_checks import RouteCheckStoreMixin, initialize_route_checks
from parkingbreaker.route_registry import RouteRegistryMixin, initialize_route_registry
from parkingbreaker.service_phase_store import (
    ServicePhaseStoreMixin,
    initialize_runtime_service_phase,
)
from parkingbreaker.session_window import DEFAULT_WINDOW
from parkingbreaker.settings_store import SettingsStoreMixin, initialize_settings
from parkingbreaker.submission_records import SubmissionCursor, decode_outcome, outcome_json
from parkingbreaker.support_store import SupportStoreMixin, initialize_support
from parkingbreaker.sweep_run_store import SweepRunStoreMixin, initialize_sweep_runs
from parkingbreaker.ticket_intake_store import initialize_ticket_intake
from parkingbreaker.ticket_observability import (
    TicketObservabilityStoreMixin,
    initialize_ticket_observability,
)
from parkingbreaker.tracking_email_store import (
    TrackingEmailStoreMixin,
    initialize_tracking_email,
)
from parkingbreaker.transaction_records import (
    CaseRecord,
    PendingEffect,
    TransitionRecord,
    WorkspaceRecord,
    decode_workspace,
    lifecycle_event_json,
    workspace_json,
)
from parkingbreaker.trick_store import TrickStoreMixin, initialize_tricks
from parkingbreaker.webhook_backlog_store import (
    WebhookBacklogStoreMixin,
    initialize_webhook_backlog,
    park_webhook,
    pin_webhook_envelope,
    settled_webhook,
)
from parkingbreaker.workspace import Workspace

REFUND_REPLAY_WINDOW = timedelta(hours=23)
# The mailing gate's fixed refusal code (address_policy.mailing_gate); a test pins equality.
GATE_REFUSED_REASON = "ADDRESS_MAILING_GATE_REFUSED"


@dataclass(frozen=True)
class ReturnedMailResolution:
    attempt: int
    responsibility: Responsibility
    reason: str
    remedy: Remedy | None
    resolved_at: datetime
    cost_is_estimate: bool | None = None


_REMEDY_V1_DDL = {
    "pb_mail_destination_bindings": """CREATE TABLE pb_mail_destination_bindings (
        case_token TEXT NOT NULL,
        attempt INTEGER NOT NULL CHECK (attempt IN (1, 2)),
        intake_id TEXT NOT NULL,
        owner TEXT NOT NULL,
        decision_revision INTEGER NOT NULL CHECK (decision_revision >= 1),
        request_sha256 TEXT NOT NULL,
        PRIMARY KEY (case_token, attempt)
    )""",
    "pb_return_remedies": """CREATE TABLE pb_return_remedies (
        case_token TEXT NOT NULL,
        attempt INTEGER NOT NULL CHECK (attempt IN (1, 2)),
        responsibility TEXT NOT NULL,
        reason TEXT NOT NULL,
        kind TEXT,
        refund_cents INTEGER NOT NULL CHECK (refund_cents >= 0),
        remails INTEGER NOT NULL CHECK (remails IN (0, 1)),
        resolved_at TEXT NOT NULL,
        PRIMARY KEY (case_token, attempt)
    )""",
}


_REMEDY_DDL = {
    **_REMEDY_V1_DDL,
    "pb_return_remedies": _REMEDY_V1_DDL["pb_return_remedies"].replace(
        "resolved_at TEXT NOT NULL,",
        "resolved_at TEXT NOT NULL,\n        "
        "cost_is_estimate INTEGER CHECK (cost_is_estimate IN (0, 1)),",
    ),
}


def _initialize_return_remedies(database: sqlite3.Connection) -> None:
    """Initialize/migrate remedies without inventing old responsibility or cost evidence."""
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='return_remedies'"
        ).fetchone()
        tables = {
            name: " ".join(sql.upper().split())
            for name, sql in database.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table'"
            )
            if name in _REMEDY_DDL
        }
        if version is None:
            if tables:
                raise StorageCorruption("unversioned return-remedy schema")
            for ddl in _REMEDY_DDL.values():
                database.execute(ddl)
            database.execute("INSERT INTO pb_store_versions VALUES ('return_remedies', 2)")
        elif version == (1,) and tables == {
            name: " ".join(ddl.upper().split()) for name, ddl in _REMEDY_V1_DDL.items()
        }:
            # Preserve every resolution; old rows have an unknown cost basis, never an
            # invented actual. New resolutions record the basis atomically with money.
            database.execute("ALTER TABLE pb_return_remedies RENAME TO pb_return_remedies_v1")
            database.execute(_REMEDY_DDL["pb_return_remedies"])
            database.execute(
                "INSERT INTO pb_return_remedies "
                "(case_token, attempt, responsibility, reason, kind, refund_cents, "
                "remails, resolved_at) "
                "SELECT case_token, attempt, responsibility, reason, kind, refund_cents, remails, "
                "resolved_at FROM pb_return_remedies_v1"
            )
            database.execute("DROP TABLE pb_return_remedies_v1")
            database.execute(
                "UPDATE pb_store_versions SET version=2 WHERE component='return_remedies'"
            )
        elif version != (2,) or tables != {
            name: " ".join(ddl.upper().split()) for name, ddl in _REMEDY_DDL.items()
        }:
            raise StorageCorruption("malformed return-remedy schema")
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


class RevisionConflict(ValueError):
    """The requested workspace or case revision does not match the stored revision."""


class EffectConflict(ValueError):
    """An outbox effect cannot be claimed or completed as requested."""


class CaseConflict(ValueError):
    """An authorization reference is already bound to another case anchor."""


class SubmissionPaused(Exception):
    """An effective incident hold refuses a new physical create intent (T74).

    A scheduling control result, never a provider rejection, timeout, stop or effect:
    nothing is written, and a matching resume may replay the same approved identity.
    Lookup-only reconciliation of an existing uncertain cursor is still allowed.
    """

    def __init__(self, case_id: str, attempt: int) -> None:
        self.case_id = case_id
        self.attempt = attempt
        super().__init__(f"submission paused by an incident hold: {case_id} attempt {attempt}")


_WORKSPACE_COLUMNS = ("workspace_token", "payload_json", "updated_at", "revision")
_HISTORY_COLUMNS = ("workspace_token", "revision", "payload_json", "updated_at")
_CASE_COLUMNS = (
    "case_token",
    "workspace_token",
    "authorization_ref",
    "authorized_at",
    "authorization_expires_at",
    "amount_cents",
    "letter_state",
    "payment_state",
    "attempt",
    "revision",
)


_COUNTER_FLUSH_LIMIT = 500


def _check_limit(limit: object) -> None:
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be a positive integer")


def _limit_clause(
    limit: int | None, order: tuple[str, ...], *, newest_first: bool = False
) -> tuple[str, tuple[int, ...]]:
    """ORDER BY/LIMIT tail; ``order`` keys sort oldest first unless ``newest_first``.

    With ``limit`` the query reads at most that many rows, so a newest-first bound
    returns the newest rows without materializing the rest of the history or outbox.
    """
    if type(newest_first) is not bool:
        raise ValueError("newest_first must be a boolean")
    keys = ", ".join(f"{key} DESC" if newest_first else key for key in order)
    if limit is None:
        return f" ORDER BY {keys}", ()
    _check_limit(limit)
    return f" ORDER BY {keys} LIMIT ?", (limit,)


def _aware(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value


def _token(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-blank string")
    return value


def _check_table(
    database: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    types: tuple[str, ...],
    primary_keys: dict[str, int],
    not_null: frozenset[str],
) -> None:
    rows = tuple(database.execute(f"PRAGMA table_info({table})").fetchall())
    if tuple(row[1] for row in rows) != columns:
        raise StorageCorruption(f"unknown {table} column layout")
    for row, expected_type in zip(rows, types, strict=True):
        _, name, column_type, required, default, primary_key = row
        if column_type != expected_type or default is not None:
            raise StorageCorruption(f"invalid {table} column definition: {name}")
        if required != (1 if name in not_null else 0):
            raise StorageCorruption(f"invalid {table} nullability: {name}")
        if primary_key != primary_keys.get(name, 0):
            raise StorageCorruption(f"invalid {table} primary key: {name}")


def _check_workspace_tables(database: sqlite3.Connection) -> None:
    _check_table(
        database,
        "pb_workspaces",
        _WORKSPACE_COLUMNS,
        ("TEXT", "TEXT", "TEXT", "INTEGER"),
        {"workspace_token": 1},
        frozenset({"payload_json", "updated_at", "revision"}),
    )
    _check_table(
        database,
        "pb_workspace_history",
        _HISTORY_COLUMNS,
        ("TEXT", "INTEGER", "TEXT", "TEXT"),
        {"workspace_token": 1, "revision": 2},
        frozenset(_HISTORY_COLUMNS),
    )
    fk = tuple(database.execute("PRAGMA foreign_key_list(pb_workspace_history)").fetchall())
    if len(fk) != 1 or tuple(fk[0][2:5]) != ("pb_workspaces", "workspace_token", "workspace_token"):
        raise StorageCorruption("invalid pb_workspace_history foreign key")
    workspaces_sql = database.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='pb_workspaces'"
    ).fetchone()[0]
    history_sql = database.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='pb_workspace_history'"
    ).fetchone()[0]
    workspace_norm = " ".join(str(workspaces_sql).upper().split())
    history_norm = " ".join(str(history_sql).upper().split())
    for expected in ("CHECK (REVISION >= 1)",):
        if expected not in workspace_norm or expected not in history_norm:
            raise StorageCorruption("workspace revision constraint is missing")


def _check_case_table(database: sqlite3.Connection, *, beta_enabled: bool = False) -> None:
    _check_table(
        database,
        "pb_cases",
        _CASE_COLUMNS,
        ("TEXT", "TEXT", "TEXT", "TEXT", "TEXT", "INTEGER", "TEXT", "TEXT", "INTEGER", "INTEGER"),
        {"case_token": 1},
        frozenset(set(_CASE_COLUMNS) - {"case_token"}),
    )
    sql_row = database.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='pb_cases'"
    ).fetchone()
    sql = " ".join(str(sql_row[0]).upper().split())
    required: tuple[str, ...] = (
        "CHECK (AMOUNT_CENTS >= 0)" if beta_enabled else "CHECK (AMOUNT_CENTS > 0)",
        "CHECK (ATTEMPT IN (1, 2))",
        "CHECK (REVISION >= 1)",
    )
    if beta_enabled:
        required += ("CHECK ((AMOUNT_CENTS = 0) = (PAYMENT_STATE = 'NO_CHARGE'))",)
    if any(check not in sql for check in required):
        raise StorageCorruption("pb_cases check constraints are missing")
    fk = tuple(database.execute("PRAGMA foreign_key_list(pb_cases)").fetchall())
    if len(fk) != 1 or tuple(fk[0][2:5]) != ("pb_workspaces", "workspace_token", "workspace_token"):
        raise StorageCorruption("invalid pb_cases foreign key")
    unique_indexes = {
        tuple(
            row[2]
            for row in database.execute(
                "SELECT seqno, cid, name FROM pragma_index_info(?) ORDER BY seqno", (index[1],)
            )
        )
        for index in database.execute("PRAGMA index_list(pb_cases)").fetchall()
        if index[2] and not index[4]
    }
    if ("authorization_ref",) not in unique_indexes:
        raise StorageCorruption("pb_cases authorization_ref uniqueness constraint is missing")


def _migrate_no_charge_cases(database: sqlite3.Connection) -> None:
    database.execute("ALTER TABLE pb_cases RENAME TO pb_cases_v4")
    database.execute("""CREATE TABLE pb_cases (
        case_token TEXT PRIMARY KEY,
        workspace_token TEXT NOT NULL REFERENCES pb_workspaces(workspace_token),
        authorization_ref TEXT NOT NULL UNIQUE, authorized_at TEXT NOT NULL,
        authorization_expires_at TEXT NOT NULL,
        amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
        letter_state TEXT NOT NULL, payment_state TEXT NOT NULL,
        attempt INTEGER NOT NULL CHECK (attempt IN (1, 2)),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        CHECK ((amount_cents = 0) = (payment_state = 'NO_CHARGE'))
    )""")
    database.execute("INSERT INTO pb_cases SELECT * FROM pb_cases_v4")
    database.execute("DROP TABLE pb_cases_v4")
    database.execute("UPDATE pb_store_versions SET version=5 WHERE component='transactions'")


@dataclass(frozen=True)
class _InodeIdentity:
    """Stable device/inode pair for one pathname.

    Equality is exact: a rename or replacement of the pathname yields a different
    identity even when the original inode is still reachable under another name.
    """

    device: int
    inode: int

    @classmethod
    def from_status(cls, status: os.stat_result) -> _InodeIdentity:
        return cls(device=status.st_dev, inode=status.st_ino)


def _canonical_database_path(path: Path) -> Path:
    """Resolve every parent alias once, leaving the final component unresolved.

    Parent symlinks and relative/``..`` aliases must name one data directory. The
    database file itself is checked separately so a symlinked file is refused
    instead of silently followed.
    """
    return Path(os.path.realpath(path.parent)) / path.name


def _directory_identity(path: Path) -> _InodeIdentity | None:
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(status.st_mode):
        raise ValueError("submission lock parent must be a directory")
    return _InodeIdentity.from_status(status)


def _database_identity(path: Path) -> _InodeIdentity | None:
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(status.st_mode):
        raise ValueError("database path must not be a symlink")
    if not stat.S_ISREG(status.st_mode):
        raise ValueError("database path must be a regular file")
    if status.st_nlink != 1:
        raise ValueError("database path must not be a hard link")
    return _InodeIdentity.from_status(status)


@dataclass(frozen=True)
class _SubmissionLockAnchor:
    """Canonical identities captured when the store opened its database."""

    directory: Path
    directory_identity: _InodeIdentity
    database: Path
    database_identity: _InodeIdentity


def _assert_anchor_unchanged(anchor: _SubmissionLockAnchor) -> None:
    """Fail closed when the data directory or database was replaced after opening."""
    try:
        directory = _directory_identity(anchor.directory)
        database = _database_identity(anchor.database)
    except (OSError, ValueError) as exc:
        raise RevisionConflict("submission lock identity changed") from exc
    if directory != anchor.directory_identity:
        raise RevisionConflict("submission lock directory was replaced")
    if database != anchor.database_identity:
        raise RevisionConflict("submission database was replaced")


def _open_anchor_directory(anchor: _SubmissionLockAnchor) -> int:
    """Open only the stable directory inode, never the SQLite database inode."""
    try:
        descriptor = os.open(
            anchor.directory,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as exc:
        raise RevisionConflict("submission lock directory cannot be opened") from exc
    try:
        if _InodeIdentity.from_status(os.fstat(descriptor)) != anchor.directory_identity:
            raise RevisionConflict("submission lock directory was replaced")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _initialize_transactions(database: sqlite3.Connection) -> None:
    # Outbox table replacement must preserve child references to the stable table name.
    # SQLite's legacy rename mode does that while foreign-key checks are explicitly
    # performed before commit, then enforcement is restored for normal store operation.
    database.execute("PRAGMA legacy_alter_table=ON")
    database.execute("PRAGMA foreign_keys=OFF")
    database.execute("BEGIN IMMEDIATE")
    try:
        check_versions_schema(database)
        version_row = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='transactions'"
        ).fetchone()
        has_workspace = (
            database.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pb_workspaces'"
            ).fetchone()
            is not None
        )
        has_history = (
            database.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pb_workspace_history'"
            ).fetchone()
            is not None
        )
        has_cases = (
            database.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pb_cases'"
            ).fetchone()
            is not None
        )
        has_outbox = (
            database.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pb_pending_effects'"
            ).fetchone()
            is not None
        )
        if version_row is None:
            if has_workspace or has_history or has_cases or has_outbox:
                raise StorageCorruption("transaction tables exist without a known version")
            database.execute(
                """CREATE TABLE pb_workspaces (
                    workspace_token TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 1)
                )"""
            )
            database.execute(
                """CREATE TABLE pb_workspace_history (
                    workspace_token TEXT NOT NULL REFERENCES pb_workspaces(workspace_token),
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (workspace_token, revision)
                )"""
            )
            database.execute(
                """CREATE TABLE pb_cases (
                    case_token TEXT PRIMARY KEY,
                    workspace_token TEXT NOT NULL REFERENCES pb_workspaces(workspace_token),
                    authorization_ref TEXT NOT NULL UNIQUE,
                    authorized_at TEXT NOT NULL,
                    authorization_expires_at TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                    letter_state TEXT NOT NULL,
                    payment_state TEXT NOT NULL,
                    attempt INTEGER NOT NULL CHECK (attempt IN (1, 2)),
                    revision INTEGER NOT NULL CHECK (revision >= 1)
                )"""
            )
            database.execute(
                "INSERT INTO pb_store_versions(component, version) VALUES ('transactions', 1)"
            )
        elif type(version_row[0]) is not int or version_row[0] not in (1, 2, 3, 4, 5):
            raise StorageCorruption("unsupported transactions schema version")
        elif not (has_workspace and has_history and has_cases):
            raise StorageCorruption("transaction schema is incomplete")
        _check_workspace_tables(database)
        version = 1 if version_row is None else version_row[0]
        _check_case_table(database, beta_enabled=version == 5)
        _initialize_lifecycle(database, 1 if version_row is None else version_row[0])
        if version < 5:
            _migrate_no_charge_cases(database)
        _check_case_table(database, beta_enabled=True)
        _initialize_monitor_indexes(database)
        _initialize_submission(database)
        if database.execute("PRAGMA foreign_key_check").fetchall():
            raise StorageCorruption("transaction migration left invalid foreign keys")
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise
    finally:
        database.execute("PRAGMA legacy_alter_table=OFF")
        database.execute("PRAGMA foreign_keys=ON")


def _initialize_monitor_indexes(database: sqlite3.Connection) -> None:
    # Derived indexes for the read-only operational monitor. Install inside the existing
    # schema transaction on both fresh and restored databases.
    for sql in (
        "CREATE INDEX IF NOT EXISTS pb_cases_monitor_state ON pb_cases(letter_state, case_token)",
        "CREATE INDEX IF NOT EXISTS pb_cases_monitor_paid_workspace "
        "ON pb_cases(workspace_token, julianday(authorized_at)) WHERE amount_cents > 0",
        "CREATE INDEX IF NOT EXISTS pb_history_monitor_letter_entry "
        "ON pb_case_history(case_token, revision DESC) WHERE machine='letter' AND source<>target",
        "CREATE INDEX IF NOT EXISTS pb_history_monitor_exhaustion "
        "ON pb_case_history(event_name) WHERE machine='payment'",
        "CREATE INDEX IF NOT EXISTS pb_effects_monitor_writeoff "
        "ON pb_pending_effects(effect) WHERE machine='payment' AND completed_at IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS pb_effects_monitor_counter_time "
        "ON pb_pending_effects(effect, julianday(created_at)) "
        "WHERE machine='counter' AND completed_at IS NOT NULL",
    ):
        database.execute(sql)


def _initialize_lifecycle(database: sqlite3.Connection, version: int) -> None:
    if version == 1:
        database.execute("""CREATE TABLE pb_case_history (
            case_token TEXT NOT NULL REFERENCES pb_cases(case_token),
            revision INTEGER NOT NULL CHECK (revision >= 2),
            machine TEXT NOT NULL CHECK (machine IN ('letter', 'payment')),
            source TEXT NOT NULL, target TEXT NOT NULL, event_name TEXT NOT NULL,
            event_json TEXT NOT NULL, occurred_at TEXT NOT NULL,
            PRIMARY KEY (case_token, revision, machine)
        )""")
        database.execute(_LEGACY_OUTBOX_DDL)
        database.execute("UPDATE pb_store_versions SET version=2 WHERE component='transactions'")
    if version < 3:
        row = database.execute(
            "SELECT sql FROM sqlite_master WHERE name='pb_pending_effects'"
        ).fetchone()
        if row is None or " ".join(row[0].upper().split()) != " ".join(
            _LEGACY_OUTBOX_DDL.upper().split()
        ):
            raise StorageCorruption("invalid legacy outbox schema")
        # v2 -> v3 admits request-scoped counter rows before a case exists.
        database.execute("ALTER TABLE pb_pending_effects RENAME TO pb_pending_effects_v2")
        database.execute(_REQUEST_OUTBOX_DDL)
        database.execute(
            "INSERT INTO pb_pending_effects SELECT *, NULL, NULL FROM pb_pending_effects_v2"
        )
        database.execute("DROP TABLE pb_pending_effects_v2")
        database.execute("UPDATE pb_store_versions SET version=3 WHERE component='transactions'")
    if version < 4:
        row = database.execute(
            "SELECT sql FROM sqlite_master WHERE name='pb_pending_effects'"
        ).fetchone()
        if row is None or " ".join(row[0].upper().split()) != " ".join(
            _REQUEST_OUTBOX_DDL.upper().split()
        ):
            raise StorageCorruption("invalid request outbox schema")
        # v3 -> v4 adds identity mail intents without changing existing row values.
        database.execute("ALTER TABLE pb_pending_effects RENAME TO pb_pending_effects_v3")
        database.execute(_OUTBOX_DDL)
        database.execute("INSERT INTO pb_pending_effects SELECT * FROM pb_pending_effects_v3")
        database.execute("DROP TABLE pb_pending_effects_v3")
        database.execute("UPDATE pb_store_versions SET version=4 WHERE component='transactions'")
    _check_table(
        database,
        "pb_case_history",
        (
            "case_token",
            "revision",
            "machine",
            "source",
            "target",
            "event_name",
            "event_json",
            "occurred_at",
        ),
        ("TEXT", "INTEGER", "TEXT", "TEXT", "TEXT", "TEXT", "TEXT", "TEXT"),
        {"case_token": 1, "revision": 2, "machine": 3},
        frozenset(
            {
                "case_token",
                "revision",
                "machine",
                "source",
                "target",
                "event_name",
                "event_json",
                "occurred_at",
            }
        ),
    )
    _check_table(
        database,
        "pb_pending_effects",
        (
            "effect_id",
            "case_token",
            "revision",
            "machine",
            "effect",
            "created_at",
            "claimed_at",
            "outcome",
            "completed_at",
            "request_token",
            "counter_json",
        ),
        ("TEXT", "TEXT", "INTEGER", "TEXT", "TEXT", "TEXT", "TEXT", "TEXT", "TEXT", "TEXT", "TEXT"),
        {"effect_id": 1},
        frozenset({"machine", "effect", "created_at"}),
    )
    history_fk = tuple(database.execute("PRAGMA foreign_key_list(pb_case_history)").fetchall())
    if len(history_fk) != 1 or tuple(history_fk[0][2:5]) != (
        "pb_cases",
        "case_token",
        "case_token",
    ):
        raise StorageCorruption("invalid pb_case_history foreign key")
    effect_fk = tuple(database.execute("PRAGMA foreign_key_list(pb_pending_effects)").fetchall())
    if len(effect_fk) != 3 or {tuple(row[2:5]) for row in effect_fk} != {
        ("pb_case_history", column, column) for column in ("case_token", "revision", "machine")
    }:
        raise StorageCorruption("invalid pb_pending_effects foreign key")
    for table, constraints in (
        ("pb_case_history", ("CHECK (REVISION >= 2)", "CHECK (MACHINE IN ('LETTER', 'PAYMENT'))")),
        (
            "pb_pending_effects",
            (
                "CHECK ((OUTCOME IS NULL) = (COMPLETED_AT IS NULL))",
                "CHECK (COMPLETED_AT IS NULL OR CLAIMED_AT IS NOT NULL)",
            ),
        ),
    ):
        row = database.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        sql = " ".join(str(row[0]).upper().split())
        if any(constraint not in sql for constraint in constraints):
            raise StorageCorruption(f"{table} check constraints are missing")
    outbox_sql = database.execute(
        "SELECT sql FROM sqlite_master WHERE name='pb_pending_effects'"
    ).fetchone()[0]
    if " ".join(outbox_sql.upper().split()) != " ".join(_OUTBOX_DDL.upper().split()):
        raise StorageCorruption("invalid request outbox schema")
    # The case history is the appeal event log: rows are appended, never changed.
    for action in ("UPDATE", "DELETE"):
        database.execute(
            f"CREATE TRIGGER IF NOT EXISTS pb_case_history_no_{action.lower()} "
            f"BEFORE {action} ON pb_case_history "
            "BEGIN SELECT RAISE(ABORT, 'case history is append-only'); END"
        )
    _initialize_counters(database)


_LEGACY_OUTBOX_DDL = """CREATE TABLE pb_pending_effects (
            effect_id TEXT PRIMARY KEY, case_token TEXT NOT NULL,
            revision INTEGER NOT NULL, machine TEXT NOT NULL,
            effect TEXT NOT NULL, created_at TEXT NOT NULL,
            claimed_at TEXT, outcome TEXT, completed_at TEXT,
            FOREIGN KEY (case_token, revision, machine)
                REFERENCES pb_case_history(case_token, revision, machine),
            CHECK ((outcome IS NULL) = (completed_at IS NULL)),
            CHECK (completed_at IS NULL OR claimed_at IS NOT NULL)
        )"""

_REQUEST_OUTBOX_DDL = """CREATE TABLE pb_pending_effects (
    effect_id TEXT PRIMARY KEY, case_token TEXT,
    revision INTEGER, machine TEXT NOT NULL,
    effect TEXT NOT NULL, created_at TEXT NOT NULL,
    claimed_at TEXT, outcome TEXT, completed_at TEXT,
    request_token TEXT, counter_json TEXT,
    FOREIGN KEY (case_token, revision, machine)
        REFERENCES pb_case_history(case_token, revision, machine),
    UNIQUE (request_token, effect),
    CHECK ((outcome IS NULL) = (completed_at IS NULL)),
    CHECK (completed_at IS NULL OR claimed_at IS NOT NULL),
    CHECK ((machine = 'counter' AND request_token IS NOT NULL AND counter_json IS NOT NULL
            AND case_token IS NULL AND revision IS NULL)
        OR (machine IN ('letter', 'payment') AND request_token IS NULL AND counter_json IS NULL
            AND case_token IS NOT NULL AND revision IS NOT NULL))
)"""

_OUTBOX_DDL = """CREATE TABLE pb_pending_effects (
    effect_id TEXT PRIMARY KEY, case_token TEXT,
    revision INTEGER, machine TEXT NOT NULL,
    effect TEXT NOT NULL, created_at TEXT NOT NULL,
    claimed_at TEXT, outcome TEXT, completed_at TEXT,
    request_token TEXT, counter_json TEXT,
    FOREIGN KEY (case_token, revision, machine)
        REFERENCES pb_case_history(case_token, revision, machine),
    UNIQUE (request_token, effect),
    CHECK ((outcome IS NULL) = (completed_at IS NULL)),
    CHECK (completed_at IS NULL OR claimed_at IS NOT NULL),
    CHECK ((machine = 'counter' AND request_token IS NOT NULL AND counter_json IS NOT NULL
            AND case_token IS NULL AND revision IS NULL)
        OR (machine IN ('letter', 'payment') AND request_token IS NULL AND counter_json IS NULL
            AND case_token IS NOT NULL AND revision IS NOT NULL)
        OR (machine = 'notification' AND effect = 'SEND_SIGN_IN_LINK'
            AND case_token IS NULL AND revision IS NULL
            AND request_token IS NOT NULL AND counter_json IS NULL))
)"""

_COUNTERS_DDL = """CREATE TABLE pb_coverage_counters (
    event_token TEXT PRIMARY KEY, payload_json TEXT NOT NULL
)"""


def _initialize_counters(database: sqlite3.Connection) -> None:
    version = database.execute(
        "SELECT version FROM pb_store_versions WHERE component='counters'"
    ).fetchone()
    row = database.execute(
        "SELECT sql FROM sqlite_master WHERE name='pb_coverage_counters'"
    ).fetchone()
    if version is None:
        if row is not None:
            raise StorageCorruption("unversioned counters schema")
        database.execute(_COUNTERS_DDL)
        database.execute("INSERT INTO pb_store_versions VALUES ('counters', 1)")
    elif type(version[0]) is not int or version[0] != 1 or row is None:
        raise StorageCorruption("unsupported counters schema")
    elif " ".join(row[0].upper().split()) != " ".join(_COUNTERS_DDL.upper().split()):
        raise StorageCorruption("invalid counters schema")


def _record_datetime(text: object, label: str) -> datetime:
    if not isinstance(text, str):
        raise StorageCorruption(f"invalid stored {label}")
    try:
        value = datetime.fromisoformat(text)
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp is not timezone-aware")
        return value
    except (TypeError, ValueError, OverflowError) as exc:
        raise StorageCorruption(f"invalid stored {label}: {exc}") from exc


def _workspace_record(row: tuple[object, ...]) -> WorkspaceRecord:
    token, payload, updated, revision = row
    try:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("invalid workspace token")
        if not isinstance(payload, str):
            raise ValueError("invalid workspace payload")
        if type(revision) is not int or revision < 1:
            raise ValueError("invalid workspace revision")
        workspace = decode_workspace(payload)
        return WorkspaceRecord(
            token, workspace, _record_datetime(updated, "workspace timestamp"), revision
        )
    except StorageCorruption:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise StorageCorruption(f"invalid stored workspace: {exc}") from exc


def _case_record(row: tuple[object, ...]) -> CaseRecord:
    (
        case_token,
        workspace_token,
        authorization_ref,
        authorized_text,
        expires_text,
        amount_cents,
        letter_text,
        payment_text,
        attempt,
        revision,
    ) = row
    try:
        case_token = _token(case_token, "stored case token")
        workspace_token = _token(workspace_token, "stored workspace token")
        authorization_ref = _token(authorization_ref, "stored authorization reference")
        if type(amount_cents) is not int or amount_cents < 0:
            raise ValueError("invalid amount")
        if type(attempt) is not int or attempt not in (1, 2):
            raise ValueError("invalid attempt")
        if type(revision) is not int or revision < 1:
            raise ValueError("invalid case revision")
        if not isinstance(letter_text, str) or not isinstance(payment_text, str):
            raise ValueError("invalid stored state")
        payment_state = PaymentState(payment_text)
        if (amount_cents == 0) != (payment_state is PaymentState.NO_CHARGE):
            raise ValueError("invalid no-charge funding")
        if payment_state is PaymentState.NO_CHARGE and (
            not authorization_ref.startswith("beta_") or attempt != 1
        ):
            raise ValueError("invalid beta service authorization")
        authorized_at = _record_datetime(authorized_text, "authorization timestamp")
        authorization_expires_at = _record_datetime(expires_text, "authorization expiry")
        if authorization_expires_at <= authorized_at:
            raise ValueError("stored authorization expiry does not follow authorization time")
        parsed_token = uuid.UUID(case_token)
        if parsed_token.version != 4 or parsed_token.hex != case_token:
            raise ValueError("stored case token is not an opaque UUID4")
        return CaseRecord(
            case_token=case_token,
            workspace_token=workspace_token,
            authorization_ref=authorization_ref,
            authorized_at=authorized_at,
            authorization_expires_at=authorization_expires_at,
            amount_cents=amount_cents,
            letter_state=CaseState(letter_text),
            payment_state=payment_state,
            attempt=attempt,
            revision=revision,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise StorageCorruption(f"invalid stored case: {exc}") from exc


class SqliteTransactionStore(
    ApprovalManifestStoreMixin,
    ServicePhaseStoreMixin,
    IntakePauseStoreMixin,
    OutcomeFollowUpStoreMixin,
    RequiredCopyStoreMixin,
    AddressStoreMixin,
    AccessEmailStoreMixin,
    AiJournalStoreMixin,
    AiSpendStoreMixin,
    DraftOwnershipStoreMixin,
    SweepRunStoreMixin,
    CaseHoldStoreMixin,
    OverseerFeedStoreMixin,
    BetaStoreMixin,
    BetaFeedbackStoreMixin,
    ReviewStoreMixin,
    RouteRegistryMixin,
    RouteCheckStoreMixin,
    TrickStoreMixin,
    TrackingEmailStoreMixin,
    SettingsStoreMixin,
    NotificationStoreMixin,
    IdentityStoreMixin,
    JourneyEventsStoreMixin,
    JourneyTimelineStoreMixin,
    TicketObservabilityStoreMixin,
    PricingStoreMixin,
    IntakeStoreMixin,
    SupportStoreMixin,
    WebhookBacklogStoreMixin,
    SqliteMailAttemptStore,
):
    """The existing SQLite database extended with workspace and case records."""

    def __init__(self, path: Path, *, journey_identity: JourneyBuildIdentity | None = None) -> None:
        if journey_identity is not None and type(journey_identity) is not JourneyBuildIdentity:
            raise ValueError("invalid journey build identity")
        self._journey_identity = journey_identity
        self._submission_locked = False
        self._submission_anchor: _SubmissionLockAnchor | None = None
        self._overseer_key: tuple[bytes, int] | None = None
        database_path = _canonical_database_path(Path(path))
        directory = database_path.parent
        expected_directory = _directory_identity(directory)
        expected_database = _database_identity(database_path)
        super().__init__(path)
        try:
            observed_directory = _directory_identity(directory)
            observed_database = _database_identity(database_path)
            if observed_directory is None or observed_database is None:
                raise ValueError("database path vanished while the store was opening")
            if expected_directory is not None and observed_directory != expected_directory:
                raise ValueError("database directory was replaced while the store was opening")
            if expected_database is not None and observed_database != expected_database:
                raise ValueError("database file was replaced while the store was opening")
            self._submission_anchor = _SubmissionLockAnchor(
                directory=directory,
                directory_identity=observed_directory,
                database=database_path,
                database_identity=observed_database,
            )
            _initialize_transactions(self._require_open())
            _initialize_payment(self._require_open())
            initialize_intake(self._require_open())
            initialize_ticket_intake(self._require_open())
            initialize_address_store(self._require_open())
            initialize_required_copy_store(self._require_open())
            initialize_identity(self._require_open())
            initialize_pricing(self._require_open())
            initialize_notification_access_links(self._require_open())
            initialize_access_email(self._require_open())
            _initialize_webhooks(self._require_open())
            initialize_webhook_backlog(self._require_open())
            _initialize_return_remedies(self._require_open())
            initialize_ai_journal(self._require_open())
            initialize_ai_spend(self._require_open())
            initialize_draft_ownership(self._require_open())
            initialize_sweep_runs(self._require_open())
            initialize_case_holds(self._require_open())
            initialize_ops_ids(self._require_open())
            initialize_beta(self._require_open())
            initialize_beta_feedback(self._require_open())
            initialize_review(self._require_open())
            initialize_tricks(self._require_open())
            initialize_approval_manifests(self._require_open())
            initialize_route_registry(self._require_open())
            initialize_route_checks(self._require_open())
            initialize_tracking_email(self._require_open())
            initialize_settings(self._require_open())
            initialize_runtime_service_phase(self._require_open())
            initialize_runtime_intake_pause(self._require_open())
            initialize_outcome_follow_up(self._require_open())
            initialize_journey_events(self._require_open())
            initialize_ticket_observability(self._require_open())
            initialize_support(self._require_open())
        except Exception:
            self.close()
            raise

    @property
    def journey_identity(self) -> JourneyBuildIdentity:
        return self._journey_identity or JourneyBuildIdentity()

    def bind_journey_identity(self, identity: JourneyBuildIdentity) -> None:
        """Bind declared current-runtime identity; never alter persisted origin/context."""
        if type(identity) is not JourneyBuildIdentity:
            raise ValueError("invalid journey build identity")
        if self._journey_identity is not None and self._journey_identity != identity:
            raise ValueError("journey runtime identity already bound")
        self._journey_identity = identity

    def __enter__(self) -> SqliteTransactionStore:
        self._require_open()
        return self

    def _bind_new_destination(
        self, case_token: str, attempt: int, request: LetterRequest, prepared_at: datetime
    ) -> None:
        # Called inside prepare's write transaction, never for a pre-existing row.
        case = self.get_case(case_token)
        intake = None if case is None else self.intake_for_workspace(case.workspace_token)
        if intake is None or intake.case_token != case_token:
            return
        self._bind_prepared_route(case_token, attempt, intake, request)
        history = destination_decisions_snapshot(
            self._require_open(), intake.intake_id, intake.owner
        )
        if not history:
            return
        saved = history[-1]
        if saved.decision.address != request.recipient:
            # The existing final submission gate refuses this request. It has no
            # matching instruction to bind, and must never gain an exclusion.
            return
        if saved.decision.decided_at > prepared_at:
            raise PreparationConflict("destination_decision_stale")
        self._require_open().execute(
            "INSERT INTO pb_mail_destination_bindings VALUES (?, ?, ?, ?, ?, ?)",
            (
                case_token,
                attempt,
                intake.intake_id,
                intake.owner,
                saved.revision,
                request_fingerprint(request),
            ),
        )

    def frozen_destination(self, case_token: str, attempt: int) -> DestinationDecision | None:
        """Resolve the exact append-only decision revision frozen with this request.

        No latest-decision fallback: missing bindings, including all legacy attempts,
        remain missing even if a customer instruction or correction is written later.
        """
        database = self._require_open()
        owns_transaction = not database.in_transaction
        try:
            if owns_transaction:
                database.execute("BEGIN")
            row = database.execute(
                "SELECT intake_id, owner, decision_revision, request_sha256 "
                "FROM pb_mail_destination_bindings WHERE case_token=? AND attempt=?",
                (case_token, attempt),
            ).fetchone()
            decision = None
            if row is not None:
                intake_id, owner, revision, fingerprint = row
                case = self.get_case(case_token)
                intake = None if case is None else self.intake_for_workspace(case.workspace_token)
                prepared = self.get(case_token, attempt)
                if (
                    intake is None
                    or (intake.intake_id, intake.owner, intake.case_token)
                    != (intake_id, owner, case_token)
                    or type(revision) is not int
                    or revision < 1
                    or prepared is None
                    or fingerprint != request_fingerprint(prepared.request)
                ):
                    raise StorageCorruption("invalid frozen destination binding")
                history = destination_decisions_snapshot(database, intake_id, owner)
                if revision > len(history):
                    raise StorageCorruption("missing frozen destination revision")
                decision = history[revision - 1].decision
                if (
                    decision.address != prepared.request.recipient
                    or decision.decided_at > prepared.prepared_at
                ):
                    raise StorageCorruption("frozen destination differs from request")
            if owns_transaction:
                database.execute("COMMIT")
            return decision
        except Exception:
            if owns_transaction and database.in_transaction:
                rollback_preserving_error(database)
            raise

    def returned_mail_resolution(
        self, case_token: str, attempt: int
    ) -> ReturnedMailResolution | None:
        row = (
            self._require_open()
            .execute(
                "SELECT responsibility, reason, kind, refund_cents, remails, resolved_at, "
                "cost_is_estimate "
                "FROM pb_return_remedies WHERE case_token=? AND attempt=?",
                (case_token, attempt),
            )
            .fetchone()
        )
        if row is None:
            return None
        try:
            responsibility, reason, kind, amount, remails, resolved_at, cost_basis = row
            if (
                (
                    cost_basis is not None
                    and (type(cost_basis) is not int or cost_basis not in (0, 1))
                )
                or type(amount) is not int
                or amount < 0
                or type(remails) is not int
                or remails not in (0, 1)
                or not isinstance(reason, str)
                or not reason
                or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for c in reason)
            ):
                raise ValueError("invalid remedy values")
            remedy = (
                None
                if kind is None
                else Remedy(
                    RemedyKind(kind), amount, bool(remails) and kind == RemedyKind.PROOF_PACKET
                )
            )
            if (remedy is None and (amount or remails)) or (
                remedy is not None and remedy.remails != bool(remails)
            ):
                raise ValueError("inconsistent remedy")
            return ReturnedMailResolution(
                attempt,
                Responsibility(responsibility),
                reason,
                remedy,
                _record_datetime(resolved_at, "remedy resolution"),
                None if cost_basis is None else bool(cost_basis),
            )
        except (ValueError, TypeError) as exc:
            raise StorageCorruption("invalid returned-mail resolution") from exc

    def resolve_returned_mail(
        self,
        case_token: str,
        expected_revision: int,
        responsibility: ResponsibilityDecision,
        remedy: Remedy | None,
        remail_request: LetterRequest | None,
        *,
        now: datetime,
        cost_is_estimate: bool | None = None,
        remail_manifest: ApprovalManifest | None = None,
        expected_research: tuple[str, int] | None = None,
    ) -> None:
        """Atomically save one remedy, its frozen re-mail or refund intent, and transition.

        Policy is supplied by the caller. No provider operation runs in this transaction.
        A restart sees either the complete resolution or the unchanged RETURNED case.
        """
        now = _aware(now, "now")
        if cost_is_estimate is not None and type(cost_is_estimate) is not bool:
            raise ValueError("invalid returned-mail cost basis")
        database = self._require_open()
        database.execute("BEGIN IMMEDIATE")
        try:
            case = self.get_case(case_token)
            if case is None:
                raise RevisionConflict("missing returned case")
            if (
                remail_manifest is None
                and self.returned_mail_resolution(case_token, case.attempt) is not None
            ):
                database.execute("COMMIT")
                return
            if case.revision != expected_revision or case.letter_state is not CaseState.RETURNED:
                raise RevisionConflict("returned case revision is stale")
            if now < max(r.occurred_at for r in self._iter_transition_history(case_token)):
                raise ValueError("remedy precedes returned-mail evidence")
            remails = remedy is not None and remedy.remails
            if remails != (remail_request is not None) or remails != (remail_manifest is not None):
                raise PreparationConflict("re-mail requires its request and explicit approval")
            if remail_request is not None:
                if case.attempt != 1:
                    raise ValueError("re-mail already used")
                prepared_attempt = self.prepare(
                    case_token, 2, remail_request, now, expected_research=expected_research
                )
                binding = self.frozen_destination(case_token, 2)
                first = self.get(case_token, 1)
                if (
                    binding is None
                    or binding.provenance is not AddressProvenance.SYSTEM_VERIFIED
                    or first is None
                    or binding.address == first.request.recipient
                ):
                    raise PreparationConflict("re-mail requires a new verified destination")
                assert remail_manifest is not None
                verify_remail_approval(
                    self.get_approval_manifest(case_token, 1),
                    remail_manifest,
                    prepared_attempt.prepared_at,
                )
                verify_dispatch_integrity(remail_manifest, remail_request)
                self.record_approval_manifest(remail_manifest, manage_transaction=False)
                event: Event = RemailRequested()
            else:
                if remedy is not None and remedy.refund_cents:
                    case = self.request_returned_mail_refund(
                        case_token, remedy.kind, remedy.refund_cents, now=now
                    )
                event = ReturnClosed()
            self._apply_event(
                case_token,
                event,
                case.revision,
                True,
                now,
                (mutation_key("return-remedy", case_token, "remail", str(case.attempt)),)
                if remails
                else (),
                manage_transaction=False,
            )
            database.execute(
                "INSERT INTO pb_return_remedies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    case_token,
                    case.attempt,
                    responsibility.responsibility,
                    responsibility.reason,
                    None if remedy is None else remedy.kind,
                    0 if remedy is None else remedy.refund_cents,
                    int(remails),
                    now.isoformat(),
                    None if cost_is_estimate is None else int(cost_is_estimate),
                ),
            )
            self.returned_mail_resolution(case_token, case.attempt)
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def record_counter(self, request_token: str, key: CounterKey, *, occurred_at: datetime) -> None:
        """Intake/research adapter seam; the token identifies one submitted request.

        Use the same opaque token for its eventual workspace, across retries and new
        authorizations. Saving an unsubmitted workspace alone does not count a request.
        Completion comes only from the confirmed submission lifecycle below.
        """
        request_token = _token(request_token, "request_token")
        occurred_at = _aware(occurred_at, "occurred_at")
        if key.milestone is CounterMilestone.APPEAL_COMPLETED:
            raise ValueError("completion requires confirmed Lob acceptance")
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            self._queue_counter(
                request_token,
                replace(
                    key,
                    milestone=CounterMilestone.REQUEST_RECEIVED,
                    jurisdiction_exclusion=None,
                ),
                occurred_at,
            )
            self._queue_counter(request_token, key, occurred_at)
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def record_digest(
        self, request_token: str, digest: LogDigest, *, occurred_at: datetime
    ) -> None:
        """Persist the research digest's cumulative counters, without reading analytics."""
        request_token = _token(request_token, "request_token")
        occurred_at = _aware(occurred_at, "occurred_at")
        keys = counter_keys(digest)
        if any(key.milestone is CounterMilestone.APPEAL_COMPLETED for key in keys):
            raise ValueError("completion requires confirmed Lob acceptance")
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            for key in keys:
                self._queue_counter(request_token, key, occurred_at)
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def _queue_counter(self, request_token: str, key: CounterKey, occurred_at: datetime) -> None:
        # These are operational outbox writes, never reads of coverage history or rates.
        self._require_open().execute(
            "INSERT INTO pb_pending_effects "
            "(effect_id, machine, effect, created_at, request_token, counter_json) "
            "VALUES (?, 'counter', ?, ?, ?, ?) "
            "ON CONFLICT(request_token, effect) DO NOTHING",
            (
                uuid.uuid4().hex,
                key.milestone,
                occurred_at.isoformat(),
                request_token,
                key.to_json(),
            ),
        )

    def _queue_lifecycle_counter(
        self,
        request_token: str,
        milestone: CounterMilestone,
        occurred_at: datetime,
        *,
        outcome: DigestOutcome = "SERVICEABLE",
        hard_stop: HardStop | None = None,
    ) -> None:
        # Reuse known dimensions from the operational outbox. No analytics SELECT and
        # no coverage-dependent decision: this only copies categories onto a new effect.
        row = (
            self._require_open()
            .execute(
                "SELECT counter_json FROM pb_pending_effects "
                "WHERE request_token=? ORDER BY rowid DESC LIMIT 1",
                (request_token,),
            )
            .fetchone()
        )
        previous = CounterKey() if row is None else decode_counter_key(row[0])
        key = replace(
            previous,
            milestone=milestone,
            outcome=outcome,
            hard_stop=hard_stop,
            jurisdiction_exclusion=None,
        )
        self._queue_counter(request_token, key, occurred_at)

    def flush_counters(self, *, occurred_at: datetime, limit: int = _COUNTER_FLUSH_LIMIT) -> bool:
        """Retryable outbox delivery. Analytics INSERT only; failure leaves work pending.

        Dispatch separately from the appeal, so unavailable analytics cannot prevent
        research, acceptance or payment. Event tokens are random outbox IDs, not case or
        request tokens, and replayed delivery is an insert-only no-op. At most ``limit``
        oldest pending rows are delivered per call (one transaction); call again for the rest.
        """
        occurred_at = _aware(occurred_at, "occurred_at")
        _check_limit(limit)
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            rows = database.execute(
                "SELECT effect_id, counter_json, created_at FROM pb_pending_effects "
                "WHERE machine='counter' AND completed_at IS NULL ORDER BY rowid LIMIT ?",
                (limit,),
            ).fetchall()
            for effect_id, payload, created in rows:
                if occurred_at < _record_datetime(created, "counter creation"):
                    raise ValueError("counter delivery cannot precede creation")
                key = decode_counter_key(payload)
                database.execute(
                    "INSERT OR IGNORE INTO pb_coverage_counters VALUES (?, ?)",
                    (effect_id, key.to_json()),
                )
                database.execute(
                    "UPDATE pb_pending_effects SET claimed_at=?, outcome='emitted', completed_at=? "
                    "WHERE effect_id=?",
                    (occurred_at.isoformat(), occurred_at.isoformat(), effect_id),
                )
            database.execute("COMMIT")
            return True
        except sqlite3.Error:
            if database.in_transaction:
                rollback_preserving_error(database)
            return False
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def create_owned_workspace(
        self,
        owner: str,
        workspace_token: str,
        workspace: Workspace,
        now: datetime,
        *,
        create_new: bool = False,
        parent_workspace: str | None = None,
    ) -> WorkspaceRecord:
        """Create the workspace and its owner atomically.

        By default one owner workspace is replayed, so a repeated start never forks a
        second transaction. ``create_new`` appends a further workspace to the same owner;
        the caller only does that after the previous transaction is finished, which is how
        a returning customer buys another appeal. When ``parent_workspace`` is given, the
        restart happens only while ``parent_workspace`` is still the owner's latest
        workspace (replaying the concurrent winner otherwise) and records lineage atomically.
        """
        if not owner.strip():
            raise ValueError("owner must not be blank")
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            existing = database.execute(
                "SELECT workspace_token FROM pb_intake_owners WHERE owner=? "
                "ORDER BY created_at DESC, rowid DESC",
                (owner,),
            ).fetchall()
            latest = str(existing[0][0]) if existing else None
            should_create = not existing or (
                create_new and (parent_workspace is None or latest == parent_workspace)
            )
            if should_create:
                created = self._save_workspace_in_transaction(workspace_token, workspace, now, None)
                self._insert_workspace_owner_in_transaction(workspace_token, owner, now)
                if parent_workspace is not None and parent_workspace != workspace_token:
                    database.execute(
                        "INSERT INTO pb_workspace_lineage "
                        "(child_workspace, parent_workspace, reason, recorded_at) "
                        "VALUES (?, ?, 'restart', ?)",
                        (workspace_token, parent_workspace, now.astimezone(UTC).isoformat()),
                    )
                database.execute("COMMIT")
                return created
            assert latest is not None
            replay = self.get_workspace(latest)
            if replay is None:
                raise StorageCorruption("workspace owner has no workspace")
            # A restart carries the earlier notes when the caller sends none, so
            # an empty request body means "no preference" and must replay.
            if workspace.notes and replay.workspace.notes != workspace.notes:
                raise IntakeConflict("appeal_already_started")
            database.execute("COMMIT")
            return replay
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def _insert_workspace_owner_in_transaction(
        self, workspace_token: str, owner: str, now: datetime
    ) -> None:
        database = self._require_open()
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

    def save_workspace(
        self,
        workspace_token: str,
        workspace: Workspace,
        now: datetime,
        expected_revision: int | None = None,
    ) -> WorkspaceRecord:
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            saved = self._save_workspace_in_transaction(
                workspace_token, workspace, now, expected_revision
            )
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
        # A later writer can commit before a post-commit reload. Acknowledge this
        # exact saved revision, using a decoded copy rather than caller-owned data.
        return saved

    def _save_workspace_in_transaction(
        self,
        workspace_token: str,
        workspace: Workspace,
        now: datetime,
        expected_revision: int | None,
    ) -> WorkspaceRecord:
        """Write a workspace and history row inside the caller's transaction."""
        workspace_token = _token(workspace_token, "workspace_token")
        now = _aware(now, "now")
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer or None")
        payload = workspace_json(workspace)
        database = self._require_open()
        row = database.execute(
            "SELECT workspace_token, payload_json, updated_at, revision "
            "FROM pb_workspaces WHERE workspace_token=?",
            (workspace_token,),
        ).fetchone()
        if expected_revision is None:
            if row is not None:
                raise RevisionConflict("workspace already exists")
            for _ in self._history_records(workspace_token, None):
                pass
            revision = 1
            database.execute(
                "INSERT INTO pb_workspaces VALUES (?, ?, ?, ?)",
                (workspace_token, payload, now.isoformat(), revision),
            )
        else:
            if row is None:
                raise RevisionConflict("workspace does not exist")
            current = _workspace_record(row)
            if current.revision != expected_revision:
                raise RevisionConflict("workspace revision is stale")
            for _ in self._history_records(workspace_token, current):
                pass
            if now < current.updated_at:
                raise ValueError("workspace timestamp cannot precede its previous update")
            revision = current.revision + 1
            database.execute(
                "UPDATE pb_workspaces SET payload_json=?, updated_at=?, revision=? "
                "WHERE workspace_token=? AND revision=?",
                (payload, now.isoformat(), revision, workspace_token, expected_revision),
            )
        if revision == 1:
            ensure_journey_identity_in_transaction(
                database,
                workspace_token=workspace_token,
                at=now,
                basis=CaptureStartBasis.WORKSPACE_CREATED,
                identity=self._journey_identity,
            )
        database.execute(
            "INSERT INTO pb_workspace_history VALUES (?, ?, ?, ?)",
            (workspace_token, revision, payload, now.isoformat()),
        )
        return WorkspaceRecord(workspace_token, decode_workspace(payload), now, revision)

    def get_workspace(self, workspace_token: str) -> WorkspaceRecord | None:
        workspace_token = _token(workspace_token, "workspace_token")
        database = self._require_open()
        try:
            row = database.execute(
                "SELECT workspace_token, payload_json, updated_at, revision "
                "FROM pb_workspaces WHERE workspace_token=?",
                (workspace_token,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageCorruption(f"cannot load workspace: {exc}") from exc
        return None if row is None else _workspace_record(row)

    def _history_records(
        self, workspace_token: str, current: WorkspaceRecord | None
    ) -> Iterator[WorkspaceRecord]:
        """Stream and validate complete history inside the caller's transaction."""
        database = self._require_open()
        cursor = database.execute(
            "SELECT workspace_token, payload_json, updated_at, revision "
            "FROM pb_workspace_history WHERE workspace_token=? ORDER BY revision",
            (workspace_token,),
        )
        last: WorkspaceRecord | None = None
        expected_revision = 1
        for row in cursor:
            record = _workspace_record(row)
            if record.workspace_token != workspace_token:
                raise StorageCorruption("workspace history token mismatch")
            if record.revision != expected_revision:
                raise StorageCorruption("workspace history revisions are incomplete")
            if last is not None and record.updated_at < last.updated_at:
                raise StorageCorruption("workspace history timestamps move backwards")
            last = record
            expected_revision += 1
            yield record
        if current is None and last is not None:
            raise StorageCorruption("workspace history exists without a current record")
        if current is not None and last != current:
            raise StorageCorruption("workspace history does not match current record")

    def _history_snapshot(
        self, workspace_token: str, current: WorkspaceRecord | None
    ) -> tuple[WorkspaceRecord, ...]:
        """Read and validate history inside the caller's read or write transaction."""
        return tuple(self._history_records(workspace_token, current))

    def workspace_history(self, workspace_token: str) -> tuple[WorkspaceRecord, ...]:
        workspace_token = _token(workspace_token, "workspace_token")
        database = self._require_open()
        try:
            # Both queries must observe one SQLite snapshot while other writers run.
            database.execute("BEGIN")
            current = self.get_workspace(workspace_token)
            records = self._history_snapshot(workspace_token, current)
            database.execute("COMMIT")
            return records
        except sqlite3.Error as exc:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise StorageCorruption(f"cannot load workspace history: {exc}") from exc
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def start_case(
        self,
        workspace_token: str,
        authorization_ref: str,
        authorized_at: datetime,
        authorization_expires_at: datetime,
        amount_cents: int = CERTIFIED_CENTS,
    ) -> CaseRecord:
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            result = self._start_case_in_transaction(
                workspace_token,
                authorization_ref,
                authorized_at,
                authorization_expires_at,
                amount_cents,
            )
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
        return result

    def start_beta_case(
        self,
        intake_id: str,
        disclosure_sha256: str,
        now: datetime,
    ) -> CaseRecord:
        """Consume one selected entitlement and service consent, with no financial hold.

        For NO_CHARGE only, the legacy authorization columns identify beta service
        consent and its existing customer-work window, never a Stripe authorization.
        """
        now = _aware(now, "now")
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            entitlement = self._beta_acceptance(intake_id, disclosure_sha256, now)
            intake = self.get_intake(intake_id)
            if intake is None:
                raise BetaConflict("beta_entitlement_required")
            if entitlement.case_token is not None:
                case = self.get_case(entitlement.case_token)
                if case is None or intake.case_token != case.case_token:
                    raise StorageCorruption("invalid beta case binding")
                database.execute("COMMIT")
                return case
            offer = json.loads(intake.prehold_json)
            if entitlement.pilot_scope is None and not any(
                isinstance(tier, dict) and tier.get("tier") == "CERTIFIED"
                for tier in offer.get("tiers", [])
            ):
                raise BetaConflict("beta_certified_offer_required")
            case = self._start_case_in_transaction(
                intake.workspace_token,
                f"beta_{entitlement.entitlement_id}",
                now,
                now + DEFAULT_WINDOW,
                0,
                no_charge=True,
            )
            changed = database.execute(
                "UPDATE pb_intake_records SET selected_tier=?, case_token=?, "
                "revision=revision+1 WHERE intake_id=? AND case_token IS NULL AND revision=?",
                (entitlement.tier, case.case_token, intake_id, intake.revision),
            )
            if changed.rowcount != 1:
                raise BetaConflict("beta_intake_changed")
            database.execute(
                "UPDATE pb_beta_entitlements SET accepted_at=?, disclosure_sha256=?, "
                "case_token=? WHERE intake_id=? AND case_token IS NULL",
                (now.isoformat(), entitlement.disclosure_sha256, case.case_token, intake_id),
            )
            database.execute("COMMIT")
            return case
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def _start_case_in_transaction(
        self,
        workspace_token: str,
        authorization_ref: str,
        authorized_at: datetime,
        authorization_expires_at: datetime,
        amount_cents: int,
        *,
        no_charge: bool = False,
    ) -> CaseRecord:
        workspace_token = _token(workspace_token, "workspace_token")
        authorization_ref = _token(authorization_ref, "authorization_ref")
        authorized_at = _aware(authorized_at, "authorized_at")
        authorization_expires_at = _aware(authorization_expires_at, "authorization_expires_at")
        if authorization_expires_at <= authorized_at:
            raise ValueError("authorization expiry must follow authorization time")
        if no_charge:
            if (
                type(amount_cents) is not int
                or amount_cents != 0
                or not authorization_ref.startswith("beta_")
            ):
                raise ValueError("invalid no-charge service authorization")
        elif (
            type(amount_cents) is not int
            or amount_cents not in prices.ALLOWED_PRICE_CENTS
            or authorization_ref.startswith("beta_")
        ):
            raise ValueError("amount_cents must be an allowed service price")
        database = self._require_open()
        workspace = database.execute(
            "SELECT workspace_token, payload_json, updated_at, revision "
            "FROM pb_workspaces WHERE workspace_token=?",
            (workspace_token,),
        ).fetchone()
        if workspace is None:
            raise ValueError("workspace must exist before starting a case")
        _workspace_record(workspace)
        existing = database.execute(
            """SELECT case_token, workspace_token, authorization_ref, authorized_at,
                      authorization_expires_at, amount_cents, letter_state, payment_state,
                      attempt, revision FROM pb_cases WHERE authorization_ref=?""",
            (authorization_ref,),
        ).fetchone()
        if existing is not None:
            record = _case_record(existing)
            if (
                record.workspace_token != workspace_token
                or record.authorized_at != authorized_at
                or record.authorization_expires_at != authorization_expires_at
                or record.amount_cents != amount_cents
            ):
                raise CaseConflict("authorization reference is already bound to another case")
            return record

        case_token = uuid.uuid4().hex
        database.execute(
            """INSERT INTO pb_cases
               (case_token, workspace_token, authorization_ref, authorized_at,
                authorization_expires_at, amount_cents, letter_state, payment_state,
                attempt, revision)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1)""",
            (
                case_token,
                workspace_token,
                authorization_ref,
                authorized_at.isoformat(),
                authorization_expires_at.isoformat(),
                amount_cents,
                CaseState.RESEARCHING.value,
                PaymentState.NO_CHARGE.value if no_charge else PaymentState.AUTHORIZED.value,
            ),
        )
        self._insert_ops_id(case_token)
        self._insert_support_code(case_token, authorized_at)
        self._queue_counter(workspace_token, CounterKey(), authorized_at)
        result = database.execute(
            """SELECT case_token, workspace_token, authorization_ref, authorized_at,
                      authorization_expires_at, amount_cents, letter_state, payment_state,
                      attempt, revision FROM pb_cases WHERE case_token=?""",
            (case_token,),
        ).fetchone()
        if result is None:
            raise StorageCorruption("case disappeared inside transaction")
        return _case_record(result)

    def complete_authorization_attempt(
        self,
        intake_id: str,
        idempotency_key: str,
        authorization_ref: str,
        authorized_at: datetime,
        authorization_expires_at: datetime,
    ) -> CaseRecord:
        """Link a confirmed hold, case row and intent outcome in one SQLite commit."""
        authorization_ref = _token(authorization_ref, "authorization_ref")
        authorized_at = _aware(authorized_at, "authorized_at")
        authorization_expires_at = _aware(authorization_expires_at, "authorization_expires_at")
        if authorization_expires_at <= authorized_at:
            raise IntakeConflict("authorization_outcome_unresolved")
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            intake = self.get_intake(intake_id)
            attempt = self.get_authorization_attempt(intake_id, idempotency_key)
            if intake is None:
                raise IntakeConflict("intake_not_found")
            if attempt is None:
                raise IntakeConflict("authorization_attempt_not_found")
            if attempt.tier != intake.selected_tier or attempt.amount_cents <= 0:
                raise IntakeConflict("authorization_identity_changed")
            if authorized_at < attempt.updated_at:
                raise IntakeConflict("authorization_outcome_unresolved")
            if attempt.authorization_ref not in {None, authorization_ref}:
                raise IntakeConflict("authorization_reference_changed")

            if intake.case_token is not None:
                case = self.get_case(intake.case_token)
                if case is None:
                    raise StorageCorruption("intake points to a missing case")
                if (
                    case.workspace_token != intake.workspace_token
                    or case.authorization_ref != authorization_ref
                    or case.amount_cents != attempt.amount_cents
                ):
                    raise IntakeConflict("authorization_identity_changed")
                if attempt.state != "authorized":
                    database.execute(
                        "UPDATE pb_intake_authorization_attempts SET state='authorized', "
                        "authorization_ref=?, authorized_at=?, authorization_expires_at=?, "
                        "updated_at=?, revision=? WHERE intake_id=? AND idempotency_key=?",
                        (
                            authorization_ref,
                            case.authorized_at.isoformat(),
                            case.authorization_expires_at.isoformat(),
                            max(authorized_at, attempt.updated_at).isoformat(),
                            attempt.revision + 1,
                            intake_id,
                            idempotency_key,
                        ),
                    )
                database.execute("COMMIT")
                return case

            if attempt.state not in {"pending", "uncertain"}:
                raise IntakeConflict("authorization_attempt_not_pending")
            if intake.selected_tier != attempt.tier:
                raise IntakeConflict("authorization_tier_changed")
            case = self._start_case_in_transaction(
                intake.workspace_token,
                authorization_ref,
                authorized_at,
                authorization_expires_at,
                attempt.amount_cents,
            )
            cursor = database.execute(
                "UPDATE pb_intake_records SET case_token=?, revision=? "
                "WHERE intake_id=? AND revision=? AND case_token IS NULL AND selected_tier=?",
                (
                    case.case_token,
                    intake.revision + 1,
                    intake_id,
                    intake.revision,
                    attempt.tier,
                ),
            )
            if cursor.rowcount != 1:
                raise IntakeConflict("intake_revision_stale")
            database.execute(
                "UPDATE pb_intake_authorization_attempts SET state='authorized', "
                "authorization_ref=?, authorized_at=?, authorization_expires_at=?, "
                "updated_at=?, revision=? WHERE intake_id=? AND idempotency_key=? "
                "AND revision=? AND state IN ('pending', 'uncertain')",
                (
                    authorization_ref,
                    authorized_at.isoformat(),
                    authorization_expires_at.isoformat(),
                    authorized_at.isoformat(),
                    attempt.revision + 1,
                    intake_id,
                    idempotency_key,
                    attempt.revision,
                ),
            )
            row = database.execute(
                """SELECT case_token, workspace_token, authorization_ref, authorized_at,
                          authorization_expires_at, amount_cents, letter_state, payment_state,
                          attempt, revision FROM pb_cases WHERE case_token=?""",
                (case.case_token,),
            ).fetchone()
            if row is None:
                raise StorageCorruption("authorized case disappeared inside transaction")
            result = _case_record(row)
            if result.payment_state is not PaymentState.NO_CHARGE and result.amount_cents > 0:
                append_journey_event_in_transaction(
                    database,
                    workspace_token=result.workspace_token,
                    occurrence_key=f"hold_authorized:{result.case_token}",
                    event=JourneyEventInput(
                        kind=JourneyEventKind.HOLD_AUTHORIZED,
                        outcome=JourneyEventOutcome.SUCCEEDED,
                        occurred_at=result.authorized_at,
                        cost_status=CostStatus.NOT_APPLICABLE,
                        context=JourneyEventContext(
                            operation=ContextOperation.PAYMENT_AUTHORIZATION,
                            build_sha256=self.journey_identity.build_sha256,
                            config_sha256=self.journey_identity.config_sha256,
                            reason=ContextReason.NOT_APPLICABLE,
                            funding=ContextFunding.PAID,
                            duration_basis=DurationBasis.NOT_APPLICABLE,
                            operation_ref=opaque_reference(
                                "op", f"payment_authorization:{intake_id}"
                            ),
                            attempt_ref=opaque_reference(
                                "att", f"payment_authorization:{idempotency_key}"
                            ),
                            causation_ref=prior_journey_event_reference_in_transaction(
                                database,
                                workspace_token=result.workspace_token,
                                occurrence_key=f"intake_submitted:{intake_id}",
                            ),
                        ),
                    ),
                )
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
        return result

    def get_case(self, case_token: str) -> CaseRecord | None:
        case_token = _token(case_token, "case_token")
        database = self._require_open()
        try:
            row = database.execute(
                """SELECT case_token, workspace_token, authorization_ref, authorized_at,
                          authorization_expires_at, amount_cents, letter_state, payment_state,
                          attempt, revision FROM pb_cases WHERE case_token=?""",
                (case_token,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageCorruption(f"cannot load case: {exc}") from exc
        if row is None:
            return None
        record = _case_record(row)
        if self.get_workspace(record.workspace_token) is None:
            raise StorageCorruption("case references a missing workspace")
        if record.payment_state is PaymentState.NO_CHARGE:
            entitlement = self.beta_case_entitlement(case_token)
            if (
                entitlement is None
                or record.authorization_ref != f"beta_{entitlement.entitlement_id}"
            ):
                raise StorageCorruption("no-charge case lacks its beta entitlement")
        return record

    def prepare(
        self,
        case_token: str,
        attempt: int,
        request: LetterRequest,
        prepared_at: datetime,
        *,
        expected_research: tuple[str, int] | None = None,
    ) -> PreparedMailAttempt:
        case = self.get_case(case_token)
        if case is not None and case.payment_state is PaymentState.NO_CHARGE and attempt != 1:
            raise PreparationConflict("beta entitlement permits one distinct submission")
        return super().prepare(
            case_token, attempt, request, prepared_at, expected_research=expected_research
        )

    def apply_letter_event(
        self,
        case_id: str,
        event: Event,
        expected_revision: int,
        *,
        occurred_at: datetime,
        effect_ids: tuple[str, ...] = (),
        new_effect_id: Callable[[], str] | None = None,
    ) -> CaseRecord:
        """new_effect_id, when given instead of effect_ids, names each emitted effect."""
        return self._apply_event(
            case_id,
            event,
            expected_revision,
            True,
            occurred_at,
            effect_ids,
            new_effect_id=new_effect_id,
        )

    def stop_unsent_expired_authorization(
        self,
        case_id: str,
        expected_revision: int,
        submit_effect_id: str,
        *,
        now: datetime,
        new_effect_id: Callable[[], str],
    ) -> CaseRecord:
        """Close an unsent queued first mail and stop without a possible-send cursor."""
        now = _aware(now, "now")
        if not self._submission_locked:
            raise RevisionConflict("requires submission ownership")
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            current = self.get_case(case_id)
            queued = self.pending_effects(case_id, kinds=(Effect.SUBMIT_LETTER,))
            if (
                current is None
                or current.letter_state is not CaseState.SUBMITTING
                or current.attempt != 1
                or current.payment_state is not PaymentState.AUTHORIZED
                or current.revision != expected_revision
                or self.submission_cursor(case_id, 1) is not None
                or len(queued) != 1
                or queued[0].effect_id != submit_effect_id
            ):
                raise RevisionConflict("first mail is no longer queued and unsent")
            result = self._apply_event(
                case_id,
                AuthorizationExpired(),
                expected_revision,
                True,
                now,
                (),
                manage_transaction=False,
                new_effect_id=new_effect_id,
            )
            if queued[0].claimed_at is None:
                self._update_effect(submit_effect_id, now, None, manage_transaction=False)
            self._update_effect(
                submit_effect_id, now, "authorization_unusable_no_send", manage_transaction=False
            )
            database.execute("COMMIT")
            return result
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def apply_payment_event(
        self,
        case_id: str,
        event: PaymentEvent,
        expected_revision: int,
        *,
        occurred_at: datetime,
        effect_ids: tuple[str, ...] = (),
    ) -> CaseRecord:
        return self._apply_event(case_id, event, expected_revision, False, occurred_at, effect_ids)

    def returned_mail_refund(self, case_id: str) -> ReturnedMailRefundRequested | None:
        found: TransitionRecord | None = None
        for row in self._iter_transition_history(case_id):
            if row.machine == "payment" and row.event_name == "ReturnedMailRefundRequested":
                if found is not None:
                    raise StorageCorruption("multiple returned-mail refund intents")
                found = row
        return (
            None if found is None else ReturnedMailRefundRequested(**json.loads(found.event_json))
        )

    def failed_remail_refund_candidates(self) -> Iterator[str]:
        """Stream the intake ids whose failed free re-mail can still take its refund intent.

        A closed case can still act only with attempt 2, a definitive failed re-mail
        submission (Rejected/NotSent/Canceled, never Accepted or Uncertain), a captured
        payment and no returned-mail refund intent yet. Settled or waiting closed cases
        are never read (worker sweep).
        """
        rows = (
            self._require_open()
            .execute(
                "SELECT r.intake_id FROM pb_intake_records r "
                "JOIN pb_cases c ON c.case_token = r.case_token "
                "WHERE c.letter_state = ? AND c.attempt = 2 AND c.payment_state = ? "
                "AND EXISTS (SELECT 1 FROM pb_submission_cursors s "
                "WHERE s.case_token = c.case_token AND s.attempt = 2 "
                "AND json_extract(s.outcome_json, '$.kind') "
                "IN ('rejected', 'not_sent', 'canceled')) "
                "AND NOT EXISTS (SELECT 1 FROM pb_case_history h "
                "WHERE h.case_token = c.case_token AND h.machine = 'payment' "
                "AND h.event_name = 'ReturnedMailRefundRequested') "
                "ORDER BY r.created_at",
                (CaseState.CLOSED_RETURNED.value, PaymentState.CAPTURED.value),
            )
            .fetchall()
        )
        # Materialized first: the sweep writes on this connection while it iterates, and
        # SQLite leaves a scan undefined when its tables change mid-read. The candidate
        # set is small by construction (only cases that still need their refund intent).
        for row in rows:
            yield str(row[0])

    def request_returned_mail_refund(
        self, case_id: str, kind: RemedyKind, amount_cents: int, *, now: datetime
    ) -> CaseRecord:
        """Persist one exact refund intent/outbox row; no provider call or remedy decision.

        Identical retries return the current case, including after settlement. The
        caller in step 2 owns responsibility binding and choosing the clerical remedy.
        """
        now = _aware(now, "now")
        database = self._require_open()
        owns_transaction = not database.in_transaction
        try:
            if owns_transaction:
                database.execute("BEGIN IMMEDIATE")
            case = self._payment_case(case_id)
            request = ReturnedMailRefundRequested(kind, amount_cents, case.attempt)
            if not 1 <= amount_cents <= case.amount_cents or (
                kind == "FULL_REFUND" and amount_cents != case.amount_cents
            ):
                raise ValueError("returned-mail refund differs from case amount")
            previous_request = self.returned_mail_refund(case_id)
            if previous_request is not None:
                if request != previous_request:
                    raise ValueError("returned-mail refund intent changed")
                if owns_transaction:
                    database.execute("COMMIT")
                return case
            cursor = self.payment_cursor(case_id)
            if (
                case.letter_state not in {CaseState.RETURNED, CaseState.CLOSED_RETURNED}
                or case.payment_state is not PaymentState.CAPTURED
                or cursor is None
                or cursor.kind != "capture"
                or not cursor.done
                or cursor.outcome.status != "succeeded"
                or cursor.outcome.amount_cents != case.amount_cents
                or not cursor.outcome.full_object
                or cursor.outcome.evidence_version != 1
                or cursor.outcome.charge_ref != cursor.outcome.original_charge_ref
            ):
                raise ValueError("returned-mail refund requires confirmed original capture")
            latest = cursor.next_at
            for row in self._iter_transition_history(case_id):
                latest = max(latest, row.occurred_at)
            if now < latest:
                raise ValueError("returned-mail refund request precedes case/payment evidence")
            effect_id = mutation_key("returned-mail", case_id, kind, str(case.attempt))
            case = self._apply_event(
                case_id,
                request,
                case.revision,
                False,
                now,
                (effect_id,),
                manage_transaction=False,
            )
            if owns_transaction:
                database.execute("COMMIT")
            return case
        except Exception:
            if owns_transaction and database.in_transaction:
                rollback_preserving_error(database)
            raise

    def _apply_event(
        self,
        case_id: str,
        event: Event | PaymentEvent,
        expected_revision: int,
        letter: bool,
        occurred_at: datetime,
        effect_ids: tuple[str, ...],
        *,
        manage_transaction: bool = True,
        new_effect_id: Callable[[], str] | None = None,
    ) -> CaseRecord:
        if new_effect_id is not None and effect_ids:
            raise ValueError("supply effect_ids or new_effect_id, not both")
        case_id = _token(case_id, "case_id")
        occurred_at = _aware(occurred_at, "occurred_at")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer")
        for effect_id in effect_ids:
            _token(effect_id, "effect_id")
        if len(set(effect_ids)) != len(effect_ids):
            raise ValueError("effect_ids must be unique")
        database = self._require_open()
        owns_transaction = manage_transaction
        try:
            if owns_transaction:
                database.execute("BEGIN IMMEDIATE")
            current = self.get_case(case_id)
            if current is None or current.revision != expected_revision:
                raise RevisionConflict("case revision is stale or case does not exist")
            no_charge = current.payment_state is PaymentState.NO_CHARGE
            if no_charge and (not letter or isinstance(event, RemailRequested)):
                raise ValueError("beta entitlement forbids payment and second-submission events")
            if isinstance(event, UnexpectedCaptureConfirmed) and (
                manage_transaction
                or current.letter_state is not CaseState.STOPPED_NO_CHARGE
                or event.authorization_ref != current.authorization_ref
                or event.amount_cents != current.amount_cents
            ):
                raise ValueError("unexpected capture requires stopped-case dispatcher evidence")
            if isinstance(event, ReturnedMailRefundRequested) and manage_transaction:
                raise ValueError("use durable returned-mail refund request entry")
            if isinstance(event, RefundIssued) and (
                current.amount_cents not in prices.ALLOWED_PRICE_CENTS
                or event.captured_cents != current.amount_cents
            ):
                raise ValueError("refund capture differs from case amount")
            refund_request = (
                self.returned_mail_refund(case_id) if isinstance(event, RefundIssued) else None
            )
            if (
                isinstance(event, RefundIssued)
                and refund_request is not None
                and (
                    manage_transaction
                    or event.amount_cents != refund_request.amount_cents
                    or event.captured_cents != current.amount_cents
                )
            ):
                raise ValueError("returned-mail refund requires reconciled exact intent")
            if (
                isinstance(event, RefundIssued)
                and current.letter_state is CaseState.STOPPED_NO_CHARGE
                and (
                    manage_transaction
                    or not event.is_full
                    or event.captured_cents != current.amount_cents
                )
            ):
                raise ValueError("stopped case requires reconciled full compensation")
            revision = current.revision + 1
            records: list[TransitionRecord] = []
            effects: list[tuple[str, Effect | PaymentEffect]] = []

            def record(
                kind: str,
                source: str,
                target: str,
                applied_event: Event | PaymentEvent,
                emitted: tuple[Effect, ...] | tuple[PaymentEffect, ...],
            ) -> None:
                records.append(
                    TransitionRecord(
                        case_id,
                        revision,
                        kind,
                        source,
                        target,
                        type(applied_event).__name__,
                        lifecycle_event_json(applied_event),
                        occurred_at,
                    )
                )
                effects.extend((kind, effect) for effect in emitted)

            if letter:
                t = transition(current.letter_state, cast(Event, event), current.attempt)
                result = replace(
                    current,
                    letter_state=t.target,
                    attempt=next_attempt(current.attempt, t),
                    revision=revision,
                )
                letter_effects = tuple(
                    effect
                    for effect in t.effects
                    if not no_charge or payment_event_for(effect) is None
                )
                record("letter", t.source, t.target, event, letter_effects)
                for effect in letter_effects:
                    payment_event = payment_event_for(effect)
                    if payment_event is not None:
                        pt = payment_transition(result.payment_state, payment_event)
                        result = replace(result, payment_state=pt.target)
                        record("payment", pt.source, pt.target, payment_event, pt.effects)
            else:
                pt = payment_transition(current.payment_state, cast(PaymentEvent, event))
                result = replace(current, payment_state=pt.target, revision=revision)
                record("payment", pt.source, pt.target, event, pt.effects)
            if new_effect_id is not None:
                effect_ids = tuple(_token(new_effect_id(), "effect_id") for _ in effects)
            if len(effect_ids) != len(effects):
                raise ValueError("supply one effect_id per emitted letter and payment effect")
            database.execute(
                "UPDATE pb_cases SET letter_state=?, payment_state=?, attempt=?, revision=? "
                "WHERE case_token=? AND revision=?",
                (
                    result.letter_state,
                    result.payment_state,
                    result.attempt,
                    revision,
                    case_id,
                    expected_revision,
                ),
            )
            # TODO(T04): Queue a diagnostic event here with each history row in the same commit.
            # Key it by case, revision and machine. Give each consumer a lease and checkpoint so
            # a crash can replay the event safely. Run provider lookups after the write commits.
            for item in records:
                database.execute(
                    "INSERT INTO pb_case_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.case_token,
                        item.revision,
                        item.machine,
                        item.source,
                        item.target,
                        item.event_name,
                        item.event_json,
                        item.occurred_at.isoformat(),
                    ),
                )
            for effect_id, (kind, emitted_effect) in zip(effect_ids, effects, strict=True):
                database.execute(
                    "INSERT INTO pb_pending_effects "
                    "(effect_id, case_token, revision, machine, effect, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (effect_id, case_id, revision, kind, emitted_effect, occurred_at.isoformat()),
                )
            if letter:
                if isinstance(event, ResearchComplete):
                    self._queue_lifecycle_counter(
                        current.workspace_token, CounterMilestone.TIER1_PASSED, occurred_at
                    )
                elif isinstance(event, SubmitConfirmed):
                    self._queue_lifecycle_counter(
                        current.workspace_token, CounterMilestone.APPEAL_COMPLETED, occurred_at
                    )
                elif isinstance(event, Stopped):
                    research_failure = current.letter_state in {
                        CaseState.RESEARCHING,
                        CaseState.NEEDS_CUSTOMER,
                    } and event.reason in {
                        StopReason.NOT_YET_VERIFIED,
                        StopReason.UNRESOLVABLE_FACT,
                    }
                    self._queue_lifecycle_counter(
                        current.workspace_token,
                        CounterMilestone.REQUIRED_FACTS_UNVERIFIED
                        if research_failure
                        else CounterMilestone.OTHER_EXIT,
                        occurred_at,
                        outcome=StopFamily.NOT_YET_VERIFIED
                        if research_failure
                        else stop_family(event.reason),
                        hard_stop=event.hard_stop,
                    )
                elif isinstance(
                    event,
                    WindowExpired
                    | SubmitRejected
                    | ConfirmedNotSent
                    | AuthorizationExpired
                    | TimingRefused
                    | DestinationRefused
                    | LetterNotSubmittable,
                ):
                    self._queue_lifecycle_counter(
                        current.workspace_token,
                        CounterMilestone.OTHER_EXIT,
                        occurred_at,
                        outcome=stop_family(event.reason),
                    )
            if letter and result.letter_state is CaseState.FROZEN:
                append_journey_event_in_transaction(
                    database,
                    workspace_token=current.workspace_token,
                    occurrence_key=f"packet_approved:{case_id}:{result.revision}:{result.attempt}",
                    event=JourneyEventInput(
                        kind=JourneyEventKind.PACKET_APPROVED,
                        outcome=JourneyEventOutcome.SUCCEEDED,
                        occurred_at=occurred_at,
                        cost_status=CostStatus.NOT_APPLICABLE,
                        context=JourneyEventContext(
                            operation=ContextOperation.PACKET_APPROVAL,
                            build_sha256=self.journey_identity.build_sha256,
                            config_sha256=self.journey_identity.config_sha256,
                            reason=ContextReason.NOT_APPLICABLE,
                            funding=ContextFunding.FREE_BETA if no_charge else ContextFunding.PAID,
                            duration_basis=DurationBasis.NOT_APPLICABLE,
                            operation_ref=opaque_reference(
                                "op", f"packet_approval:{case_id}:{result.attempt}"
                            ),
                            attempt_ref=opaque_reference(
                                "att",
                                f"packet_approval:{case_id}:{result.revision}:{result.attempt}",
                            ),
                            # Paid approval depends on this exact case's recorded hold.
                            # The no-charge route has no paid-hold causation event.
                            causation_ref=(
                                prior_journey_event_reference_in_transaction(
                                    database,
                                    workspace_token=current.workspace_token,
                                    occurrence_key=f"hold_authorized:{case_id}",
                                )
                                if not no_charge
                                else JourneyReference()
                            ),
                        ),
                    ),
                )
            if owns_transaction:
                database.execute("COMMIT")
            return result
        except Exception:
            if owns_transaction and database.in_transaction:
                rollback_preserving_error(database)
            raise

    def _history_rows(self, case_id: str, bound: tuple[str, tuple[int, ...]]) -> sqlite3.Cursor:
        return self._require_open().execute(
            "SELECT * FROM pb_case_history WHERE case_token=?" + bound[0],
            (_token(case_id, "case_id"), *bound[1]),
        )

    def _iter_transition_history(
        self, case_id: str, *, newest_first: bool = False
    ) -> Iterator[TransitionRecord]:
        """Stream one case's transitions (oldest first by default), one validated row at a time.

        Newest-first streaming lets a caller looking for a recent record stop at the first
        match: it reads only the rows behind that record, whatever its position.
        """
        bound = _limit_clause(None, ("revision", "machine"), newest_first=newest_first)
        for row in self._history_rows(case_id, bound):
            yield self._transition_record(row)

    @staticmethod
    def _transition_record(row: tuple[Any, ...]) -> TransitionRecord:
        return TransitionRecord(
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            _record_datetime(row[7], "transition time"),
        )

    def transition_history(
        self, case_id: str, *, limit: int | None = None, newest_first: bool = False
    ) -> tuple[TransitionRecord, ...]:
        """Transitions oldest first; ``limit`` (positive) returns only the first N rows.

        ``newest_first`` reverses the order, so with ``limit`` only the newest N rows
        are read and returned, newest of them first.
        """
        bound = _limit_clause(limit, ("revision", "machine"), newest_first=newest_first)
        return tuple(self._transition_record(row) for row in self._history_rows(case_id, bound))

    def _effect_rows(
        self,
        case_id: str,
        bound: tuple[str, tuple[int, ...]],
        *,
        incomplete: bool = False,
        kinds: tuple[Effect | PaymentEffect, ...] = (),
        revision: int | None = None,
    ) -> sqlite3.Cursor:
        predicate = ""
        parameters: tuple[str, ...] = ()
        if kinds:
            predicate = " AND (" + " OR ".join("(machine=? AND effect=?)" for _ in kinds) + ")"
            parameters = tuple(
                value
                for kind in kinds
                for value in ("letter" if isinstance(kind, Effect) else "payment", kind.value)
            )
        return self._require_open().execute(
            "SELECT effect_id, case_token, revision, machine, effect, created_at, "
            "claimed_at, outcome, completed_at "
            "FROM pb_pending_effects WHERE case_token=?"
            + (" AND completed_at IS NULL" if incomplete else "")
            + (" AND revision=?" if revision is not None else "")
            + predicate
            + bound[0],
            (
                _token(case_id, "case_id"),
                *((revision,) if revision is not None else ()),
                *parameters,
                *bound[1],
            ),
        )

    def _iter_effects(self, case_id: str) -> Iterator[PendingEffect]:
        """Stream every outbox row for one case in creation order."""
        bound = _limit_clause(None, ("rowid",))
        for row in self._effect_rows(case_id, bound):
            yield self._effect_record(row)

    def effects(
        self, case_id: str, *, limit: int | None = None, newest_first: bool = False
    ) -> tuple[PendingEffect, ...]:
        """All outbox rows, including claimed and completed effects; never delete them.

        ``limit`` (positive) returns only the first N rows in creation order; with
        ``newest_first`` those are the newest N rows, newest of them first.
        """
        bound = _limit_clause(limit, ("rowid",), newest_first=newest_first)
        return tuple(self._effect_record(row) for row in self._effect_rows(case_id, bound))

    def latest_effect(self, case_id: str) -> PendingEffect | None:
        """The newest outbox row for one case, read as a single row."""
        bound = _limit_clause(1, ("rowid",), newest_first=True)
        row = self._effect_rows(case_id, bound).fetchone()
        return None if row is None else self._effect_record(row)

    @staticmethod
    def _effect_record(row: tuple[object, ...]) -> PendingEffect:
        effect_id, case_id, revision, machine, effect, created, claimed, outcome, completed = row
        if (
            not isinstance(effect_id, str)
            or not isinstance(case_id, str)
            or type(revision) is not int
            or machine not in ("letter", "payment")
            or not isinstance(effect, str)
            or (outcome is not None and not isinstance(outcome, str))
        ):
            raise StorageCorruption("invalid stored effect")
        try:
            parsed = Effect(effect) if machine == "letter" else PaymentEffect(effect)
        except ValueError as exc:
            raise StorageCorruption("invalid stored effect kind") from exc
        return PendingEffect(
            effect_id,
            case_id,
            revision,
            str(machine),
            parsed,
            _record_datetime(created, "effect creation"),
            None if claimed is None else _record_datetime(claimed, "effect claim"),
            outcome,
            None if completed is None else _record_datetime(completed, "effect completion"),
        )

    @staticmethod
    def _effect_defect(effect: PendingEffect) -> ValueError | None:
        """The first recorded-effect invariant one stored row breaks, if any."""
        try:
            _token(effect.effect_id, "effect id")
        except ValueError as exc:
            return exc
        if (effect.outcome is None) != (effect.completed_at is None):
            return ValueError("partial effect completion")
        if effect.claimed_at is not None and effect.claimed_at < effect.created_at:
            return ValueError("invalid effect claim")
        if effect.completed_at is not None and (
            effect.claimed_at is None
            or effect.completed_at < effect.claimed_at
            or not effect.outcome
        ):
            return ValueError("invalid effect completion")
        return None

    def _iter_pending_effects(
        self,
        case_id: str,
        *,
        kinds: tuple[Effect | PaymentEffect, ...] = (),
        limit: int | None = None,
        revision: int | None = None,
    ) -> Iterator[PendingEffect]:
        """Stream incomplete effects; predicates can stop without collecting the outbox."""
        bound = _limit_clause(limit, ("rowid",))
        for row in self._effect_rows(
            case_id, bound, incomplete=True, kinds=kinds, revision=revision
        ):
            yield self._effect_record(row)

    def pending_effects(
        self, case_id: str, *, kinds: tuple[Effect | PaymentEffect, ...] = ()
    ) -> tuple[PendingEffect, ...]:
        """Incomplete effects, including claimed work awaiting completion.

        Only the pending rows are read back; settled outbox rows stay unread.
        """
        return tuple(self._iter_pending_effects(case_id, kinds=kinds))

    def claim_effect(self, effect_id: str, claimed_at: datetime) -> PendingEffect:
        return self._update_effect(effect_id, _aware(claimed_at, "claimed_at"), None)

    def complete_effect(
        self,
        effect_id: str,
        outcome: str,
        completed_at: datetime,
    ) -> PendingEffect:
        return self._update_effect(
            effect_id, _aware(completed_at, "completed_at"), _token(outcome, "outcome")
        )

    def _update_effect(
        self,
        effect_id: str,
        timestamp: datetime,
        outcome: str | None,
        *,
        manage_transaction: bool = True,
    ) -> PendingEffect:
        effect_id = _token(effect_id, "effect_id")
        database = self._require_open()
        try:
            if manage_transaction:
                database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT effect_id, case_token, revision, machine, effect, created_at, "
                "claimed_at, outcome, completed_at "
                "FROM pb_pending_effects WHERE effect_id=? AND machine IN ('letter', 'payment')",
                (effect_id,),
            ).fetchone()
            if row is None:
                raise EffectConflict("effect does not exist")
            current = self._effect_record(row)
            if outcome is None:
                if current.claimed_at is not None or current.completed_at is not None:
                    raise EffectConflict("effect is already claimed or completed")
                if timestamp < current.created_at:
                    raise ValueError("claim cannot precede creation")
                database.execute(
                    "UPDATE pb_pending_effects SET claimed_at=? WHERE effect_id=?",
                    (timestamp.isoformat(), effect_id),
                )
                result = replace(current, claimed_at=timestamp)
            elif current.completed_at is not None:
                if outcome != current.outcome:
                    raise EffectConflict("effect completed with a different outcome")
                result = current
            else:
                if current.claimed_at is None:
                    raise EffectConflict("effect must be claimed before completion")
                if timestamp < current.claimed_at:
                    raise ValueError("completion cannot precede claim")
                database.execute(
                    "UPDATE pb_pending_effects SET outcome=?, completed_at=? WHERE effect_id=?",
                    (outcome, timestamp.isoformat(), effect_id),
                )
                result = replace(current, outcome=outcome, completed_at=timestamp)
            if manage_transaction:
                database.execute("COMMIT")
            return result
        except Exception:
            if manage_transaction and database.in_transaction:
                rollback_preserving_error(database)
            raise

    def _payment_case(self, case_id: str) -> CaseRecord:
        """Replay the existing machines and require exactly their stored effects."""
        case = self.get_case(case_id)
        if case is None:
            raise RevisionConflict("missing case")
        no_charge = case.payment_state is PaymentState.NO_CHARGE
        letter_state, payment_state, attempt = (
            CaseState.RESEARCHING,
            PaymentState.NO_CHARGE if no_charge else PaymentState.AUTHORIZED,
            1,
        )
        expected: list[tuple[int, str, Effect | PaymentEffect, datetime]] = []
        from typing import get_args

        event_types = {t.__name__: t for t in (*get_args(Event), *get_args(PaymentEvent))}
        refund_requested: ReturnedMailRefundRequested | None = None
        try:
            history = self._iter_transition_history(case_id)
            pending_row: TransitionRecord | None = None
            extra = False
            for revision in range(2, case.revision + 1):
                rows: list[TransitionRecord] = []
                while True:
                    record = pending_row if pending_row is not None else next(history, None)
                    pending_row = None
                    if record is None or record.revision > revision:
                        pending_row = record
                        break
                    if record.revision < revision:
                        # Oldest first, so below-range rows can only lead the stream.
                        extra = True
                        continue
                    rows.append(record)
                if not rows or len(rows) > 2:
                    raise ValueError("missing or duplicate lifecycle history")
                mapped: PaymentEvent | None = None
                for row in rows:
                    cls = event_types[row.event_name]
                    payload = json.loads(row.event_json)
                    if cls is Stopped:
                        payload["reason"] = StopReason(payload["reason"])
                        if "hard_stop" in payload:
                            payload["hard_stop"] = HardStop(payload["hard_stop"])
                    event = cls(**payload)
                    if lifecycle_event_json(event) != row.event_json:
                        raise ValueError("noncanonical lifecycle event")
                    if row.machine == "letter":
                        lt = transition(letter_state, cast(Event, event), attempt)
                        if (row.source, row.target) != (lt.source, lt.target):
                            raise ValueError("invalid letter history")
                        letter_state, attempt = lt.target, next_attempt(attempt, lt)
                        letter_emitted = tuple(
                            effect
                            for effect in lt.effects
                            if not no_charge or payment_event_for(effect) is None
                        )
                        emitted: tuple[Effect, ...] | tuple[PaymentEffect, ...] = letter_emitted
                        for effect in letter_emitted:
                            mapped = payment_event_for(effect) or mapped
                    elif row.machine == "payment":
                        if no_charge:
                            raise ValueError("beta case has financial history")
                        if isinstance(event, ReturnedMailRefundRequested):
                            if (
                                refund_requested is not None
                                or not 1 <= event.amount_cents <= case.amount_cents
                                or (
                                    event.kind == "FULL_REFUND"
                                    and event.amount_cents != case.amount_cents
                                )
                                or letter_state
                                not in {CaseState.RETURNED, CaseState.CLOSED_RETURNED}
                                or event.attempt != attempt
                            ):
                                raise ValueError("invalid returned-mail refund history")
                            refund_requested = event
                        if isinstance(event, RefundIssued) and (
                            event.captured_cents != case.amount_cents
                        ):
                            raise ValueError("refund capture history differs from case amount")
                        if (
                            isinstance(event, RefundIssued)
                            and refund_requested is not None
                            and (
                                event.amount_cents != refund_requested.amount_cents
                                or event.captured_cents != case.amount_cents
                            )
                        ):
                            raise ValueError("refund history differs from returned-mail intent")
                        if isinstance(event, UnexpectedCaptureConfirmed) and (
                            letter_state is not CaseState.STOPPED_NO_CHARGE
                            or event.authorization_ref != case.authorization_ref
                            or event.amount_cents != case.amount_cents
                        ):
                            raise ValueError("invalid stopped capture history")
                        if (
                            isinstance(event, RefundIssued)
                            and letter_state is CaseState.STOPPED_NO_CHARGE
                            and (not event.is_full or event.captured_cents != case.amount_cents)
                        ):
                            raise ValueError("invalid full compensation history")
                        if len(rows) == 2 and event != mapped:
                            raise ValueError("invalid mapped payment history")
                        pt = payment_transition(payment_state, cast(PaymentEvent, event))
                        if (row.source, row.target) != (pt.source, pt.target):
                            raise ValueError("invalid payment history")
                        payment_state, emitted = pt.target, pt.effects
                    else:
                        raise ValueError("invalid history machine")
                    expected.extend((revision, row.machine, e, row.occurred_at) for e in emitted)
                if mapped is not None and len(rows) != 2:
                    raise ValueError("missing mapped payment transition")
            if extra or pending_row is not None or next(history, None) is not None:
                raise ValueError("extra lifecycle history")
            if (letter_state, payment_state, attempt) != (
                case.letter_state,
                case.payment_state,
                case.attempt,
            ):
                raise ValueError("lifecycle state disagrees with history")
            count = 0
            invalid: ValueError | None = None
            for e in self._iter_effects(case_id):
                if count == len(expected) or expected[count] != (
                    e.revision,
                    e.machine,
                    e.effect,
                    e.created_at,
                ):
                    raise ValueError("outbox disagrees with machine effects")
                count += 1
                if invalid is None:
                    invalid = self._effect_defect(e)
            if count != len(expected):
                raise ValueError("outbox disagrees with machine effects")
            if (
                not (no_charge and case.amount_cents == 0)
                and case.amount_cents not in prices.ALLOWED_PRICE_CENTS
            ):
                raise ValueError("service amount must equal an allowed price")
            if invalid is not None:
                raise invalid
            return case
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageCorruption(f"invalid payment lifecycle: {exc}") from exc

    def payment_cursor(self, case_id: str) -> PaymentCursor | None:
        database = self._require_open()
        own = not database.in_transaction
        try:
            if own:
                database.execute("BEGIN")
            case = self._payment_case(case_id)
            if case.payment_state is PaymentState.NO_CHARGE:
                if (
                    database.execute(
                        "SELECT 1 FROM pb_payment_cursor WHERE case_token=?", (case_id,)
                    ).fetchone()
                    or database.execute(
                        "SELECT 1 FROM pb_payment_operations WHERE case_token=?", (case_id,)
                    ).fetchone()
                ):
                    raise StorageCorruption("no-charge case has payment work")
                if own:
                    database.execute("COMMIT")
                return None
            returned_refund = self.returned_mail_refund(case_id)
            returned_effect = next(
                (
                    e
                    for e in self._iter_effects(case_id)
                    if e.effect is PaymentEffect.REFUND_RETURNED_MAIL
                ),
                None,
            )
            row = database.execute(
                "SELECT payload FROM pb_payment_cursor WHERE case_token=?", (case_id,)
            ).fetchone()
            operations = database.execute(
                "SELECT operation_id, intent, result, completed_at FROM pb_payment_operations "
                "WHERE case_token=? ORDER BY rowid",
                (case_id,),
            )
            if row is None:
                if operations.fetchone() is not None:
                    raise StorageCorruption("payment history without cursor")
                result = None
            else:
                result = decode_cursor(row[0])
                previous: PaymentCursor | None = None
                last_time = result.first_at
                for op_id, intent_text, result_text, completed in operations:
                    intent = decode_cursor(intent_text)
                    if intent.outcome.amount_cents != case.amount_cents or (
                        not 1 <= intent.amount_cents <= case.amount_cents
                        if intent.kind == "refund"
                        else intent.amount_cents != case.amount_cents
                    ):
                        raise ValueError("payment intent differs from case amount")
                    returned_boundary = (
                        returned_refund is not None
                        and previous is not None
                        and previous.kind == "capture"
                        and previous.done
                        and previous.outcome.status == "succeeded"
                        and previous.outcome.evidence_version == 1
                        and previous.outcome.full_object
                        and returned_effect is not None
                        and previous.case_revision
                        < returned_effect.revision
                        <= intent.case_revision
                        and returned_effect.created_at <= intent.next_at
                        and intent.kind == "refund"
                        and intent.call_kind == "retrieve"
                        and intent.amount_cents == returned_refund.amount_cents
                        and intent.effect_id
                        == mutation_key(
                            "returned-mail",
                            case_id,
                            returned_refund.kind,
                            str(returned_refund.attempt),
                        )
                    )
                    if (
                        intent.owner != _token(op_id, "payment operation")
                        or intent.done
                        or intent.compensation_started
                        or intent.outcome.status != "uncertain"
                        or (
                            previous is None
                            and (
                                intent.call_kind != "retrieve"
                                or intent.first_at != intent.next_at
                                or intent.failed_attempts != 0
                            )
                        )
                    ):
                        raise ValueError("invalid payment operation owner")
                    if intent.revision != (1 if previous is None else previous.revision + 1):
                        raise ValueError("invalid payment revision history")
                    if intent.case_token != case_id or intent.next_at < last_time:
                        raise ValueError("invalid payment intent anchor/time")
                    if previous is not None and (
                        intent.authorization_ref,
                        None if returned_boundary else intent.amount_cents,
                        intent.key,
                        intent.first_at,
                    ) != (
                        previous.authorization_ref,
                        None if returned_boundary else previous.amount_cents,
                        previous.key,
                        previous.first_at,
                    ):
                        raise ValueError("changed immutable payment anchor")
                    if previous is not None:
                        if (
                            intent.kind != previous.kind
                            and not returned_boundary
                            and not (
                                previous.kind == "release"
                                and previous.compensation_started
                                and intent.kind == "refund"
                                and previous.outcome.status == "succeeded"
                                and previous.outcome.evidence_version == 1
                                and previous.outcome.full_object
                                and intent.call_kind == "retrieve"
                                and case.letter_state is CaseState.STOPPED_NO_CHARGE
                            )
                        ):
                            raise ValueError("changed payment kind without compensation")
                        if intent.failed_attempts != previous.failed_attempts or (
                            previous.done and not returned_boundary
                        ):
                            raise ValueError(
                                "changed payment failure count or resumed terminal operation"
                            )
                        if intent.call_kind != "retrieve" and (
                            previous.owner is not None
                            or (
                                not previous.outcome.refundable
                                if intent.kind == "refund"
                                else previous.outcome.status != "requires_capture"
                            )
                            or previous.effect_id != intent.effect_id
                            or (
                                intent.contract_version == 1
                                and intent.next_at >= intent.first_at + timedelta(hours=23)
                            )
                        ):
                            raise ValueError("mutation without reconciled original object")
                    if previous is not None and intent.contract_version < previous.contract_version:
                        raise ValueError("payment contract downgrade")
                    if intent.contract_version == 3 and intent.authorization_charge_ref != (
                        None
                        if previous is None
                        else (
                            previous.authorization_charge_ref
                            or (
                                previous.outcome.original_charge_ref
                                if intent.kind == "refund"
                                else None
                            )
                        )
                    ):
                        raise ValueError("changed intent charge anchor")
                    if intent.contract_version >= 2:
                        if intent.call_kind == "retrieve" and intent.mutation_key != (
                            None if previous is None else previous.mutation_key
                        ):
                            raise ValueError("lookup rotated mutation key")
                        if intent.call_kind != "retrieve":
                            if intent.kind == "refund":
                                decision = self._refund_mutation_decision(
                                    case_id,
                                    previous,
                                    now=intent.next_at,
                                    before_operation=op_id,
                                )
                                if decision is None:
                                    raise ValueError("refund mutation without sufficient evidence")
                                mode, anchor = decision
                                expected_key = (
                                    mutation_key(
                                        "returned-mail",
                                        case_id,
                                        returned_refund.kind,
                                        str(returned_refund.attempt),
                                    )
                                    if returned_refund is not None
                                    else anchor.mutation_key
                                    if mode == "replay" and anchor is not None
                                    else mutation_key(intent.key, case_id, intent.kind, op_id)
                                )
                                if intent.mutation_key != expected_key:
                                    raise ValueError("invalid refund mutation key")
                                if (
                                    mode == "replay"
                                    and anchor is not None
                                    and (
                                        intent.effect_id != anchor.effect_id
                                        or intent.authorization_ref != anchor.authorization_ref
                                        or intent.amount_cents != anchor.amount_cents
                                        or intent.key != anchor.key
                                        or intent.authorization_charge_ref
                                        != anchor.authorization_charge_ref
                                    )
                                ):
                                    raise ValueError("refund replay changed original request")
                            elif intent.mutation_key != mutation_key(
                                intent.key, case_id, intent.kind, op_id
                            ):
                                raise ValueError("invalid derived mutation key")
                        if intent.call_kind != "retrieve" and (
                            previous is None
                            or previous.call_kind != "retrieve"
                            or not previous.outcome.full_object
                            or (
                                intent.kind == "capture"
                                and intent.next_at >= case.authorization_expires_at - EXPIRY_MARGIN
                            )
                        ):
                            raise ValueError("mutation without full-object reconciliation")
                    if intent.contract_version == 3 and intent.call_kind != "retrieve":
                        if (
                            previous is None
                            or previous.outcome.evidence_version != 1
                            or intent.authorization_charge_ref != previous.authorization_charge_ref
                            or intent.authorization_charge_ref
                            != previous.outcome.original_charge_ref
                        ):
                            raise ValueError("mutation without adapter evidence")
                        inspection = previous.outcome.authorization_inspection
                        if intent.kind == "capture" and (
                            inspection is None
                            or inspection.expires_at != case.authorization_expires_at
                        ):
                            raise ValueError("mutation without matching expiry evidence")
                    if (result_text is None) != (completed is None):
                        raise ValueError("partial payment outcome")
                    previous = intent
                    last_time = intent.next_at
                    if result_text is not None:
                        previous = decode_cursor(result_text)
                        if previous.outcome.amount_cents != case.amount_cents:
                            raise ValueError("payment outcome differs from case amount")
                        at = _record_datetime(completed, "payment completion")
                        if (
                            at < last_time
                            or previous.revision != intent.revision + 1
                            or previous.owner is not None
                        ):
                            raise ValueError("invalid payment completion")
                        if (
                            previous.case_token,
                            previous.effect_id,
                            previous.kind,
                            previous.authorization_ref,
                            previous.amount_cents,
                            previous.key,
                            previous.first_at,
                            previous.call_kind,
                            previous.mutation_key,
                            previous.final_capture,
                            previous.contract_version,
                        ) != (
                            intent.case_token,
                            intent.effect_id,
                            intent.kind,
                            intent.authorization_ref,
                            intent.amount_cents,
                            intent.key,
                            intent.first_at,
                            intent.call_kind,
                            intent.mutation_key,
                            intent.final_capture,
                            intent.contract_version,
                        ):
                            raise ValueError("changed payment outcome anchor")
                        if intent.contract_version == 3:
                            expected_charge = intent.authorization_charge_ref
                            if previous.outcome.evidence_version == 1:
                                found = previous.outcome.original_charge_ref
                                if found is not None:
                                    if expected_charge is not None and expected_charge != found:
                                        raise ValueError("changed original charge evidence")
                                    expected_charge = found
                            if previous.authorization_charge_ref != expected_charge:
                                raise ValueError("changed original charge anchor")
                            if (
                                previous.outcome.status != "uncertain"
                                and previous.outcome.evidence_version != 1
                            ):
                                raise ValueError("missing versioned adapter evidence")
                        status = previous.outcome.status
                        if previous.compensation_started and (
                            intent.kind != "release"
                            or status != "succeeded"
                            or case.letter_state is not CaseState.STOPPED_NO_CHARGE
                            or not any(
                                r.event_name == "UnexpectedCaptureConfirmed"
                                and r.revision == previous.case_revision
                                and json.loads(r.event_json)["charge_ref"]
                                == previous.outcome.charge_ref
                                for r in self._iter_transition_history(case_id)
                            )
                        ):
                            raise ValueError("compensation disagrees with lifecycle")
                        if (
                            intent.contract_version >= 2
                            and status not in {"uncertain", "transient"}
                            and not previous.outcome.full_object
                        ):
                            raise ValueError("missing full-object payment evidence")
                        # Older unresolved histories remain readable past the new bound.
                        # Only a completed accounting settlement can use this exception
                        # to the usual provider-confirmed terminal-status requirement.
                        bounded_writeoff = (
                            intent.kind == "capture"
                            and intent.call_kind == "retrieve"
                            and previous.done
                            and status in {"uncertain", "requires_capture"}
                            and at >= self._capture_reconciliation_deadline(case)
                        )
                        if bounded_writeoff:
                            if previous.case_revision != intent.case_revision + 2:
                                raise ValueError("bounded write-off revision mismatch")
                            for event, revision in (
                                (
                                    CaptureFailed(CAPTURE_RECONCILIATION_EXHAUSTED),
                                    previous.case_revision - 1,
                                ),
                                (ReconciliationExhausted(), previous.case_revision),
                            ):
                                if not any(
                                    r.machine == "payment"
                                    and r.revision == revision
                                    and r.event_name == type(event).__name__
                                    and r.event_json == lifecycle_event_json(event)
                                    and r.occurred_at == at
                                    for r in self._iter_transition_history(case_id)
                                ):
                                    raise ValueError("bounded write-off disagrees with lifecycle")
                        failure = bounded_writeoff or (
                            intent.kind == "capture"
                            and status in {"transient", "declined", "expired", "canceled"}
                        )
                        terminal = status in (
                            {"succeeded", "declined", "expired", "canceled"}
                            if intent.kind == "capture"
                            else (set() if intent.kind == "refund" else {"expired", "canceled"})
                        )
                        if intent.kind == "refund":
                            terminal = (
                                intent.call_kind == "retrieve"
                                and previous.outcome.refund_settled(intent.amount_cents)
                            )
                        terminal = terminal or bounded_writeoff
                        if intent.kind == "refund" and terminal:
                            assert previous.outcome.refunds is not None
                            refs = sorted(
                                r.refund_ref
                                for r in previous.outcome.refunds
                                if r.status == "succeeded"
                            )
                            expected_refund = json.dumps(
                                asdict(
                                    RefundIssued(
                                        ",".join(refs), intent.amount_cents, case.amount_cents
                                    )
                                ),
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            if not any(
                                r.event_name == "RefundIssued"
                                and r.revision == previous.case_revision
                                and r.event_json == expected_refund
                                for r in self._iter_transition_history(case_id)
                            ):
                                raise ValueError("refund completion disagrees with lifecycle")
                        if (
                            previous.failed_attempts != intent.failed_attempts + int(failure)
                            or previous.done != terminal
                        ):
                            raise ValueError("payment outcome count/completion mismatch")
                        if previous.next_at < at or previous.case_revision < intent.case_revision:
                            raise ValueError("payment outcome time/revision mismatch")
                        if status == "transient" and intent.kind == "capture":
                            if intent.call_kind != "capture":
                                raise ValueError("transient lookup treated as failed capture")
                            schedule = next_capture_retry(
                                previous.failed_attempts, at, case.authorization_expires_at
                            )
                            if previous.next_at != (
                                at if schedule is RetryDecision.WRITE_OFF else schedule
                            ):
                                raise ValueError("incorrect durable retry schedule")
                        last_time = at
                if (
                    previous != result
                    or result.authorization_ref != case.authorization_ref
                    or result.amount_cents
                    != (
                        returned_refund.amount_cents
                        if result.kind == "refund" and returned_refund is not None
                        else case.amount_cents
                    )
                ):
                    raise ValueError("payment cursor disagrees with history/case")
                effect = next(
                    (e for e in self._iter_effects(case_id) if e.effect_id == result.effect_id),
                    None,
                )
                if (
                    effect is None
                    or effect.machine != "payment"
                    or effect.effect
                    != (
                        PaymentEffect.CAPTURE_PAYMENT
                        if result.kind == "capture"
                        else (
                            (
                                PaymentEffect.REFUND_RETURNED_MAIL
                                if returned_refund is not None
                                else PaymentEffect.REFUND_FULL
                            )
                            if result.kind == "refund"
                            else PaymentEffect.RELEASE_HOLD
                        )
                    )
                    or effect.claimed_at is None
                ):
                    raise ValueError("payment cursor missing claimed effect")
                if result.owner is None:
                    complete = (
                        result.done
                        or result.compensation_started
                        or (result.kind == "capture" and result.outcome.status == "transient")
                    )
                    if complete != (effect.completed_at is not None) or (
                        complete and effect.outcome != payment_outcome_json(result.outcome)
                    ):
                        raise ValueError("payment effect/outcome disagreement")
                elif effect.completed_at is not None:
                    raise ValueError("owned payment effect already completed")
                if result.case_revision > case.revision:
                    raise ValueError("payment cursor ahead of case")
            if own:
                database.execute("COMMIT")
            return result
        except Exception as exc:
            if own and database.in_transaction:
                rollback_preserving_error(database)
            if isinstance(exc, (ValueError, TypeError, KeyError)) and not isinstance(
                exc, (StorageCorruption, RevisionConflict)
            ):
                raise StorageCorruption(f"invalid payment history: {exc}") from exc
            raise

    def _refund_mutation_decision(
        self,
        case_id: str,
        previous: PaymentCursor | None,
        *,
        now: datetime,
        before_operation: str | None = None,
    ) -> tuple[str, PaymentCursor | None] | None:
        if (
            previous is None
            or previous.call_kind != "retrieve"
            or not previous.outcome.full_object
            or previous.outcome.evidence_version != 1
            or not previous.outcome.refundable
            or previous.outcome.original_charge_ref != previous.authorization_charge_ref
        ):
            return None
        attempts: list[tuple[PaymentCursor, PaymentCursor]] = []
        for op_id, intent_text, result_text in self._require_open().execute(
            "SELECT operation_id, intent, result FROM pb_payment_operations "
            "WHERE case_token=? ORDER BY rowid",
            (case_id,),
        ):
            if op_id == before_operation:
                break
            intent = decode_cursor(intent_text)
            if intent.call_kind == "refund":
                result = intent if result_text is None else decode_cursor(result_text)
                attempts.append((intent, result))
        if not attempts:
            return ("new", None) if previous.outcome.refunds == () else None
        latest_intent, latest = attempts[-1]
        attempted = latest.outcome.refunds
        if (
            attempted is not None
            and len(attempted) == 1
            and attempted[0].status in {"failed", "canceled"}
            and attempted[0].amount_cents == previous.amount_cents
            and any(r == attempted[0] for r in previous.outcome.refunds or ())
        ):
            # Returned-mail has one stable request/key. A definitively failed refund
            # remains pending; only the older compensation policy rotates attempts.
            return ("new", None) if self.returned_mail_refund(case_id) is None else None
        # A complete empty lookup permits only a same-key replay, before Stripe may
        # prune the idempotency result. Anchor to the first durable intent using it.
        if latest.outcome.status != "uncertain" or latest.mutation_key is None:
            return None
        if previous.outcome.refunds != ():
            return None
        anchor = next(
            (
                intent
                for intent, _result in attempts
                if intent.mutation_key == latest_intent.mutation_key
            ),
            None,
        )
        if (
            anchor is None
            or anchor.mutation_key is None
            or latest_intent.mutation_key != previous.mutation_key
            or latest_intent.effect_id != previous.effect_id
            or latest_intent.authorization_ref != previous.authorization_ref
            or latest_intent.amount_cents != previous.amount_cents
            or latest_intent.authorization_charge_ref != previous.authorization_charge_ref
            or now >= anchor.next_at + REFUND_REPLAY_WINDOW
        ):
            return None
        return "replay", anchor

    def refund_replay_valid(
        self, previous: PaymentCursor | None, claimed: PaymentCursor, *, now: datetime
    ) -> bool:
        """Recheck a claimed same-key refund replay immediately before its provider call."""
        now = _aware(now, "now")
        if claimed.kind != "refund" or claimed.call_kind != "refund" or claimed.owner is None:
            return False
        if now < claimed.next_at:
            return False
        if self.payment_cursor(claimed.case_token) != claimed:
            return False
        decision = self._refund_mutation_decision(
            claimed.case_token,
            previous,
            now=now,
            before_operation=claimed.owner,
        )
        if decision is None or decision[0] != "replay" or decision[1] is None:
            return False
        anchor = decision[1]
        return (
            claimed.mutation_key == anchor.mutation_key
            and claimed.effect_id == anchor.effect_id
            and claimed.authorization_ref == anchor.authorization_ref
            and claimed.amount_cents == anchor.amount_cents
            and claimed.authorization_charge_ref == anchor.authorization_charge_ref
            and claimed.key == anchor.key
        )

    def claim_payment(
        self,
        case_id: str,
        expected_case_revision: int,
        expected_cursor_revision: int | None,
        *,
        now: datetime,
        operation_id: str,
        key: str,
        retry_effect_id: str,
        authorization_evidence_required: bool = False,
    ) -> PaymentCursor:
        if not self._submission_locked:
            raise RevisionConflict("exclusive process ownership required")
        now = _aware(now, "now")
        _token(operation_id, "operation_id")
        _token(key, "key")
        if (
            type(expected_case_revision) is not int
            or expected_case_revision < 1
            or (
                expected_cursor_revision is not None
                and (type(expected_cursor_revision) is not int or expected_cursor_revision < 1)
            )
        ):
            raise ValueError("invalid expected payment revision")
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            previous = self.payment_cursor(case_id)
            case = self._payment_case(case_id)
            if case.payment_state is PaymentState.NO_CHARGE:
                raise ValueError("no-charge case cannot dispatch payment")
            returned_refund = self.returned_mail_refund(case_id)
            returned_boundary = (
                returned_refund is not None
                and previous is not None
                and previous.kind == "capture"
                and previous.done
                and case.payment_state is PaymentState.CAPTURED
            )
            if (
                case.revision != expected_case_revision
                or (None if previous is None else previous.revision) != expected_cursor_revision
            ):
                raise RevisionConflict("stale payment/case revision")
            if (
                database.execute(
                    "SELECT 1 FROM pb_submission_operations WHERE operation_id=?", (operation_id,)
                ).fetchone()
                or database.execute(
                    "SELECT 1 FROM pb_pending_effects WHERE effect_id=?", (operation_id,)
                ).fetchone()
                or database.execute(
                    "SELECT 1 FROM pb_payment_operations WHERE operation_id=?", (operation_id,)
                ).fetchone()
            ):
                raise ValueError("reused operation id")
            if previous is not None:
                if previous.done and not returned_boundary:
                    raise ValueError("payment already resolved")
                if now < previous.next_at:
                    raise ValueError("payment step not due")
                if key != previous.key:
                    raise ValueError("immutable payment key changed")
            for other_id, payload in database.execute(
                "SELECT case_token, payload FROM pb_payment_cursor"
            ):
                other = decode_cursor(payload)
                if other_id != case_id and other.key == key:
                    raise ValueError("payment key already bound to another case")
            pending = list(
                self._iter_pending_effects(
                    case_id,
                    kinds=(
                        PaymentEffect.CAPTURE_PAYMENT,
                        PaymentEffect.RELEASE_HOLD,
                        PaymentEffect.REFUND_FULL,
                        PaymentEffect.REFUND_RETURNED_MAIL,
                    ),
                    limit=2,
                )
            )
            if (
                not pending
                and previous is not None
                and previous.outcome.status == "transient"
                and previous.kind == "capture"
            ):
                if (
                    retry_effect_id == operation_id
                    or database.execute(
                        "SELECT 1 FROM pb_payment_operations WHERE operation_id=? UNION ALL "
                        "SELECT 1 FROM pb_submission_operations WHERE operation_id=?",
                        (retry_effect_id, retry_effect_id),
                    ).fetchone()
                ):
                    raise ValueError("reused retry effect id")
                case = self._apply_event(
                    case_id,
                    RetryCapture(),
                    case.revision,
                    False,
                    now,
                    (_token(retry_effect_id, "retry_effect_id"),),
                    manage_transaction=False,
                )
                retried = self.latest_effect(case_id)
                pending = [] if retried is None else [retried]
            if len(pending) != 1:
                raise EffectConflict("requires exactly one pending payment effect")
            effect = pending[0]
            kind = {
                PaymentEffect.CAPTURE_PAYMENT: "capture",
                PaymentEffect.RELEASE_HOLD: "release",
                PaymentEffect.REFUND_FULL: "refund",
                PaymentEffect.REFUND_RETURNED_MAIL: "refund",
            }[cast(PaymentEffect, effect.effect)]
            if kind == "capture":
                submission = self.submission_cursor(case_id, 1)
                if submission is not None and pending_evidence(submission.outcome):
                    raise ValueError("capture requires explicit mailing evidence")
                if case.payment_state not in {
                    PaymentState.CAPTURE_PENDING,
                    PaymentState.CAPTURE_RETRYING,
                } or not any(
                    r.machine == "letter" and r.event_name == "SubmitConfirmed"
                    for r in self._iter_transition_history(case_id)
                ):
                    raise ValueError("capture requires accepted mailing and pending payment")
            elif kind == "refund":
                if returned_refund is not None:
                    if (
                        case.payment_state is not PaymentState.CAPTURED
                        or previous is None
                        or (
                            previous.outcome.original_charge_ref is None
                            and previous.authorization_charge_ref is None
                        )
                        or effect.effect is not PaymentEffect.REFUND_RETURNED_MAIL
                    ):
                        raise ValueError("refund requires original returned-mail capture")
                elif (
                    case.letter_state is not CaseState.STOPPED_NO_CHARGE
                    or case.payment_state is not PaymentState.CAPTURED
                    or previous is None
                    or (previous.authorization_charge_ref or previous.outcome.original_charge_ref)
                    is None
                    or not any(
                        r.event_name == "UnexpectedCaptureConfirmed"
                        for r in self._iter_transition_history(case_id)
                    )
                ):
                    raise ValueError("refund requires original stopped-case compensation")
            elif (
                case.letter_state is not CaseState.STOPPED_NO_CHARGE
                or case.payment_state is not PaymentState.RELEASED
            ):
                raise ValueError("release requires legitimate no-charge stop")
            if effect.claimed_at is not None:
                if previous is None or previous.effect_id != effect.effect_id:
                    raise EffectConflict("cannot recover another caller's outbox claim")
            else:
                self._update_effect(effect.effect_id, now, None, manage_transaction=False)
            # Routing was applied with the letter transition; acknowledge only.
            for routing in tuple(
                self._iter_pending_effects(
                    case_id,
                    kinds=(Effect.CAPTURE_PAYMENT, Effect.RELEASE_HOLD),
                    revision=effect.revision,
                )
            ):
                if (
                    routing.machine == "letter"
                    and routing.revision == effect.revision
                    and routing.effect.value == effect.effect.value
                    and routing.completed_at is None
                ):
                    if routing.claimed_at is not None:
                        raise EffectConflict("routing row owned by another caller")
                    self._update_effect(routing.effect_id, now, None, manage_transaction=False)
                    self._update_effect(routing.effect_id, now, "routed", manage_transaction=False)
            evidence_required = authorization_evidence_required or (
                previous is not None and previous.contract_version == 3
            )
            call_kind = "retrieve"
            refund_decision: tuple[str, PaymentCursor | None] | None = None
            if (
                previous is not None
                and previous.owner is None
                and previous.effect_id == effect.effect_id
                and previous.outcome.status == "requires_capture"
                and previous.call_kind == "retrieve"
                and previous.outcome.full_object
                and (
                    not evidence_required
                    or (
                        previous.outcome.evidence_version == 1
                        and (
                            kind == "release"
                            or (
                                previous.outcome.authorization_inspection is not None
                                and previous.outcome.authorization_inspection.expires_at
                                == case.authorization_expires_at
                            )
                        )
                    )
                )
                and (kind == "release" or now < case.authorization_expires_at - EXPIRY_MARGIN)
            ):
                call_kind = kind
            if (
                kind == "refund"
                and previous is not None
                and previous.owner is None
                and previous.kind == "refund"
                and previous.call_kind == "retrieve"
            ):
                refund_decision = self._refund_mutation_decision(case_id, previous, now=now)
                if refund_decision is not None:
                    call_kind = "refund"
            cursor = PaymentCursor(
                case_id,
                effect.effect_id,
                kind,
                case.authorization_ref,
                (
                    returned_refund.amount_cents
                    if kind == "refund" and returned_refund is not None
                    else case.amount_cents
                ),
                key,
                now if previous is None else previous.first_at,
                0 if previous is None else previous.failed_attempts,
                now,
                1 if previous is None else previous.revision + 1,
                case.revision,
                operation_id,
                call_kind,
                PaymentOutcome("uncertain", case.authorization_ref, case.amount_cents),
                False,
                (
                    None
                    if previous is None
                    else previous.mutation_key
                    if call_kind == "retrieve"
                    else (
                        refund_decision[1].mutation_key
                        if kind == "refund"
                        and refund_decision is not None
                        and refund_decision[0] == "replay"
                        and refund_decision[1] is not None
                        else (
                            mutation_key(
                                "returned-mail",
                                case_id,
                                returned_refund.kind,
                                str(returned_refund.attempt),
                            )
                            if kind == "refund" and returned_refund is not None
                            else mutation_key(key, case_id, kind, operation_id)
                        )
                    )
                ),
                True if kind == "capture" else None,
                3 if evidence_required or kind == "refund" else 2,
                None
                if previous is None
                else (
                    previous.authorization_charge_ref
                    or (previous.outcome.original_charge_ref if kind == "refund" else None)
                ),
            )
            encoded = cursor_json(cursor)
            database.execute(
                "INSERT INTO pb_payment_operations VALUES (?, ?, ?, NULL, NULL)",
                (operation_id, case_id, encoded),
            )
            database.execute(
                "INSERT INTO pb_payment_cursor VALUES (?, ?) ON CONFLICT(case_token) "
                "DO UPDATE SET payload=excluded.payload",
                (case_id, encoded),
            )
            database.execute("COMMIT")
            return cursor
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    def _capture_reconciliation_deadline(self, case: CaseRecord) -> datetime:
        """Anchor grace to the first unresolved request, never the latest retry.

        Existing operation intents persist request times even across process death.
        The first uncertain/pending call (or still-held lookup after the safe capture
        window) establishes uncertainty. Use that time when later than expiry, without
        adding a new cursor field or inventing a timestamp for historical records.
        """
        for intent_text, result_text in self._require_open().execute(
            "SELECT intent, result FROM pb_payment_operations WHERE case_token=? ORDER BY rowid",
            (case.case_token,),
        ):
            intent = decode_cursor(intent_text)
            if intent.kind != "capture":
                continue
            outcome = None if result_text is None else decode_cursor(result_text).outcome
            if (
                outcome is None
                or outcome.status == "uncertain"
                or (
                    outcome.status == "requires_capture"
                    and intent.next_at >= case.authorization_expires_at - EXPIRY_MARGIN
                )
            ):
                return (
                    max(case.authorization_expires_at, intent.next_at)
                    + CAPTURE_RECONCILIATION_GRACE
                )
        return case.authorization_expires_at + CAPTURE_RECONCILIATION_GRACE

    def finish_payment(
        self,
        claimed: PaymentCursor,
        outcome: PaymentOutcome,
        *,
        now: datetime,
        writeoff_effect_ids: tuple[str, ...] = (),
        compensation_effect_ids: tuple[str, ...] = (),
        new_effect_id: Callable[[], str] | None = None,
    ) -> PaymentCursor:
        if not self._submission_locked:
            raise RevisionConflict("exclusive process ownership required")
        now = _aware(now, "now")
        payment_outcome_json(outcome)
        case = self._payment_case(claimed.case_token)
        if (
            outcome.authorization_ref != claimed.authorization_ref
            or outcome.amount_cents != case.amount_cents
            or (
                not 1 <= claimed.amount_cents <= case.amount_cents
                if claimed.kind == "refund"
                else claimed.amount_cents != case.amount_cents
            )
        ):
            raise ValueError("provider anchor mismatch")
        if (
            claimed.contract_version >= 2
            and outcome.status not in {"uncertain", "transient"}
            and not outcome.full_object
        ):
            raise ValueError("provider full-object evidence required")
        if claimed.contract_version == 3 and (
            (outcome.status != "uncertain" and outcome.evidence_version != 1)
            or (
                claimed.authorization_charge_ref is not None
                and outcome.original_charge_ref is not None
                and outcome.original_charge_ref != claimed.authorization_charge_ref
            )
        ):
            raise ValueError("provider original charge evidence required")
        if (
            claimed.call_kind == "refund"
            and outcome.refunds is not None
            and (
                len(outcome.refunds) != 1 or outcome.refunds[0].amount_cents != claimed.amount_cents
            )
        ):
            raise ValueError("refund response differs from intent")
        database = self._require_open()
        try:
            database.execute("BEGIN IMMEDIATE")
            current = self.payment_cursor(claimed.case_token)
            if current != claimed or claimed.owner is None:
                # Repeating exactly the committed outcome preserves its first completion.
                row = database.execute(
                    "SELECT intent, result FROM pb_payment_operations WHERE operation_id=?",
                    (claimed.owner,),
                ).fetchone()
                if row and row[0] == cursor_json(claimed) and row[1] is not None:
                    saved = decode_cursor(row[1])
                    if saved.outcome == outcome:
                        database.execute("COMMIT")
                        return saved
                    raise EffectConflict("payment completed with a different outcome")
                raise RevisionConflict("payment ownership/revision changed")
            case = self._payment_case(claimed.case_token)
            if case.revision != claimed.case_revision:
                raise RevisionConflict("case changed during payment call")
            if now < claimed.next_at:
                raise ValueError("completion precedes intent")
            status = outcome.status
            if status == "requires_capture" and claimed.call_kind != "retrieve":
                raise ValueError("mutation must report a confirmed outcome or uncertainty")
            done, complete, failures, next_at = False, False, claimed.failed_attempts, now
            events: list[PaymentEvent] = []
            if claimed.kind == "capture":
                if status == "succeeded":
                    assert outcome.charge_ref is not None
                    events = [CaptureSucceeded(outcome.charge_ref)]
                    done = complete = True
                elif status in {"declined", "expired", "canceled", "transient"}:
                    if status == "transient" and claimed.call_kind != "capture":
                        raise ValueError("retrieve cannot confirm transient capture failure")
                    events = [CaptureFailed(status)]
                    failures += 1
                    complete = True
                    if status == "declined":
                        events.append(CardDeclined("provider confirmed decline"))
                        done = True
                    elif status in {"expired", "canceled"}:
                        events.append(AuthorizationLapsed())
                        done = True
                    else:
                        scheduled = next_capture_retry(failures, now, case.authorization_expires_at)
                        # Local expiry is not proof of money outcome. Reconcile instead.
                        next_at = now if scheduled is RetryDecision.WRITE_OFF else scheduled
                elif (
                    claimed.call_kind == "retrieve"
                    and now >= self._capture_reconciliation_deadline(case)
                ):
                    # Succeeded/declined/canceled evidence above wins even at the bound.
                    # Exhaustion is an accounting decision, not fabricated expiry evidence;
                    # preserve the final original-object retrieve outcome in the cursor/outbox.
                    events = [
                        CaptureFailed(CAPTURE_RECONCILIATION_EXHAUSTED),
                        ReconciliationExhausted(),
                    ]
                    failures += 1
                    done = complete = True
            elif claimed.kind == "refund":
                if claimed.call_kind == "retrieve" and outcome.refund_settled(claimed.amount_cents):
                    assert outcome.refunds is not None
                    refs = sorted(r.refund_ref for r in outcome.refunds if r.status == "succeeded")
                    events = [RefundIssued(",".join(refs), claimed.amount_cents, case.amount_cents)]
                    done = complete = True
            elif status in {"canceled", "expired"}:
                done = complete = True
            elif status == "succeeded" and outcome.full_object and outcome.evidence_version == 1:
                assert outcome.charge_ref is not None
                events = [
                    UnexpectedCaptureConfirmed(
                        case.authorization_ref, outcome.charge_ref, case.amount_cents
                    )
                ]
                complete = True
            if (
                not done
                and not complete
                and (
                    (
                        not outcome.refundable
                        if claimed.kind == "refund"
                        else status != "requires_capture"
                    )
                    or (
                        claimed.kind == "capture"
                        and now >= case.authorization_expires_at - EXPIRY_MARGIN
                    )
                )
            ):
                next_at = now + timedelta(minutes=30)
                if claimed.kind == "capture":
                    next_at = min(next_at, self._capture_reconciliation_deadline(case))
            if (
                claimed.kind == "refund"
                and self.returned_mail_refund(claimed.case_token) is not None
                and not done
                and claimed.call_kind == "retrieve"
                and outcome.refundable
                and self._refund_mutation_decision(
                    claimed.case_token, replace(claimed, outcome=outcome), now=now
                )
                is None
            ):
                next_at = now + timedelta(minutes=30)
            if compensation_effect_ids and not any(
                isinstance(e, UnexpectedCaptureConfirmed) for e in events
            ):
                raise ValueError("unexpected compensation ids")
            if writeoff_effect_ids and not any(
                isinstance(e, (CardDeclined, AuthorizationLapsed, ReconciliationExhausted))
                for e in events
            ):
                raise ValueError("unexpected write-off ids")
            for effect_id in (*writeoff_effect_ids, *compensation_effect_ids):
                if database.execute(
                    "SELECT 1 FROM pb_payment_operations WHERE operation_id=? UNION ALL "
                    "SELECT 1 FROM pb_submission_operations WHERE operation_id=?",
                    (effect_id, effect_id),
                ).fetchone():
                    raise ValueError("reused write-off effect id")
            for event in events:
                ids = (
                    writeoff_effect_ids
                    if isinstance(
                        event, (CardDeclined, AuthorizationLapsed, ReconciliationExhausted)
                    )
                    else (
                        compensation_effect_ids
                        if isinstance(event, UnexpectedCaptureConfirmed)
                        else ()
                    )
                )
                case = self._apply_event(
                    case.case_token,
                    event,
                    case.revision,
                    False,
                    now,
                    ids,
                    manage_transaction=False,
                    new_effect_id=new_effect_id,
                )
            if complete:
                self._update_effect(
                    claimed.effect_id, now, payment_outcome_json(outcome), manage_transaction=False
                )
            result = replace(
                claimed,
                owner=None,
                revision=claimed.revision + 1,
                case_revision=case.revision,
                outcome=outcome,
                done=done,
                compensation_started=any(isinstance(e, UnexpectedCaptureConfirmed) for e in events),
                failed_attempts=failures,
                next_at=next_at,
                authorization_charge_ref=(
                    claimed.authorization_charge_ref or outcome.original_charge_ref
                    if claimed.contract_version == 3
                    else None
                ),
            )
            encoded = cursor_json(result)
            database.execute(
                "UPDATE pb_payment_operations SET result=?, completed_at=? WHERE operation_id=?",
                (encoded, now.isoformat(), claimed.owner),
            )
            database.execute(
                "UPDATE pb_payment_cursor SET payload=? WHERE case_token=?",
                (encoded, case.case_token),
            )
            database.execute("COMMIT")
            return result
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise

    @contextmanager
    def submission_ownership(self) -> Iterator[None]:
        """Exclusive local-process ownership, including across cursor commits.

        Lock the stable canonical private data directory inode with flock, never the
        SQLite database inode and never a removable sidecar. Closing any fd for the
        database inode would drop this process's SQLite POSIX locks, so the lock
        cycle must not touch it. Distinct databases in one directory therefore
        serialize conservatively, which is acceptable. Never expire a live owner's
        lock: process exit releases it, allowing safe crash recovery. Every
        coordinator must use this seam on the same canonical directory; old
        database-flock owners do not mutually exclude and must be quiesced before
        upgrade or rollback.
        """
        self._require_open()
        if self._submission_locked:
            raise RevisionConflict("submission coordinator is already active")
        anchor = self._submission_anchor
        if anchor is None:
            raise RevisionConflict("submission ownership anchor is unavailable")
        _assert_anchor_unchanged(anchor)
        descriptor = _open_anchor_directory(anchor)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RevisionConflict(
                    "another submission coordinator owns this data directory"
                ) from exc
            self._submission_locked = True
            yield
        finally:
            self._submission_locked = False
            os.close(descriptor)

    def submission_cursor(self, case_id: str, attempt: int) -> SubmissionCursor | None:
        database = self._require_open()
        owns_transaction = not database.in_transaction
        try:
            if owns_transaction:
                database.execute("BEGIN")
            result = self._submission_cursor_snapshot(case_id, attempt)
            if owns_transaction:
                database.execute("COMMIT")
            return result
        except Exception:
            if owns_transaction and database.in_transaction:
                rollback_preserving_error(database)
            raise

    def _submission_cursor_snapshot(self, case_id: str, attempt: int) -> SubmissionCursor | None:
        # Also validate the frozen request on every read.
        prepared = self.get(case_id, attempt)
        row = (
            self._require_open()
            .execute(
                "SELECT * FROM pb_submission_cursors WHERE case_token=? AND attempt=?",
                (case_id, attempt),
            )
            .fetchone()
        )
        if row is None:
            return None
        if prepared is None:
            raise StorageCorruption("cursor without frozen request")
        try:
            first = _record_datetime(row[2], "first send")
            next_at = _record_datetime(row[4], "next action")
            if (
                type(row[3]) is not int
                or row[3] < 1
                or type(row[5]) is not int
                or row[5] not in (0, 1)
                or type(row[6]) is not int
                or row[6] < 1
                or type(row[8]) is not int
                or row[8] < 1
                or next_at < first
                or first < prepared.prepared_at
            ):
                raise ValueError("invalid cursor values")
            owner = None if row[7] is None else _token(row[7], "stored owner")
            outcome = decode_outcome(row[9])
            if owner is not None and not isinstance(outcome, Uncertain):
                raise ValueError("terminal cursor is owned")
            operations = self._require_open().execute(
                "SELECT operation_id, cursor_revision, kind, started_at, "
                "outcome_json, completed_at, temporary_lookup "
                "FROM pb_submission_operations WHERE case_token=? AND attempt=? ORDER BY rowid",
                (case_id, attempt),
            )
            previous_revision = 0
            last_time = first
            permanent_seen = False
            first_operation = None
            sends = 0
            last = None
            for op in operations:
                _token(op[0], "operation id")
                if type(op[1]) is not int or not 1 <= op[1] - previous_revision <= 2:
                    raise ValueError("invalid operation revision sequence")
                started = _record_datetime(op[3], "operation start")
                is_first = first_operation is None
                if is_first:
                    first_operation = started
                sends += op[2] == "create"
                if started < last_time or op[2] not in ("create", "lookup"):
                    raise ValueError("invalid operation sequence")
                if type(op[6]) is not int or op[6] not in (0, 1):
                    raise ValueError("invalid temporary lookup marker")
                if op[6] == 1 and op[2] != "lookup":
                    raise ValueError("temporary marker on a create operation")
                if op[6] == 1 and is_first:
                    raise ValueError("temporary marker on the first operation")
                if op[6] == 1 and permanent_seen:
                    raise ValueError("temporary marker after a permanent lookup")
                if permanent_seen and op[2] == "create":
                    raise ValueError("create after metadata resolution")
                if op[2] == "lookup" and op[6] == 0:
                    permanent_seen = True
                if (op[4] is None) != (op[5] is None):
                    raise ValueError("incomplete operation outcome")
                last_time = started
                if op[4] is not None:
                    decode_outcome(op[4])
                    last_time = _record_datetime(op[5], "operation completion")
                    if last_time < started:
                        raise ValueError("operation completion before start")
                previous_revision = op[1]
                last = op
            if last is None:
                raise ValueError("missing operation history")
            if permanent_seen and not row[5]:
                raise ValueError("permanent lookup requires lookup_only")
            if (
                first != first_operation
                or row[3] != sends
                or row[6] != last[1] + (1 if last[4] is not None else 0)
                or owner != (last[0] if last[4] is None else None)
                or (last[4] is not None and outcome != decode_outcome(last[4]))
            ):
                raise ValueError("cursor does not match operation history")
            return SubmissionCursor(
                row[0], row[1], first, row[3], next_at, bool(row[5]), row[6], owner, row[8], outcome
            )
        except (ValueError, TypeError) as exc:
            raise StorageCorruption(f"invalid submission cursor: {exc}") from exc

    def claim_submission(
        self,
        case_id: str,
        attempt: int,
        expected_case_revision: int,
        expected_cursor_revision: int | None,
        *,
        now: datetime,
        operation_id: str,
        evidence: bool = False,
        manage_transaction: bool = True,
    ) -> tuple[SubmissionCursor, str]:
        """Commit an uncertain call intent before any create/lookup can occur."""
        if not self._submission_locked:
            raise RevisionConflict("submission ownership required")
        now = _aware(now, "now")
        operation_id = _token(operation_id, "operation_id")
        if type(expected_case_revision) is not int or expected_case_revision < 1:
            raise ValueError("expected_case_revision must be a positive integer")
        if expected_cursor_revision is not None and (
            type(expected_cursor_revision) is not int or expected_cursor_revision < 1
        ):
            raise ValueError("expected_cursor_revision must be a positive integer or None")
        database = self._require_open()
        try:
            if manage_transaction:
                database.execute("BEGIN IMMEDIATE")
            case = self.get_case(case_id)
            prepared = self.get(case_id, attempt)
            if case is None or case.revision != expected_case_revision:
                raise RevisionConflict("case revision is stale")
            if (
                case.letter_state not in {CaseState.SUBMITTING, CaseState.IN_TRANSIT}
                or case.attempt != attempt
            ):
                raise ValueError("requires SUBMITTING case and matching approved attempt")
            if prepared is None or now < prepared.prepared_at:
                raise ValueError("requires prepared request before call time")
            current = self.submission_cursor(case_id, attempt)
            if (None if current is None else current.revision) != expected_cursor_revision:
                raise RevisionConflict("submission cursor revision is stale")
            if current is not None:
                if not pending_evidence(current.outcome):
                    raise ValueError("submission already resolved")
                last = database.execute(
                    "SELECT started_at, completed_at FROM pb_submission_operations "
                    "WHERE case_token=? AND attempt=? ORDER BY rowid DESC LIMIT 1",
                    (case_id, attempt),
                ).fetchone()
                if last is None:
                    raise StorageCorruption("cursor has no operation history")
                if now < _record_datetime(last[1] or last[0], "last operation"):
                    raise ValueError("call time moves backwards")
                if now < current.next_at and not evidence:
                    raise ValueError("submission retry is not due")
            if case.letter_state is CaseState.IN_TRANSIT and (
                current is None or not pending_evidence(current.outcome)
            ):
                raise ValueError("requires pending legacy evidence")
            # T74: the durable create-intent claim is the gate every caller crosses, so a
            # direct coordinator call cannot bypass the dispatcher. Activation uses this
            # same submission ownership, so a hold cannot become effective mid-call.
            hold = self.case_hold(case_id)
            held = hold is not None and hold.held
            if held and not evidence and current is None:
                raise SubmissionPaused(case_id, attempt)
            # A temporary hold forces this one operation to be a lookup, but it is not a
            # durable reason for later retries to stay lookup-only. Persist only the old
            # non-hold reasons; a held lookup never increments sends or the create count.
            first = now if current is None else current.first_sent_at
            persistent_lookup = current is not None and (
                evidence
                or current.lookup_only
                or not create_allowed(first, now)
                or isinstance(current.outcome, Accepted)
                or (
                    isinstance(current.outcome, Uncertain) and current.outcome.letter_id is not None
                )
                or (
                    attempt == 1
                    and case.payment_state is not PaymentState.NO_CHARGE
                    and now >= case.authorization_expires_at - EXPIRY_MARGIN - FALLBACK_LEAD
                )
            )
            operation_lookup = persistent_lookup or (held and not evidence and current is not None)
            sends = (0 if current is None else current.sends) + (0 if operation_lookup else 1)
            action = next_action(first, now, sends, VERIFIED_IDEMPOTENCY_WINDOW)
            # Reserve recovery's next action even if this process dies before calling.
            next_at = now if isinstance(action, LookupByMetadata) else action.at
            lookup_only = persistent_lookup or isinstance(action, LookupByMetadata)
            # Only a lookup whose sole reason is the current hold is temporary provenance.
            # Any permanent reason (cutoff, known ID, prior sticky, paid fallback, evidence)
            # keeps marker 0, so a later create is still corruption.
            temporary_lookup = int(
                held
                and not evidence
                and current is not None
                and not persistent_lookup
                and operation_lookup
            )
            # Only a paid case wakes at its capture-fallback boundary. A NO_CHARGE case keeps
            # the ordinary positive backoff: its window is a customer-work window.
            if attempt == 1 and case.payment_state is not PaymentState.NO_CHARGE:
                next_at = max(
                    now, min(next_at, case.authorization_expires_at - EXPIRY_MARGIN - FALLBACK_LEAD)
                )
            revision = 1 if current is None else current.revision + 1
            # A recorded original letter id survives a crash between claim and finish.
            kept = None if current is None else current.outcome
            intent = (
                Uncertain(
                    "call intent committed; outcome unknown",
                    kept.letter_id,
                    kept.rendered if isinstance(kept, Uncertain) else True,
                    kept.tracking_number,
                )
                if isinstance(kept, Uncertain | Accepted)
                else Uncertain("call intent committed; outcome unknown")
            )
            result = SubmissionCursor(
                case_id,
                attempt,
                first,
                sends,
                next_at,
                lookup_only,
                revision,
                operation_id,
                case.revision,
                intent,
            )
            database.execute(
                "INSERT INTO pb_submission_operations "
                "(operation_id, case_token, attempt, cursor_revision, kind, started_at, "
                "outcome_json, completed_at, temporary_lookup) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                (
                    operation_id,
                    case_id,
                    attempt,
                    revision,
                    "lookup" if operation_lookup else "create",
                    now.isoformat(),
                    temporary_lookup,
                ),
            )
            database.execute(
                "INSERT INTO pb_submission_cursors VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(case_token, attempt) DO UPDATE SET sends=excluded.sends, "
                "next_at=excluded.next_at, lookup_only=excluded.lookup_only, "
                "revision=excluded.revision, owner=excluded.owner, "
                "case_revision=excluded.case_revision, outcome_json=excluded.outcome_json",
                (
                    case_id,
                    attempt,
                    first.isoformat(),
                    sends,
                    next_at.isoformat(),
                    int(lookup_only),
                    revision,
                    operation_id,
                    case.revision,
                    outcome_json(result.outcome),
                ),
            )
            if manage_transaction:
                database.execute("COMMIT")
            return result, "lookup" if operation_lookup else "create"
        except Exception:
            if manage_transaction and database.in_transaction:
                rollback_preserving_error(database)
            raise

    def finish_submission(
        self,
        claimed: SubmissionCursor,
        outcome: SubmissionOutcome,
        *,
        now: datetime,
        effect_ids: tuple[str, ...],
        new_effect_id: Callable[[], str] | None = None,
        manage_transaction: bool = True,
    ) -> SubmissionCursor:
        """Outcome and existing lifecycle/history/effects share one commit."""
        if not self._submission_locked:
            raise RevisionConflict("submission ownership required")
        now = _aware(now, "now")
        encoded = outcome_json(outcome)
        database = self._require_open()
        try:
            if manage_transaction:
                database.execute("BEGIN IMMEDIATE")
            current = self.submission_cursor(claimed.case_token, claimed.attempt)
            if current != claimed or claimed.owner is None:
                raise RevisionConflict("submission ownership or revision changed")
            case = self.get_case(claimed.case_token)
            if (
                case is None
                or case.revision != claimed.case_revision
                or case.letter_state not in {CaseState.SUBMITTING, CaseState.IN_TRANSIT}
                or case.attempt != claimed.attempt
            ):
                raise RevisionConflict("case changed during submission")
            operation = database.execute(
                "SELECT started_at, kind FROM pb_submission_operations WHERE operation_id=?",
                (claimed.owner,),
            ).fetchone()
            if operation is None:
                raise StorageCorruption("missing owned operation")
            if now < _record_datetime(operation[0], "call start"):
                raise ValueError("completion precedes call")
            event: Event | None = None
            match outcome:
                case Accepted(letter_id):
                    if outcome.evidence is None:
                        raise ValueError("new acceptance requires explicit evidence")
                    if case.letter_state is CaseState.SUBMITTING:
                        event = SubmitConfirmed(letter_id)
                case Rejected(_, reason):
                    if operation[1] != "create":
                        raise ValueError("lookup cannot definitively reject create")
                    first_send = claimed.sends == 1 and claimed.attempt == 1
                    if reason == StopReason.AUTHORIZATION_EXPIRED.value and claimed.sends == 1:
                        event = AuthorizationExpired()
                    elif reason == TIMING_REFUSAL_REASON and first_send:
                        event = TimingRefused()
                    elif reason == GATE_REFUSED_REASON and first_send:
                        event = DestinationRefused()
                    elif reason == LOCAL_REFUSAL_REASON and first_send:
                        event = LetterNotSubmittable()
                    else:
                        event = SubmitRejected(reason)
                case NotSent():
                    if operation[1] != "lookup":
                        raise ValueError("absence requires lookup")
                    event = ConfirmedNotSent("authoritative metadata absence")
                case Canceled():
                    if operation[1] != "lookup" or case.letter_state is not CaseState.SUBMITTING:
                        raise ValueError("confirmed cancellation requires a lookup in SUBMITTING")
                    event = ConfirmedNotSent("letter canceled, deleted or failed")
            revision = case.revision
            if event is not None:
                revision = self._apply_event(
                    case.case_token,
                    event,
                    case.revision,
                    True,
                    now,
                    effect_ids,
                    manage_transaction=False,
                    new_effect_id=new_effect_id,
                ).revision
            elif effect_ids:
                raise ValueError("uncertain outcomes emit no effects")
            if isinstance(outcome, Accepted) and has_returned_scan(outcome.scans):
                # Lookup and webhook acceptance share this atomic transition. A missed
                # webhook cannot leave a retained return scan stranded in IN_TRANSIT.
                revision = self._apply_event(
                    case.case_token,
                    ReturnConfirmed("provider confirmed return"),
                    revision,
                    True,
                    now,
                    (),
                    manage_transaction=False,
                    new_effect_id=new_effect_id,
                ).revision
            lookup = claimed.lookup_only
            next_at = claimed.next_at
            if isinstance(outcome, Uncertain):
                action = next_action(
                    claimed.first_sent_at, now, claimed.sends, VERIFIED_IDEMPOTENCY_WINDOW
                )
                lookup = lookup or isinstance(action, LookupByMetadata)
                next_at = now if isinstance(action, LookupByMetadata) else action.at
                if operation[1] == "lookup":
                    # Unavailable resolution can be retried, but never create again.
                    next_at = now + timedelta(minutes=30)
                fallback_at = case.authorization_expires_at - EXPIRY_MARGIN - FALLBACK_LEAD
                # Before the fallback time, wake exactly then. After it, an unconfirmed
                # cancel/lookup is retried at the ordinary lookup pace, with the hold kept.
                # A NO_CHARGE case has no capture fallback: it keeps the ordinary schedule.
                if (
                    claimed.attempt == 1
                    and case.payment_state is not PaymentState.NO_CHARGE
                    and now < fallback_at
                ):
                    next_at = max(now, min(next_at, fallback_at))
            result = replace(
                claimed,
                owner=None,
                revision=claimed.revision + 1,
                case_revision=revision,
                outcome=outcome,
                next_at=next_at,
                lookup_only=lookup,
            )
            database.execute(
                "UPDATE pb_submission_operations SET outcome_json=?, completed_at=? "
                "WHERE operation_id=? AND outcome_json IS NULL",
                (encoded, now.isoformat(), claimed.owner),
            )
            database.execute(
                "UPDATE pb_submission_cursors SET next_at=?, lookup_only=?, revision=?, "
                "owner=NULL, case_revision=?, outcome_json=? WHERE case_token=? AND attempt=?",
                (
                    next_at.isoformat(),
                    int(lookup),
                    result.revision,
                    revision,
                    encoded,
                    claimed.case_token,
                    claimed.attempt,
                ),
            )
            if manage_transaction:
                database.execute("COMMIT")
            return result
        except Exception:
            if manage_transaction and database.in_transaction:
                rollback_preserving_error(database)
            raise

    def case_ids(self) -> tuple[str, ...]:
        return tuple(
            row[0]
            for row in self._require_open().execute(
                "SELECT case_token FROM pb_cases ORDER BY rowid"
            )
        )

    def record_webhook(
        self,
        provider: str,
        event_id: str,
        case_id: str | None,
        payload: str,
        *,
        letter_event: Event | None = None,
        payment_event: PaymentEvent | None = None,
        occurred_at: datetime,
        new_effect_id: Callable[[], str],
        submission_acceptance: Accepted | None = None,
        submission_token: str | None = None,
        expected_envelope: str | None = None,
        submission_identity: tuple[int, str] | None = None,
    ) -> bool:
        """Persist early evidence, or atomically bind it with dedup and machine events.

        A deferred payload is the reduced, verified envelope, never the raw body.
        Binding removes that envelope only in the same commit as its effects.
        """
        db = self._require_open()
        db.execute("BEGIN IMMEDIATE")
        try:
            if settled_webhook(db, provider, event_id):
                db.execute("COMMIT")
                return False
            pinned = None
            if expected_envelope is not None:
                if provider != "lob":
                    raise ValueError("only Lob envelopes can be pinned")
                pinned = pin_webhook_envelope(
                    db, provider, event_id, expected_envelope, occurred_at
                )
            if case_id is None:
                inserted = (
                    park_webhook(db, provider, event_id, payload, occurred_at)
                    if pinned is None
                    else pinned
                )
                db.execute("COMMIT")
                return inserted
            if case_id is not None:
                case = self.get_case(case_id)
                assert case is not None
                if submission_identity is not None:
                    identity_attempt, identity_letter = submission_identity
                    identity_cursor = self.submission_cursor(case_id, identity_attempt)
                    identity_request = self.get(case_id, identity_attempt)
                    if (
                        provider != "lob"
                        or identity_cursor is None
                        or identity_request is None
                        or not isinstance(identity_cursor.outcome, Accepted | Uncertain)
                        or identity_cursor.outcome.letter_id not in (None, identity_letter)
                        or (
                            identity_cursor.outcome.letter_id is None
                            and (
                                submission_token != identity_request.request.submission_token
                                or db.execute(
                                    "SELECT 1 FROM pb_webhook_backlog "
                                    "WHERE submission_token=? AND letter_id<>? LIMIT 1",
                                    (submission_token, identity_letter),
                                ).fetchone()
                                is not None
                            )
                        )
                    ):
                        raise RevisionConflict("webhook submission identity changed")
                if submission_acceptance is not None:
                    cursor = self.submission_cursor(case_id, case.attempt)
                    frozen = self.get(case_id, case.attempt)
                    if (
                        provider != "lob"
                        or cursor is None
                        or not pending_evidence(cursor.outcome)
                        or not isinstance(cursor.outcome, Accepted | Uncertain)
                        or cursor.outcome.letter_id not in (None, submission_acceptance.letter_id)
                        or (
                            cursor.outcome.letter_id is None
                            and (
                                frozen is None
                                or submission_token != frozen.request.submission_token
                            )
                        )
                    ):
                        raise ValueError("scan requires exact pending original letter")
                    claimed, _kind = self.claim_submission(
                        case_id,
                        case.attempt,
                        case.revision,
                        cursor.revision,
                        now=occurred_at,
                        operation_id=new_effect_id(),
                        evidence=True,
                        manage_transaction=False,
                    )
                    self.finish_submission(
                        claimed,
                        submission_acceptance,
                        now=occurred_at,
                        effect_ids=(),
                        new_effect_id=new_effect_id,
                        manage_transaction=False,
                    )
                    case = self.get_case(case_id)
                    assert case is not None
                if (
                    isinstance(letter_event, ReturnConfirmed)
                    and case.letter_state is CaseState.RETURNED
                ):
                    # finish_submission already projected the same exact return scan.
                    letter_event = None
                for event, letter in ((letter_event, True), (payment_event, False)):
                    if event is not None:
                        case = self._apply_event(
                            case_id,
                            event,
                            case.revision,
                            letter,
                            occurred_at,
                            (),
                            manage_transaction=False,
                            new_effect_id=new_effect_id,
                        )
            db.execute(
                "INSERT INTO pb_webhooks VALUES (?, ?, ?, ?)",
                (provider, event_id, case_id, payload),
            )
            db.execute(
                "DELETE FROM pb_webhook_backlog WHERE provider=? AND event_id=?",
                (provider, event_id),
            )
            db.execute("COMMIT")
            return True
        except BaseException:
            rollback_preserving_error(db)
            raise

    def _iter_tracking_payloads(self, case_id: str, *, newest_first: bool = False) -> Iterator[str]:
        """Stream Lob evidence in stored order; recent lookups can stop at their match."""
        bound = _limit_clause(None, ("rowid",), newest_first=newest_first)
        for (payload,) in self._require_open().execute(
            "SELECT payload FROM pb_webhooks WHERE provider='lob' AND case_token=?" + bound[0],
            (case_id,),
        ):
            yield payload

    def tracking_payloads(self, case_id: str) -> tuple[str, ...]:
        return tuple(self._iter_tracking_payloads(case_id))

    def dispute_facts(self, case_id: str) -> tuple[DisputeFact, ...]:
        """Recorded dispute and fraud-warning facts for one case, in arrival order."""
        facts = []
        for (payload,) in self._require_open().execute(
            "SELECT payload FROM pb_webhooks WHERE provider='stripe' AND case_token=? "
            "ORDER BY rowid",
            (_token(case_id, "case_id"),),
        ):
            try:
                data = json.loads(payload)
                if isinstance(data, dict) and "kind" in data:
                    facts.append(DisputeFact.from_json(payload))
            except (ValueError, TypeError) as exc:
                raise StorageCorruption(f"invalid stored dispute fact: {exc}") from exc
        return tuple(facts)

    def mail_failure_recorded(self, case_id: str, attempt: int) -> bool:
        """Only identity-checked ingress is failure evidence; old rows are unverified."""
        cursor = self.submission_cursor(case_id, attempt)
        if cursor is None or not isinstance(cursor.outcome, Accepted | Uncertain):
            return False
        return any(
            event.get("event") in {"letter.deleted", "letter.failed"}
            and event.get("attempt") == attempt
            and event.get("letter_id") == cursor.outcome.letter_id
            and event.get("failure_evidence") is True
            for event in (
                json.loads(payload)
                for payload in self._iter_tracking_payloads(case_id, newest_first=True)
            )
        )

    def usps_scans(self, case_id: str, attempt: int = 1) -> tuple[UspsScan, ...]:
        """Exact-attempt scan proof from the existing operations and webhook evidence.

        Read-only projection: no second evidence store or transaction. Names, USPS
        times and details remain provider data; missing fields stay absent.
        """
        if type(attempt) is not int or attempt not in (1, 2):
            raise ValueError("invalid mail attempt")
        cursor = self.submission_cursor(case_id, attempt)
        if cursor is None or not isinstance(cursor.outcome, Accepted | Uncertain):
            return ()
        original_id = cursor.outcome.letter_id
        if original_id is None:
            return ()
        scans: dict[str, UspsScan] = {}

        def retain(items: tuple[UspsScan, ...]) -> None:
            for scan in items:
                scans.setdefault(json.dumps(scan.as_sent(), sort_keys=True), scan)

        for (text,) in self._require_open().execute(
            "SELECT outcome_json FROM pb_submission_operations "
            "WHERE case_token=? AND attempt=? AND outcome_json IS NOT NULL ORDER BY rowid",
            (case_id, attempt),
        ):
            outcome = decode_outcome(text)
            if isinstance(outcome, Accepted) and outcome.letter_id == original_id:
                retain(outcome.scans)
        for payload in self._iter_tracking_payloads(case_id):
            data = json.loads(payload)
            if (
                data.get("attempt") == attempt
                and data.get("letter_id") == original_id
                and data.get("scan_evidence") is True
            ):
                retain(scan_proof(data.get("tracking_events")))
        return tuple(scans.values())

    def usps_scan_proof(
        self, case_id: str, attempt: int = 1
    ) -> tuple[tuple[str, str | None, dict[str, object] | None], ...]:
        """Provider event type, USPS time and details for one exact attempt."""
        return tuple(
            (scan.event_type, scan.time, None if scan.details is None else dict(scan.details))
            for scan in self.usps_scans(case_id, attempt)
        )


_SUBMISSION_OPERATIONS_V1 = (
    "operation_id",
    "case_token",
    "attempt",
    "cursor_revision",
    "kind",
    "started_at",
    "outcome_json",
    "completed_at",
)
_SUBMISSION_OPERATIONS_V2 = (*_SUBMISSION_OPERATIONS_V1, "temporary_lookup")
_SUBMISSION_OPERATIONS_V2_DDL = """CREATE TABLE pb_submission_operations (
            operation_id TEXT PRIMARY KEY, case_token TEXT NOT NULL,
            attempt INTEGER NOT NULL, cursor_revision INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('create', 'lookup')), started_at TEXT NOT NULL,
            outcome_json TEXT, completed_at TEXT,
            temporary_lookup INTEGER NOT NULL DEFAULT 0
                CHECK (temporary_lookup IN (0, 1) AND (temporary_lookup = 0 OR kind = 'lookup')),
            FOREIGN KEY (case_token, attempt) REFERENCES pb_mail_attempts(case_token, attempt),
            CHECK ((outcome_json IS NULL) = (completed_at IS NULL))
        )"""
_SUBMISSION_ADD_TEMPORARY_LOOKUP = (
    "ALTER TABLE pb_submission_operations ADD COLUMN temporary_lookup INTEGER NOT NULL "
    "DEFAULT 0 CHECK (temporary_lookup IN (0, 1) AND (temporary_lookup = 0 OR kind = 'lookup'))"
)


def _check_submission_operations(database: sqlite3.Connection, *, version: int) -> None:
    """Strict operation-table validation for exactly schema version 1 or 2.

    The v2 marker is appended last with a default, so the shared default-free
    ``_check_table`` is not enough here; every other column/constraint is still exact.
    """
    if version not in (1, 2):
        raise StorageCorruption("unsupported submission schema version")
    columns = _SUBMISSION_OPERATIONS_V1 if version == 1 else _SUBMISSION_OPERATIONS_V2
    types = (
        "TEXT",
        "TEXT",
        "INTEGER",
        "INTEGER",
        "TEXT",
        "TEXT",
        "TEXT",
        "TEXT",
    ) + (("INTEGER",) if version == 2 else ())
    nullable = {"operation_id", "outcome_json", "completed_at"}
    rows = tuple(database.execute("PRAGMA table_info(pb_submission_operations)").fetchall())
    if tuple(row[1] for row in rows) != columns:
        raise StorageCorruption("unknown pb_submission_operations column layout")
    for row, expected_type in zip(rows, types, strict=True):
        _, name, column_type, required, default, primary_key = row
        if column_type != expected_type:
            raise StorageCorruption(f"invalid pb_submission_operations type: {name}")
        if name == "temporary_lookup":
            if default != "0":
                raise StorageCorruption("invalid temporary_lookup default")
        elif default is not None:
            raise StorageCorruption(f"invalid pb_submission_operations default: {name}")
        if required != (0 if name in nullable else 1):
            raise StorageCorruption(f"invalid pb_submission_operations nullability: {name}")
        if primary_key != (1 if name == "operation_id" else 0):
            raise StorageCorruption(f"invalid pb_submission_operations primary key: {name}")
    sql = " ".join(
        str(
            database.execute(
                "SELECT sql FROM sqlite_master WHERE name='pb_submission_operations'"
            ).fetchone()[0]
        )
        .upper()
        .split()
    )
    constraints = [
        "CHECK (KIND IN ('CREATE', 'LOOKUP'))",
        "CHECK ((OUTCOME_JSON IS NULL) = (COMPLETED_AT IS NULL))",
    ]
    if version == 2:
        constraints.append(
            "CHECK (TEMPORARY_LOOKUP IN (0, 1) AND (TEMPORARY_LOOKUP = 0 OR KIND = 'LOOKUP'))"
        )
    if any(item not in sql for item in constraints):
        raise StorageCorruption("missing pb_submission_operations constraints")
    foreign_keys = {
        ("pb_mail_attempts", "case_token", "case_token"),
        ("pb_mail_attempts", "attempt", "attempt"),
    }
    fk = tuple(database.execute("PRAGMA foreign_key_list(pb_submission_operations)"))
    if len(fk) != len(foreign_keys) or {tuple(row[2:5]) for row in fk} != foreign_keys:
        raise StorageCorruption("invalid pb_submission_operations foreign keys")


def _initialize_submission(database: sqlite3.Connection) -> None:
    version = database.execute(
        "SELECT version FROM pb_store_versions WHERE component='submission'"
    ).fetchone()
    tables = {
        row[0]
        for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'pb_submission_%'"
        )
    }
    if version is None:
        if tables:
            raise StorageCorruption("unversioned submission schema")
        database.execute("""CREATE TABLE pb_submission_cursors (
            case_token TEXT NOT NULL REFERENCES pb_cases(case_token),
            attempt INTEGER NOT NULL CHECK (attempt IN (1, 2)),
            first_sent_at TEXT NOT NULL, sends INTEGER NOT NULL CHECK (sends >= 1),
            next_at TEXT NOT NULL, lookup_only INTEGER NOT NULL CHECK (lookup_only IN (0, 1)),
            revision INTEGER NOT NULL CHECK (revision >= 1), owner TEXT,
            case_revision INTEGER NOT NULL CHECK (case_revision >= 1), outcome_json TEXT NOT NULL,
            PRIMARY KEY (case_token, attempt),
            FOREIGN KEY (case_token, attempt) REFERENCES pb_mail_attempts(case_token, attempt)
        )""")
        database.execute(_SUBMISSION_OPERATIONS_V2_DDL)
        database.execute("INSERT INTO pb_store_versions VALUES ('submission', 2)")
        current_version = 2
        tables = {"pb_submission_cursors", "pb_submission_operations"}
    elif type(version[0]) is not int or version[0] not in (1, 2):
        raise StorageCorruption("unsupported submission schema version")
    else:
        current_version = version[0]
    if tables != {"pb_submission_cursors", "pb_submission_operations"}:
        raise StorageCorruption("incomplete submission schema")
    _check_table(
        database,
        "pb_submission_cursors",
        (
            "case_token",
            "attempt",
            "first_sent_at",
            "sends",
            "next_at",
            "lookup_only",
            "revision",
            "owner",
            "case_revision",
            "outcome_json",
        ),
        (
            "TEXT",
            "INTEGER",
            "TEXT",
            "INTEGER",
            "TEXT",
            "INTEGER",
            "INTEGER",
            "TEXT",
            "INTEGER",
            "TEXT",
        ),
        {"case_token": 1, "attempt": 2},
        frozenset(
            {
                "case_token",
                "attempt",
                "first_sent_at",
                "sends",
                "next_at",
                "lookup_only",
                "revision",
                "case_revision",
                "outcome_json",
            }
        ),
    )
    cursor_sql = " ".join(
        str(
            database.execute(
                "SELECT sql FROM sqlite_master WHERE name='pb_submission_cursors'"
            ).fetchone()[0]
        )
        .upper()
        .split()
    )
    for item in (
        "CHECK (ATTEMPT IN (1, 2))",
        "CHECK (SENDS >= 1)",
        "CHECK (LOOKUP_ONLY IN (0, 1))",
        "CHECK (REVISION >= 1)",
        "CHECK (CASE_REVISION >= 1)",
    ):
        if item not in cursor_sql:
            raise StorageCorruption("missing pb_submission_cursors constraints")
    cursor_fk = tuple(database.execute("PRAGMA foreign_key_list(pb_submission_cursors)"))
    expected_cursor_fk = {
        ("pb_cases", "case_token", "case_token"),
        ("pb_mail_attempts", "case_token", "case_token"),
        ("pb_mail_attempts", "attempt", "attempt"),
    }
    if (
        len(cursor_fk) != len(expected_cursor_fk)
        or {tuple(row[2:5]) for row in cursor_fk} != expected_cursor_fk
    ):
        raise StorageCorruption("invalid pb_submission_cursors foreign keys")

    if current_version == 1:
        # Validate the exact canonical v1 table before touching it. A v1 table that
        # already carries the marker is not canonical and fails closed here.
        _check_submission_operations(database, version=1)
        database.execute(_SUBMISSION_ADD_TEMPORARY_LOOKUP)
        database.execute("UPDATE pb_store_versions SET version=2 WHERE component='submission'")
        current_version = 2
    _check_submission_operations(database, version=current_version)

    # Historical render-only acceptance could already have requested capture. Its
    # provider outcome needs operator reconciliation, never a new release transition.
    for (encoded,) in database.execute(
        "SELECT s.outcome_json FROM pb_submission_cursors s "
        "JOIN pb_cases c ON c.case_token=s.case_token WHERE c.payment_state=?",
        (PaymentState.CAPTURE_PENDING.value,),
    ):
        try:
            outcome = decode_outcome(encoded)
        except (ValueError, TypeError) as exc:
            raise StorageCorruption("invalid legacy capture outcome") from exc
        if isinstance(outcome, Accepted) and outcome.evidence is None:
            raise StorageCorruption("legacy capture without mail evidence")


_PAYMENT_DDL = (
    """CREATE TABLE pb_payment_cursor (
        case_token TEXT PRIMARY KEY REFERENCES pb_cases(case_token), payload TEXT NOT NULL
    )""",
    """CREATE TABLE pb_payment_operations (
        operation_id TEXT PRIMARY KEY, case_token TEXT NOT NULL REFERENCES pb_cases(case_token),
        intent TEXT NOT NULL, result TEXT, completed_at TEXT,
        CHECK ((result IS NULL) = (completed_at IS NULL))
    )""",
)


def _initialize_payment(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='payment'"
        ).fetchone()
        tables = {
            r[0]: r[1]
            for r in database.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'pb_payment_%'"
            )
        }
        if version is None:
            if tables:
                raise StorageCorruption("unversioned payment schema")
            for ddl in _PAYMENT_DDL:
                database.execute(ddl)
            database.execute("INSERT INTO pb_store_versions VALUES ('payment', 1)")
        elif type(version[0]) is not int or version[0] != 1:
            raise StorageCorruption("unsupported payment schema")
        else:
            expected = {ddl.split()[2]: " ".join(ddl.upper().split()) for ddl in _PAYMENT_DDL}
            if {name: " ".join(sql.upper().split()) for name, sql in tables.items()} != expected:
                raise StorageCorruption("malformed payment schema")
        if database.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StorageCorruption("invalid payment foreign keys")
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


_WEBHOOK_DDL = """CREATE TABLE pb_webhooks (
    provider TEXT NOT NULL CHECK (provider IN ('stripe', 'lob')),
    event_id TEXT NOT NULL,
    case_token TEXT NOT NULL REFERENCES pb_cases(case_token),
    payload TEXT NOT NULL,
    PRIMARY KEY(provider, event_id)
)"""


def _initialize_webhooks(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='webhooks'"
        ).fetchone()
        table = database.execute(
            "SELECT sql FROM sqlite_master WHERE name='pb_webhooks'"
        ).fetchone()
        if version is None:
            if table is not None:
                raise StorageCorruption("unversioned webhook schema")
            database.execute(_WEBHOOK_DDL)
            database.execute("INSERT INTO pb_store_versions VALUES ('webhooks', 1)")
        elif (
            version != (1,)
            or table is None
            or (" ".join(table[0].upper().split()) != " ".join(_WEBHOOK_DDL.upper().split()))
        ):
            raise StorageCorruption("unsupported webhook schema")
        database.execute("COMMIT")
    except BaseException:
        rollback_preserving_error(database)
        raise
