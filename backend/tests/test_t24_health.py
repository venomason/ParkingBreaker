"""T24: recorded sweeps, pure health rules, read-only observation, incidents, scheduler."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.api.app import create_app
from parkingbreaker.api.service import SweepFailure, SweepResult
from parkingbreaker.case_service import CaseService
from parkingbreaker.config import ProviderMode, Settings, build_services
from parkingbreaker.email_client import EmailMessage, EmailOutcome, EmailReason
from parkingbreaker.health import (
    Alert,
    AlertKind,
    CaptureObservation,
    DiskObservation,
    HealthSnapshot,
    Severity,
    StuckCase,
    StuckWhy,
    SweepObservation,
    evaluate,
)
from parkingbreaker.health_cli import main as health_main
from parkingbreaker.health_service import IncidentLog, _stuck, observe
from parkingbreaker.payment_records import PaymentCursor, PaymentOutcome, cursor_json
from test_config import SECRET, WORKER, held_case
from test_draft_provider_refusal import get_to_drafting

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
ACCEPTED = "0b6a7a3e-1c1f-4c3e-9a55-3d1f0e2b9c11"

HEALTHY = HealthSnapshot(
    sweep=SweepObservation(NOW - timedelta(seconds=30), NOW - timedelta(seconds=20), "ok", ()),
    stuck=(),
    captures=CaptureObservation(40, 1),
    disk=DiskObservation(50 * 1024**3, 100 * 1024**3),
    last_backup_at=NOW - timedelta(hours=2),
    no_backup=False,
    oldest_webhook_backlog_at=None,
    webhook_backlog_known=True,
)


def kinds(snapshot: HealthSnapshot) -> set[AlertKind]:
    return {alert.kind for alert in evaluate(snapshot, NOW)}


def test_healthy_snapshot_is_quiet_and_unknown_observations_are_never_healthy() -> None:
    assert evaluate(HEALTHY, NOW) == ()
    unknown = HealthSnapshot(None, None, None, None, None, False, None, False)
    alerts = evaluate(unknown, NOW)
    assert {a.kind for a in alerts} == {AlertKind.OBSERVATION_UNKNOWN}
    assert {a.subject for a in alerts} == {
        "sweep",
        "cases",
        "captures",
        "disk",
        "backup",
        "webhooks",
    }


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        (
            {"sweep": SweepObservation(NOW - timedelta(minutes=4), NOW, "ok", ())},
            AlertKind.SCHEDULER_STALE,
        ),
        (
            {"sweep": SweepObservation(NOW, NOW - timedelta(minutes=11), "partial", ())},
            AlertKind.SWEEP_FAILING,
        ),
        (
            {"sweep": SweepObservation(NOW, None, "failed", ())},
            AlertKind.SWEEP_FAILING,
        ),
        (
            {"sweep": SweepObservation(NOW, NOW, "ok", (NOW - timedelta(minutes=16),))},
            AlertKind.SWEEP_INTERRUPTED,
        ),
        (
            {"stuck": (StuckCase("case_x", StuckWhy.EFFECT_OVERDUE, NOW - timedelta(hours=1)),)},
            AlertKind.CASE_STUCK,
        ),
        ({"disk": DiskObservation(1024**3, 8 * 1024**3)}, AlertKind.DISK_LOW),
        ({"disk": DiskObservation(5 * 1024**3, 100 * 1024**3)}, AlertKind.DISK_LOW),
        ({"last_backup_at": NOW - timedelta(hours=27)}, AlertKind.BACKUP_STALE),
        ({"last_backup_at": None, "no_backup": True}, AlertKind.BACKUP_STALE),
        (
            {"oldest_webhook_backlog_at": NOW - timedelta(minutes=31)},
            AlertKind.WEBHOOK_BACKLOG,
        ),
    ],
)
def test_each_rule_fires_past_its_threshold(
    changed: dict[str, object], expected: AlertKind
) -> None:
    assert kinds(replace(HEALTHY, **changed)) == {expected}  # type: ignore[arg-type]


def test_capture_rule_is_sixty_minutes_five_percent_with_low_volume_context() -> None:
    assert kinds(replace(HEALTHY, captures=CaptureObservation(100, 4))) == set()
    (alert,) = evaluate(replace(HEALTHY, captures=CaptureObservation(100, 5)), NOW)
    assert (alert.kind, alert.severity) == (AlertKind.CAPTURE_FAILURES, Severity.CRITICAL)
    (low,) = evaluate(replace(HEALTHY, captures=CaptureObservation(3, 1)), NOW)
    assert low.severity is Severity.WARNING
    assert low.detail.endswith("(low volume)")
    assert kinds(replace(HEALTHY, captures=CaptureObservation(0, 0))) == set()


@pytest.fixture
async def worker(tmp_path: Path) -> AsyncIterator[tuple[CaseService, AsyncClient]]:
    service = build_services(Settings(ProviderMode.FAKE, tmp_path, SECRET, {}))
    assert isinstance(service, CaseService)
    app = create_app(service, worker_secret=SECRET, sweep_runs=service.sweep_runs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fake.test") as client:
        yield service, client
    service._store.close()


def runs(tmp_path: Path) -> list[tuple[str | None, int | None, int | None]]:
    with sqlite3.connect(tmp_path / "parkingbreaker.sqlite") as database:
        return [
            (o, p, f)
            for o, p, f in database.execute(
                "SELECT outcome, processed, failed FROM pb_sweep_runs ORDER BY run_id"
            )
        ]


@pytest.mark.anyio
async def test_every_sweep_is_recorded_and_only_ok_counts_as_success(
    worker: tuple[CaseService, AsyncClient], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, client = worker
    assert (await client.post("/internal/dispatch", headers=WORKER)).status_code == 200
    monkeypatch.setattr(service, "advance_due", lambda: SweepResult(2, (SweepFailure("c", "x"),)))
    assert (await client.post("/internal/dispatch", headers=WORKER)).json() == {
        "processed": 2,
        "failed": 1,
    }

    def boom() -> SweepResult:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(service, "advance_due", boom)
    with pytest.raises(sqlite3.OperationalError):
        await client.post("/internal/dispatch", headers=WORKER)
    assert runs(tmp_path) == [("ok", 0, 0), ("partial", 2, 1), ("failed", 0, 0)]
    snapshot = observe(tmp_path, tmp_path / "recovery", datetime.now(UTC))
    assert snapshot.sweep is not None
    assert snapshot.sweep.last_outcome == "failed"
    assert snapshot.sweep.last_ok_finished_at is not None
    assert snapshot.sweep.unfinished_started_at == ()


@pytest.mark.anyio
async def test_observation_is_read_only_and_finds_stuck_cases_and_completed_backups(
    worker: tuple[CaseService, AsyncClient], tmp_path: Path
) -> None:
    service, client = worker
    case = await held_case(client)
    await get_to_drafting(client, case)
    record = service._store.get_intake(case)
    assert record is not None
    assert record.case_token is not None
    backups = tmp_path / "recovery"
    for name, markers in (
        ("scheduled-20261103T090000Z-11", ("COMPLETE",)),
        ("scheduled-20261103T110000Z-12", ("INCOMPLETE",)),
        ("pre-deploy-20261103T100000Z", ("COMPLETE",)),
    ):
        (backups / name).mkdir(parents=True)
        for marker in markers:
            (backups / name / marker).write_text("x")
    with sqlite3.connect(tmp_path / "parkingbreaker.sqlite") as database:
        before = list(database.iterdump())
    later = datetime.now(UTC) + timedelta(hours=1)
    snapshot = observe(tmp_path, backups, later)
    with sqlite3.connect(tmp_path / "parkingbreaker.sqlite") as database:
        assert list(database.iterdump()) == before
    assert snapshot.stuck is not None
    assert [(s.case_token, s.why) for s in snapshot.stuck] == [
        (record.case_token, StuckWhy.AUTOMATED_STEP_OVERDUE)
    ]
    assert snapshot.last_backup_at == datetime(2026, 11, 3, 10, tzinfo=UTC)
    assert snapshot.no_backup is False
    assert observe(tmp_path / "missing", backups, later).sweep is None


def test_payment_backoff_is_not_stuck_but_an_overdue_cursor_or_orphan_effect_is() -> None:
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE pb_payment_cursor (case_token TEXT PRIMARY KEY, payload TEXT);"
        "CREATE TABLE pb_submission_cursors (case_token TEXT, attempt INTEGER, "
        "next_at TEXT, outcome_json TEXT);"
        "CREATE TABLE pb_pending_effects (effect_id TEXT, case_token TEXT, machine TEXT,"
        " effect TEXT, created_at TEXT, outcome TEXT);"
        "CREATE TABLE pb_cases (case_token TEXT, authorized_at TEXT, letter_state TEXT, "
        "attempt INTEGER);"
        "CREATE TABLE pb_case_history (case_token TEXT, machine TEXT, occurred_at TEXT);"
        "CREATE TABLE pb_case_holds (case_token TEXT, held INTEGER);"
    )
    old = (NOW - timedelta(hours=2)).isoformat()

    def cursor(token: str, next_at: datetime) -> str:
        return cursor_json(
            PaymentCursor(
                token,
                f"eff_{token}",
                "capture",
                "pi_x",
                2999,
                "key",
                NOW - timedelta(hours=2),
                1,
                next_at,
                1,
                1,
                None,
                "capture",
                PaymentOutcome("uncertain", "pi_x", 2999),
                False,
            )
        )

    for token, next_at in (
        ("backoff", NOW + timedelta(hours=1)),
        ("late", NOW - timedelta(hours=1)),
    ):
        db.execute("INSERT INTO pb_payment_cursor VALUES (?, ?)", (token, cursor(token, next_at)))
        db.execute(
            "INSERT INTO pb_pending_effects VALUES (?, ?, 'payment', 'CAPTURE_PAYMENT', ?, NULL)",
            (f"eff_{token}", token, old),
        )
    db.execute(
        "INSERT INTO pb_pending_effects VALUES ('e3', 'orphan', 'letter', 'RELEASE_HOLD', ?, NULL)",
        (old,),
    )
    done = cursor("captured", NOW - timedelta(hours=1)).replace('"done":false', '"done":true')
    assert '"done":true' in done
    db.execute("INSERT INTO pb_payment_cursor VALUES ('captured', ?)", (done,))
    found = {item.case_token: item.why for item in _stuck(db, NOW)}
    assert found == {"late": StuckWhy.EFFECT_OVERDUE, "orphan": StuckWhy.EFFECT_OVERDUE}


class Mailbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.accept = True

    def __call__(self, message: EmailMessage, key: str) -> EmailOutcome:
        self.sent.append((key, message.subject))
        if self.accept:
            return EmailOutcome("accepted", provider_id=ACCEPTED)
        return EmailOutcome("uncertain", reason=EmailReason.TRANSPORT_ERROR)


def test_incidents_notify_once_retry_same_key_escalate_and_send_one_recovery(
    tmp_path: Path,
) -> None:
    log = IncidentLog(tmp_path / "incidents.sqlite")
    mail = Mailbox()
    alert = Alert(AlertKind.DISK_LOW, Severity.CRITICAL, "disk", "data disk is nearly full")
    try:
        mail.accept = False
        log.deliver((alert,), NOW, mail, "owner@example.test")
        mail.accept = True
        log.deliver((alert,), NOW + timedelta(minutes=2), mail, "owner@example.test")
        log.deliver((alert,), NOW + timedelta(minutes=4), mail, "owner@example.test")
        assert [key for key, _ in mail.sent] == ["pb-incident-1-1", "pb-incident-1-1"]
        log.deliver((alert,), NOW + timedelta(hours=7), mail, "owner@example.test")
        assert mail.sent[-1][0] == "pb-incident-1-2"
        assert "notice 2" in mail.sent[-1][1]
        log.deliver((), NOW + timedelta(hours=8), mail, "owner@example.test")
        log.deliver((), NOW + timedelta(hours=9), mail, "owner@example.test")
        assert [key for key, _ in mail.sent][-1] == "pb-incident-1-recovered"
        assert len(mail.sent) == 4
        log.deliver((alert,), NOW + timedelta(hours=10), mail, "owner@example.test")
        assert mail.sent[-1][0] == "pb-incident-2-1"
        # An unknown disk reading never clears the open disk incident.
        blind = Alert(AlertKind.OBSERVATION_UNKNOWN, Severity.WARNING, "disk", "unknown")
        log.deliver((blind,), NOW + timedelta(hours=11), mail, "owner@example.test")
        assert not any(key.endswith("-recovered") for key, _ in mail.sent[4:])
        # A pending notice is retried byte for byte, even after the alert's detail changed
        # or it cleared, and its recovery notice waits until it settles.
        mail.accept = False
        backup = Alert(AlertKind.BACKUP_STALE, Severity.CRITICAL, "backup", "first detail")
        log.deliver((blind, backup), NOW + timedelta(hours=12), mail, "owner@example.test")
        later = Alert(AlertKind.BACKUP_STALE, Severity.WARNING, "backup", "second detail")
        log.deliver((blind, later), NOW + timedelta(hours=13), mail, "owner@example.test")
        log.deliver((blind,), NOW + timedelta(hours=14), mail, "owner@example.test")
        backup_sends = [subject for key, subject in mail.sent if "-4-" in key]
        assert len(backup_sends) == 3
        assert len(set(backup_sends)) == 1
        assert "critical" in backup_sends[0]
        mail.accept = True
        log.deliver((blind,), NOW + timedelta(hours=15), mail, "owner@example.test")
        log.deliver((blind,), NOW + timedelta(hours=16), mail, "owner@example.test")
        assert [key for key, _ in mail.sent[-2:]] == ["pb-incident-4-1", "pb-incident-4-recovered"]
    finally:
        log.close()
    state = tmp_path / "state"
    state.mkdir()
    assert (
        health_main(["monitor", "--data", str(tmp_path / "none"), "--state", str(state), "--once"])
        == 0
    )
    assert (state / "incidents.sqlite").is_file()


FAKE_CURL = """#!/bin/sh
printf '%s\\n' "$*" >> "$PB_CURL_ARGS"
n=$(wc -l < "$PB_CURL_ARGS")
mode=$(printf '%s' "$PB_FAKE" | cut -d, -f"$n")
case "$mode" in
  ok) printf 200 ;;
  error) printf 500 ;;
  down) printf 000; echo 'curl: (7) Failed to connect' >&2; exit 7 ;;
  hang) printf 000; echo 'curl: (28) Operation timed out' >&2; exit 28 ;;
esac
"""

FAILED = {"event": "sweep_call_failed"}


@pytest.mark.parametrize(
    ("modes", "failures"),
    [
        ("ok", []),
        ("error", [{**FAILED, "curl_exit": 0, "http_status": "500"}]),
        ("down", [{**FAILED, "curl_exit": 7, "http_status": "000"}]),
        (
            "hang,down,ok",
            [
                {**FAILED, "curl_exit": 28, "http_status": "000"},
                {**FAILED, "curl_exit": 7, "http_status": "000"},
            ],
        ),
    ],
)
def test_scheduler_logs_failures_and_hangs_with_bounded_curl_and_keeps_going(
    tmp_path: Path, modes: str, failures: list[dict[str, object]]
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(FAKE_CURL)
    (bin_dir / "curl").chmod(0o755)
    args_file = tmp_path / "args"
    calls = len(modes.split(","))
    result = subprocess.run(
        ["sh", str(REPO / "deploy/scheduler-loop.sh"), "http://worker:8000/internal/dispatch"],
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "PB_WORKER_SECRET": "synthetic-secret",
            "PB_SCHEDULER_CALLS": str(calls),
            "PB_SCHEDULER_INTERVAL": "0",
            "PB_CURL_ARGS": str(args_file),
            "PB_FAKE": modes,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    logged = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert logged == failures
    oks = [json.loads(line) for line in result.stdout.splitlines()]
    assert oks == [{"event": "sweep_call_ok"}] * (calls - len(failures))
    assert "synthetic-secret" not in result.stdout + result.stderr
    lines = args_file.read_text().splitlines()
    assert len(lines) == calls
    assert all("--connect-timeout 5" in line and "--max-time 300" in line for line in lines)
    for compose in ("compose.yaml", "deploy/compose.production.yaml"):
        text = (REPO / compose).read_text()
        assert "|| true" not in text
        assert "scheduler-loop.sh" in text
    production = (REPO / "deploy/compose.production.yaml").read_text()
    assert "parkingbreaker.health_cli" in production
    lifecycle = (REPO / "deploy/lifecycle.sh").read_text()
    # Exact build services are exercised by the lifecycle command-shim tests.
    assert "api worker web caddy monitor" in lifecycle
    assert "exec -T monitor" in lifecycle
    assert "mkdir -p /data /monitor" in (REPO / "backend/Dockerfile").read_text()
