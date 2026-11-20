"""Offline recovery I/O. SQLite online backup is authority; replicas never become writers.

Photos are immutable, content-addressed writes before their DB reference commits. Pin the
SQLite snapshot first, then copy photo bytes and check every reference; concurrent deletion
or corruption fails the bundle without removing evidence. Frozen PDFs live inside SQLite;
export them too so restoration compares their bytes independently of customer record hashes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict, cast

from parkingbreaker.beta_feedback_store import (
    _COLUMNS as _NOTICE_COLUMNS,
)
from parkingbreaker.beta_feedback_store import (
    _DDL as _NOTICE_DDL,
)
from parkingbreaker.beta_feedback_store import (
    _TRIGGERS as _NOTICE_TRIGGERS,
)
from parkingbreaker.beta_feedback_store import (
    _accepted_beta as _accepted_notice_owner,
)
from parkingbreaker.beta_feedback_store import (
    _normalized as _normalized_schema,
)
from parkingbreaker.beta_feedback_store import (
    _receipt as _notice_receipt,
)
from parkingbreaker.case_state import CaseState
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import (
    Accepted,
    CancelableLobClient,
    Canceled,
    MetadataLookup,
    Uncertain,
    resolve_lookup,
)
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.payment_records import PaymentClient, PaymentOutcome, decode_cursor
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.recovery_policy import backup_room, letter_compatible, payment_compatible
from parkingbreaker.submission_records import decode_outcome
from parkingbreaker.transaction_store import SqliteTransactionStore

DATABASE = "parkingbreaker.sqlite"
QUARANTINE = "RESTORE_QUARANTINE.json"
DEFAULT_RESERVE_BYTES = 1 << 30  # the owner CLI's default; library callers choose their own


class Manifest(TypedDict):
    database_sha256: str
    records: dict[str, str]
    artifacts: dict[str, str]


class RestoreReport(TypedDict):
    elapsed_seconds: float
    records_match: bool
    artifacts_match: bool


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as out:
        path.chmod(0o600)
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def _sync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _alert(path: Path, event: str) -> None:
    # No provider errors/paths/customer content. Append, never truncate older evidence.
    print(json.dumps({"event": event}), file=sys.stderr, flush=True)
    try:
        with path.open("a", encoding="utf-8") as out:
            path.chmod(0o600)
            out.write(json.dumps({"event": event}) + "\n")
            out.flush()
            os.fsync(out.fileno())
    except OSError:
        print('{"event": "recovery_alert_write_failed"}', file=sys.stderr, flush=True)


def _connection(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise ValueError("regular database required")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def record_hashes(database: sqlite3.Connection) -> tuple[dict[str, str], set[str]]:
    """Hash typed, sorted full rows and schema, including every customer/intents table."""
    records: dict[str, str] = {}
    refs: set[str] = set()
    for name, schema in database.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ):
        encoded = []
        for row in database.execute(f"SELECT * FROM {_quote(name)}"):
            values = []
            for value in row:
                if isinstance(value, bytes):
                    values.append(["blob", value.hex()])
                else:
                    values.append([type(value).__name__, value])
                if isinstance(value, str):
                    refs.update(re.findall(r"photo_[a-f0-9]{64}", value))
            encoded.append(json.dumps(values, ensure_ascii=True, separators=(",", ":")))
        records[name] = _hash(json.dumps([schema, sorted(encoded)]).encode())
    return records, refs


def _artifacts(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for folder in ("photos", "pdfs"):
        base = root / folder
        if base.is_symlink():
            raise ValueError("symlink artifact directory")
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_symlink():
                raise ValueError("symlink artifact")
            if path.is_file() and not path.name.startswith("."):
                result[path.relative_to(root).as_posix()] = _hash(path.read_bytes())
    return result


def _notice_photo_paths(database: sqlite3.Connection, tables: set[str]) -> set[str]:
    """Required notice photos keyed by the actual validated case owner.

    The accepted F01 component is versioned and immutable. A missing component keeps
    legacy snapshots compatible; a present but inconsistent/unsupported one fails closed.
    Every immutable notice row is decoded, its saved owner is verified against the actual
    intake owner and accepted case binding, and every page ref becomes an owner-relative
    requirement. This is a read-only traversal: no store, initializer or migration runs.
    """
    notice_table = "pb_beta_feedback_notices"
    if "pb_store_versions" not in tables:
        if notice_table in tables:
            raise ValueError("invalid notice photo references")
        return set()
    version = database.execute(
        "SELECT version FROM pb_store_versions WHERE component='beta_feedback'"
    ).fetchone()
    table = database.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (notice_table,)
    ).fetchone()
    if version is None:
        if table is not None or notice_table in tables:
            raise ValueError("invalid notice photo references")
        return set()
    if (
        tuple(version) != (1,)
        or table is None
        or _normalized_schema(table[0]) != _normalized_schema(_NOTICE_DDL)
    ):
        raise ValueError("invalid notice photo references")
    for action in ("UPDATE", "DELETE"):
        trigger = database.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (f"{notice_table}_no_{action.lower()}",),
        ).fetchone()
        if trigger is None or _normalized_schema(trigger[0]) != _normalized_schema(
            _NOTICE_TRIGGERS[action]
        ):
            raise ValueError("invalid notice photo references")
    required: set[str] = set()
    rows = database.execute(
        f"SELECT {_NOTICE_COLUMNS} FROM pb_beta_feedback_notices ORDER BY sequence"
    ).fetchall()
    for row in rows:
        _, receipt = _notice_receipt(row)
        _accepted_notice_owner(database, receipt.case_id, receipt.owner)
        owner_folder = _hash(receipt.owner.encode())
        for page in receipt.pages:
            required.add(f"photos/{owner_folder}/{page.photo_ref}")
    return required


def _owned_photo_paths(database: sqlite3.Connection) -> set[str]:
    required: set[str] = set()
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master")}
    try:
        required |= _notice_photo_paths(database, tables)
    except Exception:
        # Fixed category only: never notice/case/owner/ref/date/deadline bytes or text.
        raise ValueError("invalid notice photo references") from None
    for table, columns in (
        ("pb_workspaces", ("payload_json",)),
        ("pb_workspace_history", ("payload_json",)),
        ("pb_intake_records", ("intake_json", "prehold_json", "agency_json")),
    ):
        if table not in tables or "pb_intake_owners" not in tables:
            continue
        selection = ",".join("r." + _quote(c) for c in columns)
        for owner, *payloads in database.execute(
            f"SELECT o.owner,{selection} FROM {_quote(table)} r "
            "JOIN pb_intake_owners o ON o.workspace_token=r.workspace_token"
        ):
            owner_folder = _hash(owner.encode())
            for payload in payloads:
                if isinstance(payload, str):
                    for ref in re.findall(r"photo_[a-f0-9]{64}", payload):
                        required.add(f"photos/{owner_folder}/{ref}")
    return required


def _manifest(root: Path) -> Manifest:
    value: Any = json.loads((root / "manifest.json").read_text())
    if not isinstance(value, dict) or set(value) != {"database_sha256", "records", "artifacts"}:
        raise ValueError("invalid manifest")
    if not isinstance(value["database_sha256"], str):
        raise ValueError("invalid database hash")
    for field in ("records", "artifacts"):
        if not isinstance(value[field], dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value[field].items()
        ):
            raise ValueError("invalid manifest hashes")
    return cast(Manifest, value)


def verify(root: Path) -> Manifest:
    if not (root / "COMPLETE").is_file() or (root / "INCOMPLETE").exists():
        raise ValueError("incomplete snapshot")
    manifest = _manifest(root)
    if _hash((root / DATABASE).read_bytes()) != manifest["database_sha256"]:
        raise ValueError("database hash mismatch")
    with closing(_connection(root / DATABASE)) as db:
        if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("database integrity failure")
        records, refs = record_hashes(db)
        owned_photos = _owned_photo_paths(db)
        pdfs = {}
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        if "pb_mail_attempts" in tables:
            for token, attempt, pdf in db.execute(
                "SELECT case_token, attempt, pdf FROM pb_mail_attempts"
            ):
                pdfs["pdfs/" + _hash(f"{token}:{attempt}".encode()) + ".pdf"] = _hash(pdf)
    if records != manifest["records"]:
        raise ValueError("customer record hash mismatch")
    actual = _artifacts(root)
    if not owned_photos <= set(actual):
        raise ValueError("referenced owned photo missing")
    if actual != manifest["artifacts"]:
        raise ValueError("artifact hash mismatch")
    if {name: digest for name, digest in actual.items() if name.startswith("pdfs/")} != pdfs:
        raise ValueError("exported PDF differs from frozen database bytes")
    photos = {Path(name).name for name in actual if name.startswith("photos/")}
    if not refs <= photos:
        raise ValueError("referenced photo missing")
    return manifest


def _require_room(data: Path, output: Path, reserve_bytes: int) -> None:
    """Refuse before writing anything when the copy could starve the authority disk.

    Needed: the database and its WAL (twice, for the exported frozen PDFs) plus every photo.
    """
    needed = 0
    for name in (DATABASE, DATABASE + "-wal"):
        if (data / name).is_file():
            needed += 2 * (data / name).stat().st_size
    photos = data / "photos"
    if photos.is_dir() and not photos.is_symlink():
        needed += sum(p.stat().st_size for p in photos.rglob("*") if p.is_file())
    probe = output.parent
    while not probe.exists():
        probe = probe.parent
    if not backup_room(
        free_bytes=shutil.disk_usage(probe).free, needed_bytes=needed, reserve_bytes=reserve_bytes
    ):
        _alert(output.parent / "recovery-alerts.jsonl", "backup_insufficient_space")
        raise ValueError("insufficient free space for a verified backup")


def backup(data: Path, output: Path, *, reserve_bytes: int = 0) -> Manifest:
    """Online SQLite backup; never copy the live DB/WAL files with shutil/cp."""
    if output.exists() or output.is_symlink() or output.resolve() == data.resolve():
        raise ValueError("new backup destination required")
    _require_room(data, output, reserve_bytes)
    output.mkdir(parents=True, mode=0o700)
    _write(output / "INCOMPLETE", b"backup pending\n")
    try:
        with (
            closing(_connection(data / DATABASE)) as source,
            closing(sqlite3.connect(output / DATABASE)) as dest,
        ):
            (output / DATABASE).chmod(0o600)
            # Establish the read view before copying immutable artifacts.
            source.execute("BEGIN")
            source.execute("SELECT count(*) FROM sqlite_master").fetchone()
            source.backup(dest, pages=256)
            dest.execute("PRAGMA journal_mode=DELETE")
            records, refs = record_hashes(dest)
            if dest.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("backup database integrity failure")
            photos = data / "photos"
            if photos.exists():
                # Only the canonical photo directory; recovery archives are never recursed.
                for name in _artifacts(data):
                    if name.startswith("photos/"):
                        payload = (data / name).read_bytes()
                        if Path(name).name != "photo_" + _hash(payload):
                            raise ValueError("photo content hash mismatch")
                        _write(output / name, payload)
            tables = {row[0] for row in dest.execute("SELECT name FROM sqlite_master")}
            if "pb_mail_attempts" in tables:
                for token, attempt, pdf in dest.execute(
                    "SELECT case_token, attempt, pdf FROM pb_mail_attempts"
                ):
                    name = _hash(f"{token}:{attempt}".encode()) + ".pdf"
                    _write(output / "pdfs" / name, pdf)
            artifacts = _artifacts(output)
            if not refs <= {Path(name).name for name in artifacts if name.startswith("photos/")}:
                raise ValueError("referenced photo missing")
        manifest: Manifest = {
            "database_sha256": _hash((output / DATABASE).read_bytes()),
            "records": records,
            "artifacts": artifacts,
        }
        _write(output / "manifest.json", json.dumps(manifest, sort_keys=True).encode())
        _write(output / "COMPLETE", b"verified snapshot; never synchronous authority\n")
        (output / "INCOMPLETE").unlink()
        for directory in sorted((p for p in output.rglob("*") if p.is_dir()), reverse=True):
            _sync_dir(directory)
        _sync_dir(output)
        _sync_dir(output.parent)
        return verify(output)
    except Exception:
        # Retain partial bytes and the failure marker, even if final verification failed.
        (output / "INCOMPLETE").touch(mode=0o600)
        _alert(output.parent / "recovery-alerts.jsonl", "backup_failed")
        raise


def restore(bundle: Path, destination: Path) -> RestoreReport:
    """Never promote or overwrite an authority path. Output stays quarantined."""
    if (
        destination.exists()
        or destination.is_symlink()
        or destination.resolve().is_relative_to(bundle.resolve())
    ):
        raise ValueError("new isolated restore destination required")
    destination.mkdir(parents=True, mode=0o700)
    _write(destination / QUARANTINE, b'{"status":"reconciliation_required"}\n')
    started = time.monotonic()
    try:
        manifest = verify(bundle)
        _write(destination / DATABASE, (bundle / DATABASE).read_bytes())
        for name in manifest["artifacts"]:
            # verify() compares actual canonical relative names before we use the manifest.
            _write(destination / name, (bundle / name).read_bytes())
        with closing(_connection(destination / DATABASE)) as db:
            if record_hashes(db)[0] != manifest["records"]:
                raise ValueError("restored record hash mismatch")
        if _artifacts(destination) != manifest["artifacts"]:
            raise ValueError("restored artifact hash mismatch")
        report: RestoreReport = {
            "elapsed_seconds": time.monotonic() - started,
            "records_match": True,
            "artifacts_match": True,
        }
        _write(destination / "restore-report.json", json.dumps(report).encode())
        _sync_dir(destination)
        return report
    except Exception:
        _alert(destination.parent / "recovery-alerts.jsonl", "restore_failed")
        raise


def replicate(bundle: Path, replica: Path) -> None:
    """Local transport rehearsal. Real off-site storage/credentials are NEEDS_EXTERNAL."""
    try:
        verify(bundle)
        if replica.exists() or replica.resolve().is_relative_to(bundle.resolve()):
            raise ValueError("new independent replica required")
        shutil.copytree(bundle, replica)
        verify(replica)
        _sync_dir(replica)
    except Exception:
        _alert(bundle.parent / "recovery-alerts.jsonl", "replication_failed")
        raise


class ReconciliationReport(TypedDict):
    status: str
    checked: int
    issues: list[str]


def export_reconciliation_inventory(destination: Path, output: Path) -> None:
    """Export local lookup anchors from a completed restore without opening it for writes."""
    marker = destination / QUARANTINE
    if not marker.is_file() or marker.is_symlink() or destination.is_symlink():
        raise ValueError("isolated quarantined restore required")
    if output.resolve().is_relative_to(destination.resolve()):
        raise ValueError("write inventory outside the restore directory")
    restore_receipt = json.loads((destination / "restore-report.json").read_text())
    if (
        restore_receipt["records_match"] is not True
        or restore_receipt["artifacts_match"] is not True
    ):
        raise ValueError("completed restore required")
    with closing(_connection(destination / DATABASE)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        payments = []
        for row in db.execute(
            """SELECT c.case_token, c.authorization_ref, c.amount_cents, c.payment_state,
                      p.payload
               FROM pb_cases c LEFT JOIN pb_payment_cursor p USING (case_token)
               WHERE c.payment_state != ? ORDER BY c.case_token""",
            (PaymentState.NO_CHARGE,),
        ):
            cursor = None if row["payload"] is None else decode_cursor(row["payload"])
            payments.append(
                {
                    "case_token": row["case_token"],
                    "authorization_ref": row["authorization_ref"],
                    "amount_cents": row["amount_cents"],
                    "restored_payment_state": row["payment_state"],
                    "charge_ref": (
                        None
                        if cursor is None
                        else cursor.outcome.original_charge_ref
                        or cursor.outcome.charge_ref
                        or cursor.authorization_charge_ref
                    ),
                    "refund_lookup_required": (
                        row["payment_state"] == PaymentState.REFUNDED
                        or (cursor is not None and cursor.kind == "refund")
                    ),
                }
            )
        letters = []
        for row in db.execute(
            """SELECT m.case_token, m.attempt, m.submission_token, m.pdf_sha256,
                      m.request_sha256, m.prepared_at, s.outcome_json,
                      a.manifest_sha256, a.envelope_sha256, a.provider_letter_id
               FROM pb_mail_attempts m
               LEFT JOIN pb_submission_cursors s USING (case_token, attempt)
               LEFT JOIN pb_approval_manifests a USING (case_token, attempt)
               ORDER BY m.case_token, m.attempt"""
        ):
            outcome = None if row["outcome_json"] is None else decode_outcome(row["outcome_json"])
            cursor_letter_id = (
                outcome.letter_id if isinstance(outcome, Accepted | Uncertain | Canceled) else None
            )
            letters.append(
                {
                    "case_token": row["case_token"],
                    "attempt": row["attempt"],
                    "prepared_at": row["prepared_at"],
                    "submission_token": row["submission_token"],
                    "cursor_letter_id": cursor_letter_id,
                    "dispatch_letter_id": row["provider_letter_id"],
                    "packet_pdf_sha256": row["pdf_sha256"],
                    "request_sha256": row["request_sha256"],
                    "approval_manifest_sha256": row["manifest_sha256"],
                    "approved_envelope_sha256": row["envelope_sha256"],
                }
            )
        open_authorizations = [
            dict(row)
            for row in db.execute(
                """SELECT intake_id, attempt_number, authorization_ref, amount_cents,
                          state, created_at
                   FROM pb_intake_authorization_attempts
                   WHERE state IN ('pending', 'uncertain') ORDER BY intake_id, attempt_number"""
            )
        ]
        missing_mail_anchors = [
            dict(row)
            for row in db.execute(
                """SELECT c.case_token, c.attempt, c.letter_state
                   FROM pb_cases c
                   WHERE c.letter_state IN ('FROZEN', 'SUBMITTING', 'IN_TRANSIT', 'DELIVERED',
                                            'RETURNED', 'CLOSED_RETURNED')
                     AND NOT EXISTS (
                         SELECT 1 FROM pb_mail_attempts m WHERE m.case_token = c.case_token
                         AND m.attempt = c.attempt
                     )
                   ORDER BY c.case_token"""
            )
        ]
    _write(
        output,
        (
            json.dumps(
                {
                    "status": "NEEDS_EXTERNAL",
                    "scope": "restored records only",
                    "stripe_payment_intents": payments,
                    "lob_letter_attempts": letters,
                    "unresolved_intake_authorizations": open_authorizations,
                    "missing_mail_anchors": missing_mail_anchors,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode(),
    )


def reconcile_restored(
    destination: Path,
    lob: CancelableLobClient,
    payments: PaymentClient,
    *,
    now: datetime,
    new_id: Callable[[], str],
) -> ReconciliationReport:
    """Offline original-object reconciliation, using the existing claim/finish APIs.

    Only the exact existing fakes may be exercised here. Real adapters/objects require
    target account/mode evidence and an owner-authorized run; never call them implicitly.
    No provider mutation is performed. Original prepared tokens, keys and charge anchors
    survive; a known remote effect is committed locally before the dispatcher can resume.
    A validated NO_CHARGE case has no payment object: it is reconciled by mail identity
    only, never by retrieving or comparing a provider authorization.
    """
    report: ReconciliationReport = {"status": "BLOCKED", "checked": 0, "issues": []}
    if type(lob) is not FakeLobClient or type(payments) is not FakePaymentClient:
        report["status"] = "NEEDS_EXTERNAL"
        report["issues"] = ["original Stripe/Lob account objects require external qualification"]
        return report
    if not (destination / QUARANTINE).is_file():
        raise ValueError("isolated quarantined restore required")
    try:
        with SqliteTransactionStore(destination / DATABASE) as store, store.submission_ownership():
            # No replacement authorization may be created from an unanchored restored intent.
            if (
                store._require_open()
                .execute(
                    "SELECT count(*) FROM pb_intake_authorization_attempts "
                    "WHERE state IN ('pending','uncertain')"
                )
                .fetchone()[0]
            ):
                raise ValueError("unanchored authorization requires external reconciliation")
            for token in store.case_ids():
                case = store.get_case(token)
                assert case is not None
                # A validated NO_CHARGE case has no provider payment object at all. Its
                # authorization_ref is beta service consent, so never ask an adapter to
                # retrieve it and never compare it as a Stripe authorization.
                no_charge = case.payment_state is PaymentState.NO_CHARGE
                remote: PaymentOutcome | None = None
                if not no_charge:
                    remote = payments.retrieve(case.authorization_ref)
                    if remote.authorization_ref != case.authorization_ref:
                        raise ValueError("original payment identity mismatch")
                for attempt in range(1, case.attempt + 1):
                    packet = store.get(token, attempt)
                    if packet is None:
                        # Snapshot may predate a prepared identity now known remotely.
                        raise ValueError("missing frozen identity requires external reconciliation")
                    cursor = store.submission_cursor(token, attempt)
                    outcome = None if cursor is None else cursor.outcome
                    original = (
                        outcome.letter_id if isinstance(outcome, Accepted | Uncertain) else None
                    )
                    found = lob.find_by_metadata(packet.request.submission_token)
                    if (
                        original is not None
                        and isinstance(found, MetadataLookup)
                        and found.letter_id is None
                    ):
                        found = lob.lookup_letter(original, packet.request.submission_token)
                    if not isinstance(found, MetadataLookup) or not letter_compatible(
                        original, found
                    ):
                        raise ValueError("original letter identity unresolved")
                    resolved = resolve_lookup(found, original)
                    if isinstance(resolved, Canceled):
                        raise ValueError("changed original letter needs external reconciliation")
                    if (
                        no_charge
                        and case.letter_state is CaseState.FROZEN
                        and found.letter_id is not None
                    ):
                        # A pre-submission snapshot holds no committed intent or saved
                        # create outcome, so a remote original cannot be proven consistent.
                        # Keep the quarantine instead of inferring a restored submission.
                        raise ValueError("pre-submission snapshot has an original remote letter")
                    if case.letter_state is CaseState.SUBMITTING and attempt == case.attempt:
                        if found.letter_id is not None:
                            claimed, _ = store.claim_submission(
                                token,
                                attempt,
                                case.revision,
                                None if cursor is None else cursor.revision,
                                now=now,
                                operation_id=new_id(),
                                evidence=True,
                            )
                            store.finish_submission(
                                claimed, resolved, now=now, effect_ids=(), new_effect_id=new_id
                            )
                        # Authoritative fake absence leaves the existing identity untouched;
                        # the normal coordinator still owns the bounded replay/lookup rule.
                    elif found.letter_id is None:
                        raise ValueError("saved mailing absent from original provider")
                case = store.get_case(token)
                assert case is not None
                if not no_charge:
                    assert remote is not None
                    if not payment_compatible(case.payment_state, remote, case.amount_cents):
                        raise ValueError("original payment status conflicts with restored state")
                    payment = store.payment_cursor(token)
                    if payment is not None and payment.kind == "refund":
                        # Refund inventories and remail histories need additional original objects.
                        raise ValueError("refund intent requires external reconciliation")
                    if (
                        case.payment_state
                        in {PaymentState.CAPTURE_PENDING, PaymentState.CAPTURE_RETRYING}
                        and remote.status == "succeeded"
                    ):
                        claimed_payment = store.claim_payment(
                            token,
                            case.revision,
                            None if payment is None else payment.revision,
                            now=now,
                            operation_id=new_id(),
                            key=f"pb-payment-{token}" if payment is None else payment.key,
                            retry_effect_id=new_id(),
                        )
                        store.finish_payment(claimed_payment, remote, now=now, new_effect_id=new_id)
                    elif case.payment_state is PaymentState.CAPTURED:
                        if payment is None or payment.outcome.charge_ref != remote.charge_ref:
                            raise ValueError("original charge identity mismatch")
                report["checked"] += 1
        report["status"] = "READY_FAKE"
    except Exception:
        # Preserve DB, quarantine and earlier reports. Provider strings never enter alerts.
        report["issues"].append(
            "original-object reconciliation incomplete; keep processing disabled"
        )
        _alert(destination.parent / "recovery-alerts.jsonl", "reconciliation_failed")
    _write(destination / f"reconciliation-{new_id()}.json", json.dumps(report).encode())
    if report["status"] == "READY_FAKE":
        marker = destination / "FAKE_RECOVERY_ONLY"
        if not marker.exists():
            _write(marker, b"offline fakes only; never enable test/live processing\n")
        _sync_dir(destination)
        (destination / QUARANTINE).unlink()
        _sync_dir(destination)
    return report
