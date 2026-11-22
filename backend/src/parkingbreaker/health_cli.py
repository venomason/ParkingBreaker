"""Health view and monitor loop (T24). I/O entry point; the rules live in ``health``.

``check`` prints the current alerts as JSON (exit 1 if any). ``monitor`` runs in its own
container: every interval it observes the database read-only, evaluates the rules and
delivers incidents by email to ``PB_ALERT_EMAIL_TO`` through the existing Resend client
(``RESEND_API_KEY``, ``RESEND_FROM_EMAIL``). Without email configuration it logs alerts to
stderr only. It never writes case data. A whole-VM failure stops this monitor too and is
outside its coverage.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from parkingbreaker.email_client import EmailMessage, EmailOutcome, EmailReason, ResendEmailClient
from parkingbreaker.health import evaluate
from parkingbreaker.health_service import IncidentLog, Notifier, observe
from parkingbreaker.release_readiness import invalidate_monitor_poll, record_monitor_poll


def _stderr_notifier(message: EmailMessage, key: str) -> EmailOutcome:
    # No email configured: the alert is visible in the container log only, and is
    # retried each poll, since nothing confirmed delivery.
    print(
        json.dumps({"event": "alert_not_emailed", "key": key, "subject": message.subject}),
        file=sys.stderr,
        flush=True,
    )
    return EmailOutcome("rejected", reason=EmailReason.INVALID_MESSAGE)


def _notifier(environ: dict[str, str]) -> tuple[Notifier, str]:
    to = environ.get("PB_ALERT_EMAIL_TO", "")
    key = environ.get("RESEND_API_KEY", "")
    sender = environ.get("RESEND_FROM_EMAIL", "")
    if to and key and sender:
        return ResendEmailClient(key, sender).send, to
    return _stderr_notifier, to or "unconfigured"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="parkingbreaker.health_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "monitor"):
        command = sub.add_parser(name)
        command.add_argument("--data", type=Path, required=True)
        command.add_argument("--backups", type=Path)
        if name == "monitor":
            command.add_argument("--state", type=Path, required=True)
            command.add_argument("--interval", type=int, default=120)
            command.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    backups = args.backups if args.backups is not None else args.data / "recovery"
    require_replica = os.environ.get("PB_REQUIRE_OFF_HOST_REPLICA") == "1"
    if args.command == "check":
        now = datetime.now(UTC)
        alerts = evaluate(observe(args.data, backups, now, require_off_host=require_replica), now)
        print(json.dumps([asdict(alert) for alert in alerts], indent=2))
        return 1 if alerts else 0
    notify, to = _notifier(dict(os.environ))
    log = IncidentLog(args.state / "incidents.sqlite")
    try:
        while True:
            now = datetime.now(UTC)
            # A poll that is in progress, blind or interrupted must never leave the
            # previous healthy receipt behind; only a completed poll republishes.
            invalidate_monitor_poll(args.state)
            snapshot = observe(args.data, backups, now, require_off_host=require_replica)
            alerts = evaluate(snapshot, now)
            log.deliver(alerts, now, notify, to)
            record_monitor_poll(snapshot, args.data, args.state, now, datetime.now(UTC))
            print(
                json.dumps({"event": "health", "alerts": [a.kind.value for a in alerts]}),
                flush=True,
            )
            if args.once:
                return 0
            time.sleep(max(30, args.interval))
    finally:
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
