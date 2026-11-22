"""The T75 read feed: opaque identifiers and closed operational categories only.

Identifier setup is a write-side operation: application setup backfills existing cases,
and case creation stores an identifier in its own transaction. GET opens a separate
``mode=ro`` connection and observes one SQLite snapshot. Both customer and worker
processes must configure the same identifier key/version, although only workers expose
the feed. A version is permanently bound to one key; rotation uses a new version and
does not rename existing identifiers or revoke previously configured versions.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import shutil
import sqlite3
from collections import Counter
from contextlib import closing, suppress
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from parkingbreaker.case_hold_store import HoldReason
from parkingbreaker.case_state import CaseState
from parkingbreaker.health import (
    SCHEDULER_STALE_AFTER,
    SWEEP_FAILING_AFTER,
    SWEEP_INTERRUPTED_AFTER,
    AlertKind,
    DiskObservation,
    HealthSnapshot,
    Severity,
    StuckWhy,
    evaluate,
)
from parkingbreaker.health_service import (
    _captures,
    _holds,
    _last_backup,
    _stuck,
    _sweep,
    _webhook_backlog,
)
from parkingbreaker.lob_submission import pending_evidence
from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.oplog.ai_journal import AiCall
from parkingbreaker.payment_records import decode_cursor
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.submission_records import decode_outcome
from parkingbreaker.sweep_run_store import SweepOutcome

MAX_PAGE = 100
OPS_ID_PATTERN = r"^op[1-9][0-9]{0,4}_[0-9a-f]{32}$"
_OPS_ID = re.compile(OPS_ID_PATTERN)
_DOMAIN = b"parkingbreaker/overseer-ops-id\x00"
_KEY_DOMAIN = b"parkingbreaker/overseer-key-binding\x00"
_KEYS_DDL = """CREATE TABLE pb_overseer_key_versions (
    key_version INTEGER PRIMARY KEY CHECK (key_version BETWEEN 1 AND 65535),
    key_tag TEXT NOT NULL UNIQUE
)"""
_OPS_DDL = """CREATE TABLE pb_case_ops_ids (
    case_token TEXT PRIMARY KEY REFERENCES pb_cases(case_token),
    ops_id TEXT NOT NULL UNIQUE,
    key_version INTEGER NOT NULL REFERENCES pb_overseer_key_versions(key_version)
)"""


class FreshnessState(StrEnum):
    NO_RUN = "no_run"
    IN_PROGRESS = "in_progress"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    PARTIAL = "partial"
    STALE = "stale"
    FRESH = "fresh"


class SweepErrorClass(StrEnum):
    FAILED = "sweep_failed"
    PARTIAL = "sweep_partial"
    INTERRUPTED = "sweep_interrupted"


class CaseErrorClass(StrEnum):
    """Observed state classes, without guessing a private or unrecorded root cause."""

    STOPPED_NO_CHARGE = "stopped_no_charge"
    PAYMENT_RETRYING = "payment_retrying"
    PAYMENT_WRITTEN_OFF = "payment_written_off"


class HealthDomain(StrEnum):
    SWEEP = "sweep"
    CASES = "cases"
    CAPTURES = "captures"
    DISK = "disk"
    BACKUP = "backup"
    WEBHOOKS = "webhooks"
    HOLDS = "holds"


@dataclass(frozen=True)
class FeedFreshness:
    state: FreshnessState
    stale: bool
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_successful_finished_at: datetime | None
    last_outcome: SweepOutcome | None
    unfinished_runs: int
    error_class: SweepErrorClass | None


@dataclass(frozen=True)
class FeedAlert:
    kind: AlertKind
    severity: Severity
    domain: HealthDomain
    count: int


@dataclass(frozen=True)
class FeedCase:
    ops_id: str
    letter_state: CaseState
    payment_state: PaymentState
    attempt: int
    age_hours: int
    payment_due_at: datetime | None
    submission_due_at: datetime | None
    held: bool
    hold_generation: int | None
    hold_reason: HoldReason | None
    review_due_at: datetime | None
    stuck: StuckWhy | None
    error_class: CaseErrorClass | None
    ai_settled_cents: int
    ai_unsettled_reserved_cents: int
    ai_unknown_cost_calls: int


@dataclass(frozen=True)
class FeedPage:
    generated_at: datetime
    freshness: FeedFreshness
    alerts: tuple[FeedAlert, ...]
    cases: tuple[FeedCase, ...]
    next_after: str | None


class _Connected(Protocol):
    path: Path
    _overseer_key: tuple[bytes, int] | None

    def _require_open(self) -> sqlite3.Connection: ...


def ops_id(key: bytes, case_token: str, version: int = 1) -> str:
    if len(key) != 32 or type(version) is not int or not 1 <= version <= 65535:
        raise ValueError("invalid operations ID key or version")
    message = _DOMAIN + version.to_bytes(2, "big") + case_token.encode()
    return f"op{version}_" + hmac.new(key, message, hashlib.sha256).hexdigest()[:32]


def initialize_ops_ids(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")
    try:
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='overseer_ops_ids'"
        ).fetchone()
        for table, ddl in (
            ("pb_overseer_key_versions", _KEYS_DDL),
            ("pb_case_ops_ids", _OPS_DDL),
        ):
            found = database.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if version is None:
                if found is not None:
                    raise StorageCorruption("unversioned overseer identifier schema")
                database.execute(ddl)
            elif (
                version != (1,)
                or found is None
                or " ".join(found[0].upper().split()) != " ".join(ddl.upper().split())
            ):
                raise StorageCorruption("malformed overseer identifier schema")
            for action in ("UPDATE", "DELETE"):
                database.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                    f"BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'overseer identifiers are immutable'); END"
                )
        if version is None:
            database.execute("INSERT INTO pb_store_versions VALUES ('overseer_ops_ids', 1)")
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def _identifier(value: object, version: object) -> str:
    if (
        not isinstance(value, str)
        or _OPS_ID.fullmatch(value) is None
        or type(version) is not int
        or not 1 <= version <= 65535
        or not value.startswith(f"op{version}_")
    ):
        raise StorageCorruption("invalid operations identifier")
    return value


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise StorageCorruption("invalid feed timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StorageCorruption("invalid feed timestamp")
    return parsed


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _time(value)


def _count(value: object, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise StorageCorruption("invalid feed count")
    return value


def _health(database: sqlite3.Connection, path: Path, now: datetime) -> HealthSnapshot:
    # Database-backed T24 observations share the feed transaction. Filesystem metadata
    # and live lock probes are sampled while it is held, as in the independent monitor.
    disk = None
    try:
        usage = shutil.disk_usage(path.parent)
        disk = DiskObservation(usage.free, usage.total)
    except OSError:
        pass
    backup = None
    no_backup = False
    with suppress(OSError, ValueError):
        backup, no_backup = _last_backup(path.parent / "recovery")
    sweep = _sweep(database, now)
    if sweep.last_outcome is not None:
        SweepOutcome(sweep.last_outcome)  # refuse arbitrary values from a corrupt row
    return HealthSnapshot(
        sweep,
        _stuck(database, now, path),
        _captures(database, now),
        disk,
        backup,
        no_backup,
        _webhook_backlog(database),
        True,
        _holds(database),
    )


_ALERT_DOMAIN = {
    AlertKind.SCHEDULER_STALE: HealthDomain.SWEEP,
    AlertKind.SWEEP_FAILING: HealthDomain.SWEEP,
    AlertKind.SWEEP_INTERRUPTED: HealthDomain.SWEEP,
    AlertKind.CASE_STUCK: HealthDomain.CASES,
    AlertKind.CAPTURE_FAILURES: HealthDomain.CAPTURES,
    AlertKind.DISK_LOW: HealthDomain.DISK,
    AlertKind.BACKUP_STALE: HealthDomain.BACKUP,
    AlertKind.OFF_HOST_REPLICA_STALE: HealthDomain.BACKUP,
    AlertKind.WEBHOOK_BACKLOG: HealthDomain.WEBHOOKS,
    AlertKind.HOLD_REVIEW_DUE: HealthDomain.HOLDS,
}


def _alerts(snapshot: HealthSnapshot, now: datetime) -> tuple[FeedAlert, ...]:
    counts: Counter[tuple[AlertKind, Severity, HealthDomain]] = Counter()
    for alert in evaluate(snapshot, now):
        domain = (
            HealthDomain(alert.subject)
            if alert.kind is AlertKind.OBSERVATION_UNKNOWN
            else _ALERT_DOMAIN[alert.kind]
        )
        counts[alert.kind, alert.severity, domain] += 1
    return tuple(FeedAlert(*key, count) for key, count in sorted(counts.items()))


def _freshness(database: sqlite3.Connection, now: datetime) -> FeedFreshness:
    latest = database.execute(
        "SELECT started_at, finished_at, outcome FROM pb_sweep_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    (unfinished,) = database.execute(
        "SELECT COUNT(*) FROM pb_sweep_runs WHERE finished_at IS NULL"
    ).fetchone()
    success = database.execute(
        "SELECT finished_at FROM pb_sweep_runs WHERE outcome='ok' ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    last_ok = None if success is None else _time(success[0])
    if latest is None:
        return FeedFreshness(FreshnessState.NO_RUN, True, None, None, None, None, 0, None)
    started, finished = _time(latest[0]), _optional_time(latest[1])
    outcome = None if latest[2] is None else SweepOutcome(latest[2])
    if (finished is None) != (outcome is None):
        raise StorageCorruption("invalid sweep completion")
    stale = (
        now - started > SCHEDULER_STALE_AFTER
        or last_ok is None
        or now - last_ok > SWEEP_FAILING_AFTER
    )
    error = None
    if finished is None:
        if now - started > SWEEP_INTERRUPTED_AFTER:
            state, error = FreshnessState.INTERRUPTED, SweepErrorClass.INTERRUPTED
        else:
            state = FreshnessState.IN_PROGRESS
    elif outcome is SweepOutcome.FAILED:
        state, error = FreshnessState.FAILED, SweepErrorClass.FAILED
    elif outcome is SweepOutcome.PARTIAL:
        state, error = FreshnessState.PARTIAL, SweepErrorClass.PARTIAL
    else:
        state = FreshnessState.STALE if stale else FreshnessState.FRESH
    return FeedFreshness(state, stale, started, finished, last_ok, outcome, unfinished, error)


def _case(database: sqlite3.Connection, row: tuple[object, ...], now: datetime) -> FeedCase:
    op, version, token, letter, payment, attempt, authorized, held, generation, reason, review = row
    identifier = _identifier(op, version)
    if (
        not isinstance(letter, str)
        or not isinstance(payment, str)
        or (reason is not None and not isinstance(reason, str))
    ):
        raise StorageCorruption("invalid feed category")
    letter_state, payment_state = CaseState(letter), PaymentState(payment)
    error_class = {
        PaymentState.CAPTURE_RETRYING: CaseErrorClass.PAYMENT_RETRYING,
        PaymentState.WRITTEN_OFF: CaseErrorClass.PAYMENT_WRITTEN_OFF,
    }.get(payment_state)
    if error_class is None and letter_state is CaseState.STOPPED_NO_CHARGE:
        error_class = CaseErrorClass.STOPPED_NO_CHARGE
    attempt_count = _count(attempt, 1)
    if attempt_count not in (1, 2) or held not in (None, 0, 1):
        raise StorageCorruption("invalid feed case")
    intake = database.execute(
        "SELECT intake_id FROM pb_intake_records WHERE case_token=?", (token,)
    ).fetchone()
    subjects = (token,) if intake is None else (token, intake[0])
    marks = ", ".join("?" for _ in subjects)
    settled, reserved = database.execute(
        "SELECT COALESCE(SUM(settled_cents), 0), "
        "COALESCE(SUM(CASE WHEN settled_cents IS NULL THEN reserved_cents END), 0) "
        f"FROM pb_ai_spend WHERE subject_token IN ({marks})",
        subjects,
    ).fetchone()
    unknown = sum(
        AiCall.from_json(entry).cost_cents is None
        for (entry,) in database.execute(
            f"SELECT entry_json FROM pb_ai_calls WHERE subject_token IN ({marks})", subjects
        )
    )
    cursor = database.execute(
        "SELECT payload FROM pb_payment_cursor WHERE case_token=?", (token,)
    ).fetchone()
    payment_cursor = None if cursor is None else decode_cursor(cursor[0])
    submission = database.execute(
        "SELECT next_at, outcome_json FROM pb_submission_cursors WHERE case_token=? AND attempt=?",
        (token, attempt_count),
    ).fetchone()
    submission_due = (
        _time(submission[0])
        if submission is not None and pending_evidence(decode_outcome(submission[1]))
        else None
    )
    return FeedCase(
        identifier,
        letter_state,
        payment_state,
        attempt_count,
        max(0, int((now - _time(authorized)).total_seconds())) // 3600,
        None if payment_cursor is None or payment_cursor.done else payment_cursor.next_at,
        submission_due,
        held == 1,
        None if generation is None else _count(generation, 1),
        None if reason is None else HoldReason(reason),
        _optional_time(review),
        None,
        error_class,
        _count(settled),
        _count(reserved),
        unknown,
    )


class OverseerFeedStoreMixin:
    _overseer_key: tuple[bytes, int] | None

    def configure_ops_ids(self: _Connected, key: bytes, version: int = 1) -> None:
        """Startup backfill; both API roles call this before accepting case writes."""
        ops_id(key, "", version)  # validate without including a private identifier
        database = self._require_open()
        if database.in_transaction:
            raise StorageCorruption("identifier setup needs its own transaction")
        tag = hmac.new(key, _KEY_DOMAIN, hashlib.sha256).hexdigest()
        database.execute("BEGIN IMMEDIATE")
        try:
            known = database.execute(
                "SELECT key_tag FROM pb_overseer_key_versions WHERE key_version=?", (version,)
            ).fetchone()
            if known is not None and not hmac.compare_digest(known[0], tag):
                raise StorageCorruption("overseer key version is already bound")
            if known is None:
                if database.execute(
                    "SELECT 1 FROM pb_overseer_key_versions WHERE key_tag=?", (tag,)
                ).fetchone():
                    raise StorageCorruption("overseer key is already bound to another version")
                database.execute(
                    "INSERT INTO pb_overseer_key_versions VALUES (?, ?)", (version, tag)
                )
            for token, identifier, stored_version in database.execute(
                "SELECT case_token, ops_id, key_version FROM pb_case_ops_ids"
            ):
                _identifier(identifier, stored_version)
                if stored_version == version and not hmac.compare_digest(
                    identifier, ops_id(key, token, version)
                ):
                    raise StorageCorruption("invalid operations identifier")
            database.executemany(
                "INSERT INTO pb_case_ops_ids VALUES (?, ?, ?)",
                [
                    (token, ops_id(key, token, version), version)
                    for (token,) in database.execute(
                        "SELECT case_token FROM pb_cases WHERE case_token NOT IN "
                        "(SELECT case_token FROM pb_case_ops_ids)"
                    )
                ],
            )
            database.execute("COMMIT")
        except Exception:
            if database.in_transaction:
                rollback_preserving_error(database)
            raise
        self._overseer_key = (key, version)

    def _insert_ops_id(self: _Connected, case_token: str) -> None:
        """Called only inside the case creator's existing write transaction."""
        if self._overseer_key is not None:
            key, version = self._overseer_key
            self._require_open().execute(
                "INSERT INTO pb_case_ops_ids VALUES (?, ?, ?)",
                (case_token, ops_id(key, case_token, version), version),
            )

    def case_for_ops_id(self: _Connected, value: str) -> str | None:
        row = (
            self._require_open()
            .execute("SELECT case_token FROM pb_case_ops_ids WHERE ops_id=?", (value,))
            .fetchone()
        )
        return None if row is None else str(row[0])

    def overseer_page(self: _Connected, now: datetime, after: str | None, limit: int) -> FeedPage:
        """No writes, migrations, providers or case refreshes occur on this path."""
        if not 1 <= limit <= MAX_PAGE or (after is not None and _OPS_ID.fullmatch(after) is None):
            raise ValueError("invalid feed pagination")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("feed time must be aware")
        self._require_open()
        uri = self.path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)) as database:
            database.execute("BEGIN")
            try:
                if database.execute(
                    "SELECT 1 FROM pb_cases c LEFT JOIN pb_case_ops_ids o "
                    "ON o.case_token=c.case_token WHERE o.ops_id IS NULL LIMIT 1"
                ).fetchone():
                    raise StorageCorruption("operations identifiers need startup backfill")
                rows = database.execute(
                    "SELECT o.ops_id, o.key_version, c.case_token, c.letter_state, "
                    "c.payment_state, c.attempt, c.authorized_at, h.held, h.generation, "
                    "h.reason_code, h.review_due_at "
                    "FROM pb_case_ops_ids o JOIN pb_cases c ON c.case_token=o.case_token "
                    "LEFT JOIN pb_case_holds h ON h.case_token=c.case_token "
                    "WHERE o.ops_id > ? ORDER BY o.ops_id LIMIT ?",
                    ("" if after is None else after, limit + 1),
                ).fetchall()
                snapshot = _health(database, self.path, now)
                why = {item.case_token: item.why for item in snapshot.stuck or ()}
                cases = tuple(_case(database, row, now) for row in rows[:limit])
                cases = tuple(
                    replace(case, stuck=why.get(str(row[2])))
                    for case, row in zip(cases, rows[:limit], strict=True)
                )
                page = FeedPage(
                    now,
                    _freshness(database, now),
                    _alerts(snapshot, now),
                    cases,
                    cases[-1].ops_id if len(rows) > limit and cases else None,
                )
                database.execute("COMMIT")
                return page
            except Exception:
                if database.in_transaction:
                    rollback_preserving_error(database)
                raise
