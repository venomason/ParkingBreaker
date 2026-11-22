"""Pure health rules: an observed snapshot and an explicit time in, typed alerts out (T24).

No clock, file, environment or network access here; ``health_service`` gathers the
snapshot. Every observation may be missing (``None``): a missing or unreadable
observation is reported as unknown, never as healthy. Alerts carry fixed text and opaque
subjects only, never customer data or provider error text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

# The scheduler calls the sweep every minute; three missed minutes is stale.
SCHEDULER_STALE_AFTER = timedelta(minutes=3)
# No successful (ok) sweep for this long while runs keep failing.
SWEEP_FAILING_AFTER = timedelta(minutes=10)
# A run still unfinished after this long died or hung.
SWEEP_INTERRUPTED_AFTER = timedelta(minutes=15)
# The recorded 2.x capture rule kept for one-VM monitoring: 60-minute window, 5 percent.
CAPTURE_WINDOW = timedelta(minutes=60)
CAPTURE_FAILURE_RATE = 0.05
# Below this many capture attempts in the window, report the rate with low-volume context.
CAPTURE_LOW_VOLUME = 20
DISK_MIN_FREE_FRACTION = 0.10
DISK_MIN_FREE_BYTES = 2 * 1024**3
BACKUP_STALE_AFTER = timedelta(hours=26)
OFF_HOST_STALE_AFTER = timedelta(minutes=60)
WEBHOOK_BACKLOG_AFTER = timedelta(minutes=30)


class AlertKind(StrEnum):
    OBSERVATION_UNKNOWN = "observation_unknown"
    SCHEDULER_STALE = "scheduler_stale"
    SWEEP_FAILING = "sweep_failing"
    SWEEP_INTERRUPTED = "sweep_interrupted"
    CASE_STUCK = "case_stuck"
    CAPTURE_FAILURES = "capture_failures"
    DISK_LOW = "disk_low"
    BACKUP_STALE = "backup_stale"
    OFF_HOST_REPLICA_STALE = "off_host_replica_stale"
    WEBHOOK_BACKLOG = "webhook_backlog"
    HOLD_REVIEW_DUE = "hold_review_due"


class Severity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Alert:
    kind: AlertKind
    severity: Severity
    # A fixed observation name or an opaque case token; never customer data.
    subject: str
    detail: str


@dataclass(frozen=True)
class SweepObservation:
    last_started_at: datetime | None
    last_ok_finished_at: datetime | None
    last_outcome: str | None
    unfinished_started_at: tuple[datetime, ...]


class StuckWhy(StrEnum):
    EFFECT_OVERDUE = "effect_overdue"
    SUBMISSION_OVERDUE = "submission_overdue"
    AUTOMATED_STEP_OVERDUE = "automated_step_overdue"


@dataclass(frozen=True)
class StuckCase:
    case_token: str
    why: StuckWhy
    since: datetime


@dataclass(frozen=True)
class CaptureObservation:
    attempts: int
    failures: int


@dataclass(frozen=True)
class DiskObservation:
    free_bytes: int
    total_bytes: int


@dataclass(frozen=True)
class HoldObservation:
    case_token: str
    review_due_at: datetime


@dataclass(frozen=True)
class HealthSnapshot:
    sweep: SweepObservation | None
    stuck: tuple[StuckCase, ...] | None
    captures: CaptureObservation | None
    disk: DiskObservation | None
    # The newest completed (COMPLETE marker, no INCOMPLETE) backup's recorded time;
    # ``None`` means unknown. ``no_backup`` is an observed absence.
    last_backup_at: datetime | None
    no_backup: bool
    oldest_webhook_backlog_at: datetime | None
    webhook_backlog_known: bool
    # Cases under a T74 incident hold; ``None`` means unknown.
    holds: tuple[HoldObservation, ...] | None = ()
    require_off_host_replica: bool = False
    last_off_host_replica_at: datetime | None = None


def _unknown(name: str) -> Alert:
    return Alert(
        AlertKind.OBSERVATION_UNKNOWN,
        Severity.WARNING,
        name,
        f"{name} could not be observed; treat as unhealthy until it can",
    )


def evaluate(snapshot: HealthSnapshot, now: datetime) -> tuple[Alert, ...]:
    alerts: list[Alert] = []
    sweep = snapshot.sweep
    if sweep is None:
        alerts.append(_unknown("sweep"))
    else:
        if sweep.last_started_at is None or now - sweep.last_started_at > SCHEDULER_STALE_AFTER:
            alerts.append(
                Alert(
                    AlertKind.SCHEDULER_STALE,
                    Severity.CRITICAL,
                    "scheduler",
                    "no sweep has started in the last 3 minutes",
                )
            )
        if (
            sweep.last_ok_finished_at is None
            or now - sweep.last_ok_finished_at > SWEEP_FAILING_AFTER
        ):
            alerts.append(
                Alert(
                    AlertKind.SWEEP_FAILING,
                    Severity.CRITICAL,
                    "sweep",
                    "no fully successful sweep in the last 10 minutes",
                )
            )
        if any(now - started > SWEEP_INTERRUPTED_AFTER for started in sweep.unfinished_started_at):
            alerts.append(
                Alert(
                    AlertKind.SWEEP_INTERRUPTED,
                    Severity.WARNING,
                    "sweep",
                    "a sweep started over 15 minutes ago never finished",
                )
            )
    if snapshot.stuck is None:
        alerts.append(_unknown("cases"))
    else:
        for item in snapshot.stuck:
            alerts.append(
                Alert(
                    AlertKind.CASE_STUCK,
                    Severity.CRITICAL,
                    item.case_token,
                    f"{item.why.value} since {item.since.isoformat()}",
                )
            )
    captures = snapshot.captures
    if captures is None:
        alerts.append(_unknown("captures"))
    elif captures.failures and captures.failures >= CAPTURE_FAILURE_RATE * captures.attempts:
        low = captures.attempts < CAPTURE_LOW_VOLUME
        alerts.append(
            Alert(
                AlertKind.CAPTURE_FAILURES,
                Severity.WARNING if low else Severity.CRITICAL,
                "captures",
                f"{captures.failures} of {captures.attempts} captures failed in 60 minutes"
                + (" (low volume)" if low else ""),
            )
        )
    disk = snapshot.disk
    if disk is None or disk.total_bytes <= 0:
        alerts.append(_unknown("disk"))
    elif (
        disk.free_bytes < DISK_MIN_FREE_BYTES
        or disk.free_bytes < DISK_MIN_FREE_FRACTION * disk.total_bytes
    ):
        alerts.append(
            Alert(AlertKind.DISK_LOW, Severity.CRITICAL, "disk", "data disk is nearly full")
        )
    if snapshot.no_backup:
        alerts.append(
            Alert(AlertKind.BACKUP_STALE, Severity.CRITICAL, "backup", "no completed backup")
        )
    elif snapshot.last_backup_at is None:
        alerts.append(_unknown("backup"))
    elif now - snapshot.last_backup_at > BACKUP_STALE_AFTER:
        alerts.append(
            Alert(
                AlertKind.BACKUP_STALE,
                Severity.CRITICAL,
                "backup",
                "the newest completed backup is over 26 hours old",
            )
        )
    if snapshot.require_off_host_replica:
        replica_at = snapshot.last_off_host_replica_at
        if replica_at is None or not timedelta(0) <= now - replica_at <= OFF_HOST_STALE_AFTER:
            alerts.append(
                Alert(
                    AlertKind.OFF_HOST_REPLICA_STALE,
                    Severity.CRITICAL,
                    "off_host_replica",
                    "no verified off-host replica with a source snapshot in the past 60 minutes",
                )
            )
    if not snapshot.webhook_backlog_known:
        alerts.append(_unknown("webhooks"))
    elif (
        snapshot.oldest_webhook_backlog_at is not None
        and now - snapshot.oldest_webhook_backlog_at > WEBHOOK_BACKLOG_AFTER
    ):
        alerts.append(
            Alert(
                AlertKind.WEBHOOK_BACKLOG,
                Severity.WARNING,
                "webhooks",
                "a provider event has waited over 30 minutes to be matched",
            )
        )
    if snapshot.holds is None:
        alerts.append(_unknown("holds"))
    else:
        for hold in snapshot.holds:
            if now >= hold.review_due_at:
                alerts.append(
                    Alert(
                        AlertKind.HOLD_REVIEW_DUE,
                        Severity.CRITICAL,
                        hold.case_token,
                        f"a held case is due for review (due {hold.review_due_at.isoformat()})",
                    )
                )
    return tuple(alerts)
