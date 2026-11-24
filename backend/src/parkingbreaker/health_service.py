"""Gather a health snapshot read-only, and deliver alerts as durable incidents (T24).

The monitor opens the application database read-only (SQLite ``mode=ro``) and never
creates, migrates or writes it. Each observation is gathered on its own; one that cannot
be read becomes unknown in the snapshot rather than healthy. Incident state lives in the
monitor's own small database, so the monitor never needs write access to case data.

Each open incident sends one notice, re-sends (escalates) every ``ESCALATE_EVERY`` while
it stays open, and sends one recovery notice when it clears. A notice that the email
provider does not accept is retried on the next poll under the same idempotency key.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path

from parkingbreaker.case_state import TERMINAL_STATES, CaseState, StopFamily
from parkingbreaker.draft_ownership_store import drafting_owners
from parkingbreaker.email_client import EmailMessage, EmailOutcome
from parkingbreaker.health import (
    CAPTURE_WINDOW,
    Alert,
    AlertKind,
    CaptureObservation,
    DiskObservation,
    HealthSnapshot,
    HoldObservation,
    OperationalObservation,
    StuckCase,
    StuckWhy,
    SweepObservation,
)
from parkingbreaker.lob_submission import pending_evidence
from parkingbreaker.oplog.counters import decode_counter_key
from parkingbreaker.oplog.digest import CounterMilestone
from parkingbreaker.payment_records import decode_cursor
from parkingbreaker.submission_records import decode_outcome
from parkingbreaker.webhook_backlog_store import CONFLICTING_LETTER, CONTRADICTS_CASE_STATE

DATABASE = "parkingbreaker.sqlite"
# Outbox effects and submission follow-ups this far past due are stuck.
EFFECT_OVERDUE_AFTER = timedelta(minutes=15)
# Research, drafting and submitting are automated; the 30-minute customer window has
# long ended for one still sitting here this long after its last transition.
AUTOMATED_STEP_OVERDUE_AFTER = timedelta(minutes=45)
ESCALATE_EVERY = timedelta(hours=6)
_AUTOMATED = ("RESEARCHING", "DRAFTING", "SUBMITTING")
_FINISHED = tuple(sorted(state.value for state in TERMINAL_STATES))
_BUNDLE_TIME = re.compile(r"(\d{8}T\d{6}Z)")

Notifier = Callable[[EmailMessage, str], EmailOutcome]


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed


@contextmanager
def _read_only(database: Path) -> Iterator[sqlite3.Connection]:
    if not database.is_file():
        raise FileNotFoundError(database)
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)) as connection:
        yield connection


def _sweep(db: sqlite3.Connection, now: datetime) -> SweepObservation:
    last = db.execute(
        "SELECT started_at, outcome FROM pb_sweep_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    ok = db.execute(
        "SELECT finished_at FROM pb_sweep_runs WHERE outcome='ok' ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    unfinished = db.execute(
        "SELECT started_at FROM pb_sweep_runs WHERE finished_at IS NULL "
        "ORDER BY run_id DESC LIMIT 50"
    ).fetchall()
    return SweepObservation(
        None if last is None else _time(last[0]),
        None if ok is None else _time(ok[0]),
        None if last is None else last[1],
        tuple(_time(row[0]) for row in unfinished),
    )


def _holds(db: sqlite3.Connection) -> tuple[HoldObservation, ...]:
    """Held cases that may still need review; a finished case has nothing left to pause."""
    placeholders = ", ".join("?" for _ in _FINISHED)
    return tuple(
        HoldObservation(token, _time(due))
        for token, due in db.execute(
            "SELECT h.case_token, h.review_due_at FROM pb_case_holds h "
            "JOIN pb_cases c ON c.case_token = h.case_token "
            f"WHERE h.held = 1 AND c.letter_state NOT IN ({placeholders}) "
            "ORDER BY h.case_token",
            _FINISHED,
        )
    )


def _stuck(
    db: sqlite3.Connection, now: datetime, database: Path | None = None
) -> tuple[StuckCase, ...]:
    """Overdue by each item's own effective due time, never by age alone where one exists.

    Payment work is due at its cursor's next attempt (so scheduled backoff is not stuck);
    letter submission at its submission cursor's next lookup or send.
    """
    found: dict[str, StuckCase] = {}
    payment_due: dict[str, datetime] = {}
    for (payload,) in db.execute("SELECT payload FROM pb_payment_cursor"):
        cursor = decode_cursor(payload)
        if cursor.done:
            continue
        payment_due[cursor.effect_id] = cursor.next_at
        if now - cursor.next_at > EFFECT_OVERDUE_AFTER:
            found[cursor.case_token] = StuckCase(
                cursor.case_token, StuckWhy.EFFECT_OVERDUE, cursor.next_at
            )
    submitting: set[str] = set()
    pending_lookup: set[str] = set()
    for token, next_at, outcome in db.execute(
        "SELECT s.case_token, s.next_at, s.outcome_json FROM pb_submission_cursors s "
        "JOIN pb_cases c ON c.case_token=s.case_token AND c.attempt=s.attempt"
    ):
        submitting.add(token)
        if not pending_evidence(decode_outcome(outcome)):
            continue
        pending_lookup.add(token)
        due = _time(next_at)
        if now - due > EFFECT_OVERDUE_AFTER:
            found.setdefault(token, StuckCase(token, StuckWhy.SUBMISSION_OVERDUE, due))
    for effect_id, token, machine, effect, created in db.execute(
        "SELECT effect_id, case_token, machine, effect, created_at FROM pb_pending_effects "
        "WHERE machine IN ('letter', 'payment') AND outcome IS NULL"
    ):
        if effect in ("SUBMIT_LETTER", "SUBMIT_REMAIL") and token in submitting:
            continue
        scheduled = payment_due.get(effect_id) if machine == "payment" else None
        since = scheduled if scheduled is not None else _time(created)
        if now - since > EFFECT_OVERDUE_AFTER:
            found.setdefault(token, StuckCase(token, StuckWhy.EFFECT_OVERDUE, since))
    # A hold pauses research and drafting only, so a held case in those states is reported by
    # its review deadline instead (T74); a case whose per-case lock is live is being worked on
    # now. A hold does not pause mail, and a case waiting on a lookup is judged by that
    # lookup's own due time above. Overdue release, capture, refund or lookup work on a held
    # case keeps alerting.
    paused = {
        token for (token,) in db.execute("SELECT case_token FROM pb_case_holds WHERE held = 1")
    }
    if database is not None:
        paused |= drafting_owners(
            database, db.execute("SELECT case_token, slot FROM pb_draft_slots").fetchall()
        )
    placeholders = ", ".join("?" for _ in _AUTOMATED)
    for token, state, authorized, latest in db.execute(
        "SELECT c.case_token, c.letter_state, c.authorized_at, MAX(h.occurred_at) FROM pb_cases c "
        "LEFT JOIN pb_case_history h ON h.case_token = c.case_token AND h.machine = 'letter' "
        f"WHERE c.letter_state IN ({placeholders}) GROUP BY c.case_token",
        _AUTOMATED,
    ):
        if token in (pending_lookup if state == "SUBMITTING" else paused):
            continue
        since = _time(latest if latest is not None else authorized)
        if now - since > AUTOMATED_STEP_OVERDUE_AFTER:
            found.setdefault(token, StuckCase(token, StuckWhy.AUTOMATED_STEP_OVERDUE, since))
    return tuple(sorted(found.values(), key=lambda item: (item.since, item.case_token)))


def _captures(db: sqlite3.Connection, now: datetime) -> CaptureObservation:
    attempts = failures = 0
    for name, occurred in db.execute(
        "SELECT event_name, occurred_at FROM pb_case_history WHERE machine = 'payment' "
        "AND event_name IN ('CaptureSucceeded', 'CaptureFailed')"
    ):
        if now - _time(occurred) <= CAPTURE_WINDOW:
            attempts += 1
            failures += name == "CaptureFailed"
    return CaptureObservation(attempts, failures)


def _webhook_backlog(db: sqlite3.Connection) -> datetime | None:
    (oldest,) = db.execute("SELECT MIN(received_at) FROM pb_webhook_backlog").fetchone()
    return None if oldest is None else _time(oldest)


def _last_backup(backups: Path) -> tuple[datetime | None, bool]:
    """(newest completed backup time, no completed backup exists)."""
    if not backups.exists():
        return None, True
    newest: datetime | None = None
    for bundle in backups.iterdir():
        match = _BUNDLE_TIME.search(bundle.name)
        if (
            match is None
            or not bundle.is_dir()
            or not (bundle / "COMPLETE").is_file()
            or (bundle / "INCOMPLETE").exists()
        ):
            continue
        at = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%z")
        newest = at if newest is None or at > newest else newest
    return newest, newest is None


def _last_off_host_replica(backups: Path, now: datetime) -> datetime | None:
    # Recovery imports the store, which itself uses the monitor incident types.
    from parkingbreaker.recovery import OFF_HOST_RECEIPT, SNAPSHOT_TIME

    newest: datetime | None = None
    for bundle in backups.iterdir():
        if (
            bundle.is_symlink()
            or not (bundle / "COMPLETE").is_file()
            or (bundle / "INCOMPLETE").exists()
            or not (bundle / OFF_HOST_RECEIPT).is_file()
        ):
            continue
        with suppress(OSError, ValueError):
            receipt = json.loads((bundle / OFF_HOST_RECEIPT).read_text())
            if not isinstance(receipt, dict):
                continue
            snapshot_at = (bundle / SNAPSHOT_TIME).read_text()
            if receipt.get("snapshot_at") != snapshot_at:
                continue
            at = _time(snapshot_at)
            if at > now or (newest is not None and at <= newest):
                continue
            digest = hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest()
            if receipt.get("manifest_sha256") == digest:
                newest = at
    return newest


# The bounds apply before joins or per-row investigation. Saturation reports an
# unknown observation, never zero, which also avoids false no-success streaks.
_MONITOR_LIMIT = 1000
_COVERAGE_WINDOW = timedelta(days=7)
_WEBHOOK_DESYNC_SQL = (
    "SELECT 1 FROM pb_webhook_unmatched INDEXED BY pb_webhook_unmatched_reason "
    "WHERE reason IN (?, ?) LIMIT 1"
)
_WRITE_OFF_HISTORY_SQL = (
    "SELECT 1 FROM pb_case_history INDEXED BY pb_history_monitor_exhaustion "
    "WHERE machine='payment' AND event_name='ReconciliationExhausted' LIMIT 1"
)
_WRITE_OFF_EFFECT_SQL = (
    "SELECT 1 FROM pb_pending_effects INDEXED BY pb_effects_monitor_writeoff "
    "WHERE machine='payment' AND effect='LOG_WRITE_OFF' "
    "AND completed_at IS NOT NULL LIMIT 1"
)
_TRANSIENT_SQL = """
    SELECT coalesce((
        SELECT h.occurred_at FROM pb_case_history h INDEXED BY pb_history_monitor_letter_entry
        WHERE h.case_token=c.case_token AND h.machine='letter' AND h.source<>h.target
        ORDER BY h.revision DESC LIMIT 1
    ), c.authorized_at),
    CASE WHEN c.letter_state='SUBMITTING' THEN (
        SELECT json_extract(s.outcome_json, '$.letter_id')
        FROM pb_submission_cursors s WHERE s.case_token=c.case_token AND s.attempt=c.attempt
    ) END
    FROM pb_cases c INDEXED BY pb_cases_monitor_state
    WHERE c.letter_state=? LIMIT ?
"""
_COVERAGE_SQL = """
    WITH recent AS MATERIALIZED (
        SELECT effect_id, request_token, created_at, effect
        FROM pb_pending_effects INDEXED BY pb_effects_monitor_counter_time
        WHERE machine='counter' AND completed_at IS NOT NULL AND effect=?
        AND julianday(created_at) BETWEEN julianday(?) AND julianday(?) LIMIT ?
    )
    SELECT coverage.payload_json, EXISTS (
        SELECT 1 FROM pb_cases c INDEXED BY pb_cases_monitor_paid_workspace
        WHERE c.workspace_token=recent.request_token AND c.amount_cents>0
        AND julianday(c.authorized_at)<=julianday(recent.created_at) LIMIT 1
    ),
    recent.effect='OTHER_EXIT' AND EXISTS (
        SELECT 1 FROM pb_pending_effects e
        JOIN pb_coverage_counters earlier ON earlier.event_token=e.effect_id
        WHERE e.request_token=recent.request_token AND e.effect='REQUIRED_FACTS_UNVERIFIED'
        AND e.completed_at IS NOT NULL
        AND julianday(e.created_at) BETWEEN julianday(?) AND julianday(?)
        AND json_extract(earlier.payload_json, '$.issuer_token')
            =json_extract(coverage.payload_json, '$.issuer_token')
    )
    FROM recent LEFT JOIN pb_coverage_counters coverage ON coverage.event_token=recent.effect_id
"""


def _transient_count(
    db: sqlite3.Connection, now: datetime, state: CaseState, ttl: timedelta
) -> int | None:
    rows = db.execute(_TRANSIENT_SQL, (state.value, _MONITOR_LIMIT + 1)).fetchall()
    if len(rows) > _MONITOR_LIMIT:
        return None
    return sum(anchor is None and now - _time(since) > ttl for since, anchor in rows)


def _issuer_stop_count(db: sqlite3.Connection, now: datetime) -> int | None:
    stops: Counter[str] = Counter()
    serviceable: set[str] = set()
    start, end = (now - _COVERAGE_WINDOW).isoformat(), now.isoformat()
    for milestone in (
        CounterMilestone.REQUIRED_FACTS_UNVERIFIED,
        CounterMilestone.OTHER_EXIT,
        CounterMilestone.TIER1_PASSED,
        CounterMilestone.APPEAL_COMPLETED,
    ):
        rows = db.execute(
            _COVERAGE_SQL,
            (milestone.value, start, end, _MONITOR_LIMIT + 1, start, end),
        ).fetchall()
        if len(rows) > _MONITOR_LIMIT:
            return None
        for payload, paid, duplicate_stop in rows:
            if payload is None:
                return None
            key = decode_counter_key(payload)
            if key.milestone is not milestone:
                return None
            if key.issuer_token == "unknown":
                continue
            if key.outcome == "SERVICEABLE":
                serviceable.add(key.issuer_token)
            elif paid and not duplicate_stop and key.outcome is StopFamily.NOT_YET_VERIFIED:
                stops[key.issuer_token] += 1
    return sum(count >= 3 and issuer not in serviceable for issuer, count in stops.items())


def _operations(db: sqlite3.Connection, now: datetime) -> OperationalObservation:
    def observe_count(read: Callable[[], int | None]) -> int | None:
        with suppress(sqlite3.Error, ValueError, TypeError):
            return read()
        return None

    return OperationalObservation(
        webhook_desync=observe_count(
            lambda: int(
                db.execute(
                    _WEBHOOK_DESYNC_SQL, (CONTRADICTS_CASE_STATE, CONFLICTING_LETTER)
                ).fetchone()
                is not None
            )
        ),
        capture_written_off=observe_count(
            lambda: int(
                db.execute(_WRITE_OFF_HISTORY_SQL).fetchone() is not None
                or db.execute(_WRITE_OFF_EFFECT_SQL).fetchone() is not None
            )
        ),
        research_stalled=observe_count(
            lambda: _transient_count(db, now, CaseState.RESEARCHING, timedelta(hours=1))
        ),
        submission_unanchored=observe_count(
            lambda: _transient_count(db, now, CaseState.SUBMITTING, timedelta(hours=24))
        ),
        issuer_stop_streak=observe_count(lambda: _issuer_stop_count(db, now)),
    )


def observe(
    data: Path, backups: Path, now: datetime, *, require_off_host: bool = False
) -> HealthSnapshot:
    """Gather each observation independently; a failure leaves only that one unknown."""
    database = data / DATABASE
    sweep = stuck = captures = None
    holds: tuple[HoldObservation, ...] | None = None
    backlog: datetime | None = None
    backlog_known = False
    operations = OperationalObservation(None, None, None, None, None)
    try:
        with _read_only(database) as db:
            with suppress(sqlite3.Error, ValueError, TypeError):
                sweep = _sweep(db, now)
            with suppress(sqlite3.Error, OSError, ValueError, TypeError):
                stuck = _stuck(db, now, database)
            with suppress(sqlite3.Error, ValueError, TypeError):
                captures = _captures(db, now)
            with suppress(sqlite3.Error, ValueError, TypeError):
                holds = _holds(db)
            with suppress(sqlite3.Error, ValueError, TypeError):
                backlog, backlog_known = _webhook_backlog(db), True
            operations = _operations(db, now)
    except (OSError, sqlite3.Error):
        pass
    disk = None
    try:
        usage = shutil.disk_usage(data)
        disk = DiskObservation(usage.free, usage.total)
    except OSError:
        pass
    last_backup: datetime | None = None
    no_backup = False
    with suppress(OSError, ValueError):
        last_backup, no_backup = _last_backup(backups)
    last_replica = None
    if require_off_host:
        with suppress(OSError):
            last_replica = _last_off_host_replica(backups, now)
    return HealthSnapshot(
        sweep,
        stuck,
        captures,
        disk,
        last_backup,
        no_backup,
        backlog,
        backlog_known,
        holds,
        require_off_host_replica=require_off_host,
        last_off_host_replica_at=last_replica,
        operations=operations,
    )


_INCIDENTS_DDL = """CREATE TABLE IF NOT EXISTS pb_incidents (
    incident_id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    domain TEXT NOT NULL,
    severity TEXT NOT NULL,
    subject TEXT NOT NULL,
    detail TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    notices INTEGER NOT NULL DEFAULT 0,
    last_notice_at TEXT,
    resolved_at TEXT,
    recovery_queued INTEGER NOT NULL DEFAULT 0
)"""

# Each notice is stored, with its exact recipient, text and key, before it is sent, and
# re-sent byte for byte until the provider accepts it.
_NOTICES_DDL = """CREATE TABLE IF NOT EXISTS pb_notices (
    idempotency_key TEXT PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES pb_incidents(incident_id),
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    accepted_at TEXT
)"""

# Which observation each alert depends on: an incident clears only when its observation
# was actually made and the alert is gone, never because the observation went unknown.
_DOMAIN = {
    AlertKind.SCHEDULER_STALE: "sweep",
    AlertKind.SWEEP_FAILING: "sweep",
    AlertKind.SWEEP_INTERRUPTED: "sweep",
    AlertKind.CASE_STUCK: "cases",
    AlertKind.CAPTURE_FAILURES: "captures",
    AlertKind.DISK_LOW: "disk",
    AlertKind.BACKUP_STALE: "backup",
    AlertKind.OFF_HOST_REPLICA_STALE: "off_host_replica",
    AlertKind.WEBHOOK_BACKLOG: "webhooks",
    AlertKind.HOLD_REVIEW_DUE: "holds",
    AlertKind.WEBHOOK_DESYNC: "webhooks",
    AlertKind.CAPTURE_WRITTEN_OFF: "captures",
    AlertKind.RESEARCH_STALLED: "cases",
    AlertKind.SUBMISSION_UNANCHORED: "cases",
    AlertKind.ISSUER_STOP_STREAK: "coverage",
}


def _key(alert: Alert) -> str:
    return hashlib.sha256(f"{alert.kind.value}\x00{alert.subject}".encode()).hexdigest()[:32]


def _domain(alert: Alert) -> str:
    return alert.subject if alert.kind is AlertKind.OBSERVATION_UNKNOWN else _DOMAIN[alert.kind]


class IncidentLog:
    """The monitor's own incident state: one open incident per alert kind and subject."""

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path, timeout=5, isolation_level=None)
        self._db.execute(_INCIDENTS_DDL)
        self._db.execute(_NOTICES_DDL)
        self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS pb_incidents_one_open "
            "ON pb_incidents(incident_key) WHERE resolved_at IS NULL"
        )

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _atomic(self) -> Iterator[None]:
        """A notice and the incident change that queued it commit together."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def _queue(
        self, key: str, incident_id: int, to: str, subject: str, body: str, stamp: str
    ) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO pb_notices VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (key, incident_id, to, subject, body, stamp),
        )

    def _pending(self, incident_id: int) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM pb_notices WHERE incident_id=? AND accepted_at IS NULL",
                (incident_id,),
            ).fetchone()
            is not None
        )

    def deliver(self, alerts: tuple[Alert, ...], now: datetime, notify: Notifier, to: str) -> None:
        # TODO(T05): In the monitor DB, atomically enqueue one diagnosis per incident opening.
        # Record each repair's expected result and verify it against fresh authoritative receipts.
        # A quiet poll can clear an alert while a charge is still unresolved. Track both outcomes.
        current = {_key(alert): alert for alert in alerts}
        unknown = {a.subject for a in alerts if a.kind is AlertKind.OBSERVATION_UNKNOWN}
        stamp = now.isoformat()
        for key, alert in current.items():
            updated = self._db.execute(
                "UPDATE pb_incidents SET last_seen_at=?, severity=?, detail=? "
                "WHERE incident_key=? AND resolved_at IS NULL",
                (stamp, alert.severity.value, alert.detail, key),
            ).rowcount
            if not updated:
                self._db.execute(
                    "INSERT INTO pb_incidents (incident_key, kind, domain, severity, subject, "
                    "detail, opened_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        alert.kind.value,
                        _domain(alert),
                        alert.severity.value,
                        alert.subject,
                        alert.detail,
                        stamp,
                        stamp,
                    ),
                )
        rows = self._db.execute(
            "SELECT incident_id, incident_key, kind, domain, severity, subject, detail, "
            "notices, last_notice_at FROM pb_incidents WHERE resolved_at IS NULL"
        ).fetchall()
        for incident_id, key, kind, domain, severity, subject, detail, sent, last in rows:
            if key not in current:
                if kind != AlertKind.OBSERVATION_UNKNOWN.value and domain in unknown:
                    continue
                self._db.execute(
                    "UPDATE pb_incidents SET resolved_at=? WHERE incident_id=?",
                    (stamp, incident_id),
                )
                continue
            if self._pending(incident_id) or (
                sent and last is not None and now - _time(last) < ESCALATE_EVERY
            ):
                continue
            notice = sent + 1
            with self._atomic():
                self._queue(
                    f"pb-incident-{incident_id}-{notice}",
                    incident_id,
                    to,
                    f"ParkingBreaker {severity}: {kind}"
                    + ("" if notice == 1 else f" (still open, notice {notice})"),
                    f"{kind} ({severity})\nsubject: {subject}\n{detail}\nincident "
                    f"{incident_id}, notice {notice}.",
                    stamp,
                )
                self._db.execute(
                    "UPDATE pb_incidents SET notices=?, last_notice_at=? WHERE incident_id=?",
                    (notice, stamp, incident_id),
                )
        # A recovery notice follows only once every earlier notice has settled.
        for incident_id, kind, subject in self._db.execute(
            "SELECT incident_id, kind, subject FROM pb_incidents "
            "WHERE resolved_at IS NOT NULL AND recovery_queued = 0 AND notices > 0"
        ).fetchall():
            if self._pending(incident_id):
                continue
            with self._atomic():
                self._queue(
                    f"pb-incident-{incident_id}-recovered",
                    incident_id,
                    to,
                    f"ParkingBreaker recovered: {kind}",
                    f"{kind} cleared\nsubject: {subject}\nincident {incident_id} is resolved.",
                    stamp,
                )
                self._db.execute(
                    "UPDATE pb_incidents SET recovery_queued=1 WHERE incident_id=?", (incident_id,)
                )
        for key, recipient, subject, body in self._db.execute(
            "SELECT idempotency_key, recipient, subject, body FROM pb_notices "
            "WHERE accepted_at IS NULL ORDER BY created_at, idempotency_key"
        ).fetchall():
            if notify(EmailMessage(recipient, subject, body), key).status == "accepted":
                self._db.execute(
                    "UPDATE pb_notices SET accepted_at=? WHERE idempotency_key=?", (stamp, key)
                )
