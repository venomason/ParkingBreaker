"""Private commands for opening and administering the free First-Class pilot."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from parkingbreaker.beta_store import FIRST_CLASS_PILOT_CAPACITY, BetaConflict, FirstClassPilot
from parkingbreaker.config import PHASE_VARIABLE
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.mail_config import MailClass
from parkingbreaker.route_registry import RouteScope
from parkingbreaker.service_phase import ServicePhase, parse_service_phase
from parkingbreaker.transaction_store import SqliteTransactionStore


def _database_path(path: Path) -> Path:
    if not path.is_file():
        raise BetaConflict("existing_beta_database_required")
    resolved = path.resolve(strict=True)
    for directory in {path.parent, resolved.parent}:
        for name in ("RESTORE_QUARANTINE.json", "FAKE_RECOVERY_ONLY"):
            marker = directory / name
            if marker.exists() or marker.is_symlink():
                raise BetaConflict("beta_management_disabled_on_restored_data")
    return resolved


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="parkingbreaker.beta_campaign_cli", description=__doc__)
    parser.add_argument(
        "--database", required=True, type=Path, help="Existing transaction database"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Show the stored First-Class pilot and its selected count")
    commands.add_parser("report", help="Private operational counts for every selected pilot case")
    opening = commands.add_parser("open-first-class", help="Open the one 14-day First-Class pilot")
    selection = commands.add_parser("select", help="Apply the pilot policy to one saved intake")
    for command in (opening, selection):
        command.add_argument("--campaign-id", required=True)
    opening.add_argument("--issuer", required=True)
    opening.add_argument("--stage", required=True)
    opening.add_argument("--identity-policy-ref", required=True)
    opening.add_argument("--configuration-ref", required=True)
    opening.add_argument("--evidence-sha256", required=True)
    selection.add_argument("--case-id", required=True)
    return parser


def _pilot_payload(pilot: FirstClassPilot | None) -> dict[str, object] | None:
    if pilot is None:
        return None
    return {
        "campaign_id": pilot.campaign_id,
        "issuer": pilot.scope.issuer,
        "stage": pilot.scope.stage,
        "mail_class": pilot.scope.mail_class,
        "starts_at": pilot.starts_at.isoformat(),
        "ends_at": pilot.ends_at.isoformat(),
        "capacity": FIRST_CLASS_PILOT_CAPACITY,
        "selected_count": pilot.selected_count,
        "evidence_sha256": pilot.evidence_sha256,
        "identity_policy_ref": pilot.identity_policy_ref,
        "configuration_ref": pilot.configuration_ref,
    }


def _feedback_report(store: SqliteTransactionStore) -> dict[str, object]:
    """Read the five-case operational record in one snapshot without customer identifiers."""
    db = store._require_open()
    db.execute("BEGIN")
    try:
        pilot = store.first_class_pilot_status()
        if pilot is None:
            return {"pilot": None}
        tokens = db.execute(
            "SELECT case_token FROM pb_beta_entitlements WHERE campaign_id=?",
            (pilot.campaign_id,),
        ).fetchall()
        routing: Counter[str] = Counter()
        acknowledgment: Counter[str] = Counter()
        disposition: Counter[str] = Counter()
        accepted = responded = scanned = handed_off = 0
        for (token,) in tokens:
            feedback = None if token is None else store.pilot_feedback(token)
            accepted += int(token is not None)
            responded += int(feedback is not None and feedback.updated_at is not None)
            scanned += int(feedback is not None and feedback.first_postal_scan_at is not None)
            handed_off += int(feedback is not None and feedback.printer_handoff_at is not None)
            routing[(feedback.routing if feedback else None) or "unreported"] += 1
            acknowledgment[(feedback.acknowledgment if feedback else None) or "unreported"] += 1
            disposition[(feedback.disposition if feedback else None) or "unreported"] += 1
        return {
            "pilot": pilot.campaign_id,
            "selected": len(tokens),
            "accepted_free_offer": accepted,
            "dated_postal_scan": scanned,
            "dated_printer_handoff": handed_off,
            "customers_reporting": responded,
            "customer_reported_routing": dict(routing),
            "customer_reported_acknowledgment": dict(acknowledgment),
            "customer_reported_disposition": dict(disposition),
        }
    finally:
        db.rollback()


def _run(
    store: SqliteTransactionStore, args: argparse.Namespace, now: datetime
) -> dict[str, object]:
    if args.command == "status":
        return {"pilot": _pilot_payload(store.first_class_pilot_status())}
    if args.command == "report":
        return _feedback_report(store)
    if args.command == "open-first-class":
        opened = store.open_first_class_pilot(
            args.campaign_id,
            RouteScope(args.issuer, args.stage, MailClass.FIRST_CLASS),
            args.identity_policy_ref,
            args.configuration_ref,
            args.evidence_sha256,
            now,
        )
        return {"pilot": _pilot_payload(opened)}
    pilot = store.first_class_pilot_status()
    if pilot is None or pilot.campaign_id != args.campaign_id:
        raise BetaConflict("first_class_pilot_not_found")
    fallback = parse_service_phase(os.environ.get(PHASE_VARIABLE, "").strip() or ServicePhase.PAID)
    selected = store.offer_first_class_pilot(args.case_id, now, configured_phase=fallback)
    if selected is None:
        raise BetaConflict("first_class_pilot_offer_unavailable")
    return {
        "campaign_id": selected.campaign_id,
        "batch_id": selected.batch_id,
        "selected_case_id": selected.intake_id,
        "eligible_until": selected.eligible_until.isoformat(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with SqliteTransactionStore(_database_path(args.database)) as store:
            payload = _run(store, args, datetime.now(UTC))
        print(json.dumps(payload))
        return 0
    except BetaConflict as exc:
        error = str(exc)
    except (OSError, sqlite3.Error, StorageCorruption):
        error = "beta_database_unavailable"
    except ValueError:
        error = "invalid_beta_campaign_command"
    print(json.dumps({"error": error}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
