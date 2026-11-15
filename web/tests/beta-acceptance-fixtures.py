#!/usr/bin/env python3
"""Test-only synthetic beta fixture controller.

Qualifies and selects one synthetic candidate through the existing beta store APIs,
or reports persisted proof. It refuses anything that is not an explicitly marked
synthetic rehearsal runtime and never touches a canonical or live data directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

MARKER = "ParkingBreaker synthetic rehearsal\n"
CASE = re.compile(r"case_[a-f0-9]{8,64}")
CAMPAIGN_PREFIX = "beta-canary-synthetic"
JURISDICTION_PREFIX = "us-ca-synthetic"


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"beta-acceptance-fixtures: {message}")


def runtime_paths() -> tuple[Path, Path]:
    if os.environ.get("SYNTHETIC_REHEARSAL") != "1":
        fail("SYNTHETIC_REHEARSAL=1 is required")
    raw = os.environ.get("PB_SYNTHETIC_RUNTIME", "")
    runtime = Path(raw)
    if not raw or not runtime.is_absolute():
        fail("PB_SYNTHETIC_RUNTIME must be an absolute synthetic runtime path")
    if any(path.is_symlink() for path in (runtime, *runtime.parents)):
        fail("synthetic runtime must not contain symlinks")
    if not runtime.is_dir() or (runtime.stat().st_mode & 0o777) != 0o700:
        fail("synthetic runtime must be a private mode0700 directory")
    marker = runtime / "SYNTHETIC_ONLY"
    if not marker.is_file() or marker.read_text() != MARKER:
        fail("synthetic runtime marker is missing or invalid")
    database = runtime / "data" / "parkingbreaker.sqlite"
    if not database.is_file() or (runtime / "data").is_symlink():
        fail("synthetic database is missing or linked")
    return runtime, database


def main() -> int:
    parser = argparse.ArgumentParser(description="synthetic beta fixture controller")
    parser.add_argument("--action", choices=("select", "proof"), required=True)
    parser.add_argument("--case-id", required=True)
    args = parser.parse_args()
    if CASE.fullmatch(args.case_id) is None:
        fail("case id is not a synthetic case id")
    _, database = runtime_paths()

    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "backend" / "src"))
    from parkingbreaker.transaction_store import SqliteTransactionStore

    if args.action == "select":
        now = datetime.now(UTC)
        # One synthetic campaign per case keeps independently selected fixtures isolated.
        campaign = f"{CAMPAIGN_PREFIX}-{args.case_id[-8:]}"
        jurisdiction = f"{JURISDICTION_PREFIX}-{args.case_id[-8:]}"
        store = SqliteTransactionStore(database)
        # Fixed synthetic campaign identity so repeated local runs stay idempotent.
        store.create_beta_campaign(
            campaign,
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
            "synthetic-identity-policy",
            "synthetic-configuration",
            now,
        )
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT prehold_json FROM pb_intake_records WHERE intake_id=?",
                (args.case_id,),
            ).fetchone()
        if row is None or not row[0]:
            fail("synthetic intake has no pre-hold offer to qualify")
        issuer = json.loads(row[0])["issuer"]["issuer_id"]
        store.bind_beta_jurisdiction(
            campaign,
            issuer,
            jurisdiction,
            hashlib.sha256(b"synthetic-jurisdiction-evidence").hexdigest(),
            now,
        )
        store.qualify_beta_candidate(
            campaign,
            args.case_id,
            hashlib.sha256(b"synthetic-qualification").hexdigest(),
            now + timedelta(hours=1),
            now,
        )
        stamp = now.strftime("%Y%m%d%H%M%S%f")
        # A later synthetic selection requires the earlier batch to be reviewed.
        with sqlite3.connect(database) as connection:
            unreviewed = [
                row[0]
                for row in connection.execute(
                    "SELECT batch_id FROM pb_beta_batches WHERE campaign_id=? "
                    "AND reviewed_at IS NULL ORDER BY batch_id",
                    (campaign,),
                )
            ]
        for batch_id in unreviewed:
            store.review_beta_batch(batch_id, f"review-synthetic-{stamp}-{batch_id[-8:]}", now)
        selected = store.select_beta_batch(
            campaign, f"batch-synthetic-{stamp}", f"approval-synthetic-{stamp}", now
        )
        print(json.dumps({"selected": [item.intake_id for item in selected]}))
        return 0

    with sqlite3.connect(database) as connection:
        entitlement = connection.execute(
            "SELECT accepted_at, case_token, disclosure_sha256 FROM pb_beta_entitlements "
            "WHERE intake_id=?",
            (args.case_id,),
        ).fetchone()
        authorization_attempts = connection.execute(
            "SELECT COUNT(*) FROM pb_intake_authorization_attempts WHERE intake_id=?",
            (args.case_id,),
        ).fetchone()[0]
        case = connection.execute(
            "SELECT case_token, payment_state FROM pb_cases WHERE case_token=("
            "SELECT case_token FROM pb_intake_records WHERE intake_id=?)",
            (args.case_id,),
        ).fetchone()
        mail_attempts = (
            0
            if case is None
            else connection.execute(
                "SELECT COUNT(*) FROM pb_mail_attempts WHERE case_token=?", (case[0],)
            ).fetchone()[0]
        )
    print(
        json.dumps(
            {
                "case_id": args.case_id,
                "entitlement_present": entitlement is not None,
                "accepted_at": None if entitlement is None else entitlement[0],
                "case_token": None if entitlement is None else entitlement[1],
                "disclosure_sha256": None if entitlement is None else entitlement[2],
                "payment_authorization_rows": authorization_attempts,
                "mail_attempt_rows": mail_attempts,
                "payment_state": None if case is None else case[1],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
