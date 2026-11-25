"""Record independently verified city evidence from the local service account."""

import argparse
import hashlib
import os
import pwd
import sqlite3
import stat
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from parkingbreaker.transaction_store import RevisionConflict, SqliteTransactionStore


def _private_file_info(descriptor: int) -> os.stat_result:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("private file owned by this account required")
    return info


def record_verified_city(
    database: Path, evidence: Path, case_token: str, letter_id: str, finding: str
) -> bool:
    """Accept only from the OS account that owns the private transaction database."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(database, flags), "rb") as database_file:
        database_info = _private_file_info(database_file.fileno())
        with os.fdopen(os.open(evidence, flags), "rb") as evidence_file:
            evidence_info = _private_file_info(evidence_file.fileno())
            if evidence_info.st_size <= 0 or os.path.samestat(database_info, evidence_info):
                raise ValueError("separate, nonempty evidence file required")
            digest = hashlib.file_digest(evidence_file, "sha256").hexdigest()
            if (
                evidence_info.st_mtime_ns != os.fstat(evidence_file.fileno()).st_mtime_ns
                or evidence_info.st_size != os.fstat(evidence_file.fileno()).st_size
            ):
                raise ValueError("evidence changed during verification")
        with SqliteTransactionStore(database) as store, store.submission_ownership():
            if not os.path.samestat(database_info, database.stat()):
                raise ValueError("transaction database changed")
            return store.record_original_mail_finding(
                case_token,
                letter_id,
                "verified_city",
                finding,
                digest,
                pwd.getpwuid(os.geteuid()).pw_name,
                now=datetime.now(UTC),
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument(
        "evidence", type=Path, help="Private evidence file from the official source"
    )
    parser.add_argument("--case", required=True, help="Internal case token")
    parser.add_argument("--letter", required=True, help="Exact original Lob letter ID")
    parser.add_argument("--finding", required=True, choices=("city_receipt", "never_mailed"))
    parser.add_argument(
        "--attest-verified",
        action="store_true",
        required=True,
        help=(
            "I verified the official source and matched the original letter. A missing city record "
            "or a customer's assertion is insufficient."
        ),
    )
    args = parser.parse_args(argv)
    try:
        inserted = record_verified_city(
            args.database, args.evidence, args.case, args.letter, args.finding
        )
    except (OSError, ValueError, KeyError, RevisionConflict, sqlite3.Error):
        print(
            "City evidence refused. Check private file access, exact identity and prior bindings."
        )
        return 2
    print(
        "Verified city evidence recorded."
        if inserted
        else "Verified city evidence already recorded."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
