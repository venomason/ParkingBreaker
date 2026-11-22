"""Owner recovery commands. No provider setup/keys, deployment or remote storage calls."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image

from parkingbreaker import case_state as cs
from parkingbreaker import recovery
from parkingbreaker.agencies import MailingAddress
from parkingbreaker.blob_store import LocalBlobStore
from parkingbreaker.effect_dispatch import dispatch_once
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LetterOptions, LetterRequest, LobResponse
from parkingbreaker.mail_config import MailClass
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace


def rehearse(root: Path) -> dict[str, Any]:
    """Persist a synthetic seeded backup/restore/reconciliation with a lost local outcome."""
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    data = root / "authority"
    data.mkdir(mode=0o700)
    image = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(image, "PNG")
    photo = LocalBlobStore(data / "photos").put("synthetic-owner", image.getvalue())
    now = datetime(2026, 10, 22, 12, tzinfo=UTC)
    address = MailingAddress("Synthetic", None, "123 Example St", None, "Oakland", "CA", "94612")
    lob = FakeLobClient(
        [LobResponse(200, "lob-offline-original", evidence=LetterEvidence.PRODUCTION)]
    )
    payments = FakePaymentClient()
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        store.save_workspace(
            "synthetic-workspace",
            Workspace(position="My exact synthetic words.", photo_refs=(photo,)),
            now,
        )
        case = store.start_case("synthetic-workspace", "auth", now, now + timedelta(days=5))
        for event in (
            cs.ResearchComplete(),
            cs.DraftReady(),
            cs.CustomerApproved(),
            cs.SubmitRequested(),
        ):
            case = store.apply_letter_event(
                case.case_token,
                event,
                case.revision,
                occurred_at=now,
                new_effect_id=lambda: uuid.uuid4().hex,
            )
        request = LetterRequest(
            b"%PDF-1.4\nsynthetic frozen recovery fixture",
            "offline-submission-token",
            address,
            address,
            LetterOptions(MailClass.CERTIFIED),
        )
        packet = store.prepare(case.case_token, 1, request, now)
        with store.submission_ownership():
            store.claim_submission(
                case.case_token, 1, case.revision, None, now=now, operation_id="original-send"
            )
        recovery.backup(data, root / "backup")
        # Provider accepted but the original response never committed locally.
        lob.create_letter(packet.request, packet.key)
    restore = recovery.restore(root / "backup", root / "restored")
    reconciliation = recovery.reconcile_restored(
        root / "restored",
        lob,
        payments,
        now=now + timedelta(minutes=2),
        new_id=lambda: uuid.uuid4().hex,
    )
    if reconciliation["status"] != "READY_FAKE":
        raise ValueError("offline reconciliation failed")
    with SqliteTransactionStore(root / "restored" / recovery.DATABASE) as restored:
        for _ in range(3):
            dispatch_once(
                restored,
                lob,
                payments,
                lambda: now + timedelta(minutes=5),
                lambda: uuid.uuid4().hex,
            )
    recovery.replicate(root / "backup", root / "replica")
    result = {
        "restore": restore,
        "reconciliation": reconciliation,
        "letters": len(lob.calls),
        "captures": payments.captures,
    }
    (root / "rehearsal-report.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("backup", "restore", "replicate", "reconcile", "rehearse")
    )
    parser.add_argument("--data", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--reserve-bytes",
        type=int,
        default=recovery.DEFAULT_RESERVE_BYTES,
        help="free space a backup must leave on the destination disk",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run a seeded local rehearsal in a new temp directory",
    )
    parser.add_argument(
        "--record-off-host", action="store_true", help="record a verified off-host replication"
    )
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result: object
        if args.dry_run:
            with tempfile.TemporaryDirectory(prefix="pb-recovery-") as folder:
                result = rehearse(Path(folder) / "rehearsal")
        elif args.action == "rehearse" and args.output is not None:
            result = rehearse(args.output)
        elif args.action == "backup" and args.data is not None and args.output is not None:
            result = recovery.backup(args.data, args.output, reserve_bytes=args.reserve_bytes)
        elif args.action == "restore" and args.data is not None and args.output is not None:
            result = recovery.restore(args.data, args.output)
        elif args.action == "replicate" and args.data is not None and args.output is not None:
            recovery.replicate(args.data, args.output, record_off_host=args.record_off_host)
            result = {
                "status": (
                    "verified off-host replica"
                    if args.record_off_host
                    else "verified local replica; off-site storage NEEDS_EXTERNAL"
                )
            }
        elif args.action == "reconcile":
            if args.data is not None or args.output is not None:
                if args.data is None or args.output is None:
                    parser.error("reconcile inventory requires --data and new --output")
                recovery.export_reconciliation_inventory(args.data, args.output)
            print(
                json.dumps(
                    {
                        "status": "NEEDS_EXTERNAL",
                        "requires": (
                            "original account Stripe/Lob objects, account/mode "
                            "and target qualification"
                        ),
                    }
                )
            )
            return 3
        else:
            parser.error("--data and new --output required; rehearse requires --output")
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print(
            json.dumps(
                {
                    "event": f"{args.action}_failed",
                    "status": "preserve recovery evidence; processing remains disabled",
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
