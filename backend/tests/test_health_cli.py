"""The monitor may publish a receipt only after a completed, fully observed poll.

The tests inject the observer and notifier so no database owned by the app, provider or
network is touched; the receipt itself is the real release-readiness receipt.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parkingbreaker import health_cli
from parkingbreaker.email_client import EmailMessage, EmailOutcome, EmailReason
from parkingbreaker.health import (
    CaptureObservation,
    DiskObservation,
    HealthSnapshot,
    SweepObservation,
)
from parkingbreaker.health_service import Notifier
from parkingbreaker.release_readiness import RECEIPT, ready, record_monitor_poll

REJECTED = EmailOutcome("rejected", reason=EmailReason.INVALID_MESSAGE)


def database(data: Path, now: datetime) -> Path:
    path = data / "parkingbreaker.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE pb_sweep_runs ("
            "run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, "
            "finished_at TEXT, outcome TEXT, processed INTEGER, failed INTEGER)"
        )
        connection.execute(
            "INSERT INTO pb_sweep_runs (started_at, finished_at, outcome, processed, failed) "
            "VALUES (?, ?, 'ok', 1, 0)",
            (
                (now - timedelta(minutes=2)).isoformat(),
                (now - timedelta(minutes=1)).isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return path


def complete(now: datetime, *, no_backup: bool = False) -> HealthSnapshot:
    return HealthSnapshot(
        sweep=SweepObservation(now - timedelta(seconds=30), now - timedelta(seconds=20), "ok", ()),
        stuck=(),
        captures=CaptureObservation(40, 0),
        disk=DiskObservation(50 * 1024**3, 100 * 1024**3),
        last_backup_at=None if no_backup else now - timedelta(hours=1),
        no_backup=no_backup,
        oldest_webhook_backlog_at=None,
        webhook_backlog_known=True,
        holds=(),
    )


def blind() -> HealthSnapshot:
    return HealthSnapshot(None, None, None, None, None, False, None, False, None)


def rejected_notifier(environ: dict[str, str]) -> tuple[Notifier, str]:
    def notify(message: EmailMessage, key: str) -> EmailOutcome:
        return REJECTED

    return notify, "owner@example.test"


def setup(tmp_path: Path) -> tuple[Path, Path, datetime]:
    data = tmp_path / "data"
    data.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    now = datetime.now(UTC)
    database(data, now)
    return data, state, now


def run_once(data: Path, state: Path) -> int:
    return health_cli.main(["monitor", "--data", str(data), "--state", str(state), "--once"])


def prior_receipt(data: Path, state: Path, now: datetime) -> None:
    record_monitor_poll(
        complete(now), data, state, now - timedelta(seconds=10), now - timedelta(seconds=5)
    )
    assert (state / RECEIPT).is_file()


def test_once_publishes_only_after_a_completed_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data, state, now = setup(tmp_path)
    observed: list[datetime] = []

    def fake_observe(data: Path, backups: Path, now: datetime) -> HealthSnapshot:
        assert not (state / RECEIPT).exists()  # the old receipt is invalidated up front
        observed.append(now)
        return complete(now)

    monkeypatch.setattr(health_cli, "observe", fake_observe)
    monkeypatch.setattr(health_cli, "_notifier", rejected_notifier)

    assert run_once(data, state) == 0
    assert observed
    assert (state / RECEIPT).is_file()
    assert ready(data, state, now - timedelta(minutes=5), datetime.now(UTC)) is True


def test_interrupted_observation_removes_prior_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data, state, now = setup(tmp_path)
    prior_receipt(data, state, now)

    def boom(data: Path, backups: Path, now: datetime) -> HealthSnapshot:
        raise RuntimeError("observer crashed")

    monkeypatch.setattr(health_cli, "observe", boom)
    monkeypatch.setattr(health_cli, "_notifier", rejected_notifier)

    with pytest.raises(RuntimeError):
        run_once(data, state)
    assert not (state / RECEIPT).exists()


def test_blind_poll_removes_prior_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data, state, now = setup(tmp_path)
    prior_receipt(data, state, now)

    def fake_observe(data: Path, backups: Path, now: datetime) -> HealthSnapshot:
        return blind()

    monkeypatch.setattr(health_cli, "observe", fake_observe)
    monkeypatch.setattr(health_cli, "_notifier", rejected_notifier)

    assert run_once(data, state) == 0
    assert not (state / RECEIPT).exists()
    assert ready(data, state, now - timedelta(minutes=5), datetime.now(UTC)) is False


def test_notifier_error_does_not_reuse_obsolete_healthy_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data, state, now = setup(tmp_path)
    prior_receipt(data, state, now)

    def fake_observe(data: Path, backups: Path, now: datetime) -> HealthSnapshot:
        return complete(now, no_backup=True)

    def raising_notifier(environ: dict[str, str]) -> tuple[Notifier, str]:
        def notify(message: EmailMessage, key: str) -> EmailOutcome:
            raise RuntimeError("provider unavailable")

        return notify, "owner@example.test"

    monkeypatch.setattr(health_cli, "observe", fake_observe)
    monkeypatch.setattr(health_cli, "_notifier", raising_notifier)

    with pytest.raises(RuntimeError):
        run_once(data, state)
    assert not (state / RECEIPT).exists()
    assert ready(data, state, now - timedelta(minutes=5), datetime.now(UTC)) is False
