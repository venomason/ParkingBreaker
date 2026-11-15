"""Read-only runtime release evidence; never runs dispatch or changes case data.

The monitor publishes a small receipt after a completed poll with known observations.
Alerts (including no backup or no sweep on a first boot) do not invalidate a poll.
Release additionally requires a completed, successful sweep from the candidate runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from parkingbreaker.health import HealthSnapshot

RECEIPT = "health-poll.json"
MAX_RECEIPT_BYTES = 2048
MAX_POLL_AGE = timedelta(seconds=150)  # Normal monitor interval is 120 seconds.
_KEYS = {
    "version",
    "observer",
    "pid",
    "process_birth",
    "database_identity",
    "started_at",
    "completed_at",
}


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed


def _process_birth(pid: int) -> str:
    # Linux /proc field 22 survives exec and distinguishes a recycled PID. The command
    # name may contain spaces/parentheses, so split only after its closing delimiter.
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    if fields[0] in {"Z", "X"}:
        raise ValueError("monitor exited")
    return fields[19]


def _database_identity(data: Path) -> list[int]:
    stat = (data / "parkingbreaker.sqlite").stat()
    return [stat.st_dev, stat.st_ino]


def record_monitor_poll(
    snapshot: HealthSnapshot,
    data: Path,
    state: Path,
    started_at: datetime,
    completed_at: datetime,
) -> None:
    """Publish only complete observations; remove prior evidence when a poll is blind."""
    receipt = state / RECEIPT
    if (
        snapshot.sweep is None
        or snapshot.stuck is None
        or snapshot.captures is None
        or snapshot.disk is None
        or snapshot.holds is None
        or not snapshot.webhook_backlog_known
        or (snapshot.last_backup_at is None and not snapshot.no_backup)
    ):
        receipt.unlink(missing_ok=True)
        return
    payload = {
        "version": 1,
        "observer": "parkingbreaker.health_cli.monitor",
        "pid": os.getpid(),
        "process_birth": _process_birth(os.getpid()),
        "database_identity": _database_identity(data),
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
    }
    # Atomic replacement prevents a reader from accepting a partially written poll.
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=state, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(receipt)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def invalidate_monitor_poll(state: Path) -> None:
    """Remove prior evidence at the start of a poll; only a completed poll may republish."""
    (state / RECEIPT).unlink(missing_ok=True)


def ready(data: Path, state: Path, not_before: datetime, now: datetime) -> bool:
    """Check bounded, fresh evidence using only the monitor receipt and read-only DB."""
    try:
        if not_before.tzinfo is None or now.tzinfo is None or not_before > now:
            return False
        receipt = state / RECEIPT
        if receipt.is_symlink():
            return False
        with receipt.open("rb") as handle:
            raw = handle.read(MAX_RECEIPT_BYTES + 1)
        if len(raw) > MAX_RECEIPT_BYTES:
            return False
        decoded: object = json.loads(raw)
        if not isinstance(decoded, dict) or set(decoded) != _KEYS:
            return False
        poll = cast("dict[str, object]", decoded)
        if type(poll["version"]) is not int or poll["version"] != 1:
            return False
        if poll["observer"] != "parkingbreaker.health_cli.monitor":
            return False
        pid = poll["pid"]
        if type(pid) is not int or pid <= 0:
            return False
        if poll["process_birth"] != _process_birth(pid):
            return False
        identity = _database_identity(data)
        if poll["database_identity"] != identity:
            return False
        started, completed = _time(poll["started_at"]), _time(poll["completed_at"])
        if not (not_before <= started <= completed <= now):
            return False
        if now - completed > MAX_POLL_AGE:
            return False
        database = data / "parkingbreaker.sqlite"
        with closing(
            sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
        ) as db:
            # The newest run must itself prove success. Filtering to outcome='ok' here
            # would hide a newer failed, partial or still-running sweep.
            row = db.execute(
                "SELECT started_at, finished_at, outcome, failed FROM pb_sweep_runs "
                "ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return False
        sweep_started, sweep_finished, outcome, failed = row
        if outcome != "ok" or sweep_finished is None:
            return False
        if type(failed) is not int or failed != 0:
            return False
        sweep_started_at, sweep_finished_at = _time(sweep_started), _time(sweep_finished)
        return (
            not_before <= sweep_started_at <= sweep_finished_at <= now
            and _database_identity(data) == identity
        )
    except (OSError, ValueError, TypeError, IndexError, sqlite3.Error):
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="parkingbreaker.release_readiness")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--not-before", required=True)
    args = parser.parse_args(argv)
    try:
        boundary = _time(args.not_before)
    except ValueError:
        return 1
    return 0 if ready(args.data, args.state, boundary, datetime.now(UTC)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
