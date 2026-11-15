"""Private operator CLI for the runtime service phase. No public endpoint exists.

``status`` reports the stored override or its honest absence; ``set`` appends one change
under an exact expected revision. Both commands open the supported transaction store on an
existing database path only, so a typo cannot create a new production-looking database.
No provider, credential or settings boundary is composed here.

Example (see ``docs/operations/service-mode.md``)::

    python -m parkingbreaker.service_phase_cli --database /srv/pb/parkingbreaker.sqlite status
    python -m parkingbreaker.service_phase_cli --database /srv/pb/parkingbreaker.sqlite set \
        --phase controlled_free_beta --expected-revision 0 --command-id beta-on-2026-11-09 \
        --actor Amir --reason "Pause new paid authorizations"
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.service_phase import ServicePhase
from parkingbreaker.service_phase_store import (
    ServicePhaseChange,
    ServicePhaseConflict,
    ServicePhaseStatus,
)
from parkingbreaker.transaction_store import SqliteTransactionStore


def _status_payload(database: Path, status: ServicePhaseStatus) -> dict[str, object]:
    override = None if status.stored_override is None else status.stored_override.value
    payload: dict[str, object] = {
        "ok": True,
        "database": str(database),
        "stored_override": override,
        "revision": status.revision,
        "command_id": status.command_id,
        "actor": status.actor,
        "reason": status.reason,
        "recorded_at": None if status.recorded_at is None else status.recorded_at.isoformat(),
        # No override means the configured PB_SERVICE_PHASE remains in force. This CLI
        # does not read the process environment, so it never invents that value here.
        "effective_phase": override,
        "effective_source": "configured_fallback" if override is None else "stored_override",
    }
    if override is None:
        payload["note"] = "No stored override. The configured PB_SERVICE_PHASE remains in force."
    return payload


def _change_payload(database: Path, change: ServicePhaseChange) -> dict[str, object]:
    return {
        "ok": True,
        "database": str(database),
        "revision": change.revision,
        "phase": change.phase.value,
        "command_id": change.command_id,
        "actor": change.actor,
        "reason": change.reason,
        "recorded_at": change.recorded_at.isoformat(),
        "replayed": change.replayed,
    }


def _failure(database: Path, error: str) -> int:
    print(
        json.dumps({"ok": False, "database": str(database), "error": error}, indent=2),
        file=sys.stderr,
    )
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="parkingbreaker.service_phase_cli",
        description="Inspect or change the private runtime service phase.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="Existing ParkingBreaker SQLite database; never created by this CLI.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Print the stored override and revision as JSON.")
    change = commands.add_parser("set", help="Append one operator phase change.")
    change.add_argument("--phase", required=True, choices=[phase.value for phase in ServicePhase])
    change.add_argument("--expected-revision", required=True, type=int)
    change.add_argument("--command-id", required=True)
    change.add_argument("--actor", required=True)
    change.add_argument("--reason", required=True)
    args = parser.parse_args(argv)

    if not args.database.is_file():
        return _failure(args.database, "database_missing")
    try:
        with SqliteTransactionStore(args.database) as store:
            if args.command == "status":
                payload = _status_payload(args.database, store.runtime_service_phase_status())
                print(json.dumps(payload, indent=2))
                return 0
            saved = store.set_runtime_service_phase(
                ServicePhase(args.phase),
                args.expected_revision,
                args.command_id,
                args.actor,
                args.reason,
                now=datetime.now(UTC),
            )
            print(json.dumps(_change_payload(args.database, saved), indent=2))
            return 0
    except ServicePhaseConflict as exc:
        return _failure(args.database, str(exc))
    except (StorageCorruption, OSError, sqlite3.Error, ValueError) as exc:
        return _failure(args.database, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
