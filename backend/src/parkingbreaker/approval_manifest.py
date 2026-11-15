"""Durable approval & dispatch audit manifest (audit A031, A032, A034, A035, A085).

Binds customer approval to the exact packet PDF bytes, canonical envelope, raw and
approved customer text hashes, ordered declaration affirmations, explicit UTC timestamp,
case ID, and build commit. Verifies byte-for-byte identity before dispatch or retry.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from parkingbreaker.lob_submission import LetterRequest
from parkingbreaker.mail_request import envelope_sha256


class ApprovalIntegrityError(ValueError):
    """Raised when an approval or dispatch manifest fails byte-identity verification."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("approval timestamp must be timezone-aware UTC")
    return dt.astimezone(UTC).isoformat()


def affirmations_digest(affirmations: Sequence[tuple[str, str]]) -> str:
    """Canonical SHA-256 over ordered (declaration_id, text_sha256) pairs."""
    canonical = json.dumps(
        [[decl_id, text_sha] for decl_id, text_sha in affirmations],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return _sha256_text(canonical)


@dataclass(frozen=True)
class ApprovalManifest:
    case_id: str
    case_token: str
    attempt: int
    approved_at_utc: str
    build_commit: str | None
    raw_customer_text_sha256: str
    approved_text_sha256: str
    packet_pdf_sha256: str
    envelope_sha256: str
    affirmations: tuple[tuple[str, str], ...]
    affirmations_sha256: str
    manifest_sha256: str
    provider_letter_id: str | None = None
    expected_delivery: str | None = None
    dispatched_pdf_sha256: str | None = None
    dispatched_at_utc: str | None = None


def _compute_manifest_sha256(
    *,
    case_id: str,
    case_token: str,
    attempt: int,
    approved_at_utc: str,
    build_commit: str | None,
    raw_customer_text_sha256: str,
    approved_text_sha256: str,
    packet_pdf_sha256: str,
    envelope_sha256_hex: str,
    affirmations_sha256: str,
) -> str:
    payload = json.dumps(
        {
            "case_id": case_id,
            "case_token": case_token,
            "attempt": attempt,
            "approved_at_utc": approved_at_utc,
            "build_commit": build_commit,
            "raw_customer_text_sha256": raw_customer_text_sha256,
            "approved_text_sha256": approved_text_sha256,
            "packet_pdf_sha256": packet_pdf_sha256,
            "envelope_sha256": envelope_sha256_hex,
            "affirmations_sha256": affirmations_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return _sha256_text(payload)


def build_approval_manifest(
    *,
    case_id: str,
    case_token: str,
    attempt: int,
    approved_at: datetime,
    build_commit: str | None,
    raw_customer_text: str,
    approved_text: str,
    request: LetterRequest,
    affirmations: Sequence[tuple[str, str]],
) -> ApprovalManifest:
    """Build a tamper-evident ApprovalManifest from exact approved inputs."""
    approved_at_utc = _utc_iso(approved_at)
    raw_sha = _sha256_text(raw_customer_text)
    approved_sha = _sha256_text(approved_text)
    pdf_sha = _sha256_bytes(request.pdf)
    env_sha = envelope_sha256(request)
    ordered_affirmations = tuple((str(d), str(h)) for d, h in affirmations)
    aff_sha = affirmations_digest(ordered_affirmations)
    manifest_sha = _compute_manifest_sha256(
        case_id=case_id,
        case_token=case_token,
        attempt=attempt,
        approved_at_utc=approved_at_utc,
        build_commit=build_commit,
        raw_customer_text_sha256=raw_sha,
        approved_text_sha256=approved_sha,
        packet_pdf_sha256=pdf_sha,
        envelope_sha256_hex=env_sha,
        affirmations_sha256=aff_sha,
    )
    return ApprovalManifest(
        case_id=case_id,
        case_token=case_token,
        attempt=attempt,
        approved_at_utc=approved_at_utc,
        build_commit=build_commit,
        raw_customer_text_sha256=raw_sha,
        approved_text_sha256=approved_sha,
        packet_pdf_sha256=pdf_sha,
        envelope_sha256=env_sha,
        affirmations=ordered_affirmations,
        affirmations_sha256=aff_sha,
        manifest_sha256=manifest_sha,
    )


def build_remail_approval_manifest(
    original: ApprovalManifest,
    request: LetterRequest,
    *,
    approved_at: datetime,
) -> ApprovalManifest:
    """Build attempt-2 approval manifest inheriting approved customer text and affirmations."""
    approved_at_utc = _utc_iso(approved_at)
    pdf_sha = _sha256_bytes(request.pdf)
    env_sha = envelope_sha256(request)
    manifest_sha = _compute_manifest_sha256(
        case_id=original.case_id,
        case_token=original.case_token,
        attempt=2,
        approved_at_utc=approved_at_utc,
        build_commit=original.build_commit,
        raw_customer_text_sha256=original.raw_customer_text_sha256,
        approved_text_sha256=original.approved_text_sha256,
        packet_pdf_sha256=pdf_sha,
        envelope_sha256_hex=env_sha,
        affirmations_sha256=original.affirmations_sha256,
    )
    return ApprovalManifest(
        case_id=original.case_id,
        case_token=original.case_token,
        attempt=2,
        approved_at_utc=approved_at_utc,
        build_commit=original.build_commit,
        raw_customer_text_sha256=original.raw_customer_text_sha256,
        approved_text_sha256=original.approved_text_sha256,
        packet_pdf_sha256=pdf_sha,
        envelope_sha256=env_sha,
        affirmations=original.affirmations,
        affirmations_sha256=original.affirmations_sha256,
        manifest_sha256=manifest_sha,
    )


def verify_dispatch_integrity(
    manifest: ApprovalManifest,
    request: LetterRequest,
    affirmations: Sequence[tuple[str, str]] | None = None,
) -> None:
    """Verify 1-byte identity of PDF, envelope, manifest digest, and affirmations."""
    expected_manifest_sha = _compute_manifest_sha256(
        case_id=manifest.case_id,
        case_token=manifest.case_token,
        attempt=manifest.attempt,
        approved_at_utc=manifest.approved_at_utc,
        build_commit=manifest.build_commit,
        raw_customer_text_sha256=manifest.raw_customer_text_sha256,
        approved_text_sha256=manifest.approved_text_sha256,
        packet_pdf_sha256=manifest.packet_pdf_sha256,
        envelope_sha256_hex=manifest.envelope_sha256,
        affirmations_sha256=manifest.affirmations_sha256,
    )
    if not hmac.compare_digest(manifest.manifest_sha256, expected_manifest_sha):
        raise ApprovalIntegrityError("manifest_tampered")
    current_pdf_sha = _sha256_bytes(request.pdf)
    if not hmac.compare_digest(manifest.packet_pdf_sha256, current_pdf_sha):
        raise ApprovalIntegrityError("packet_pdf_mismatch")
    current_env_sha = envelope_sha256(request)
    if not hmac.compare_digest(manifest.envelope_sha256, current_env_sha):
        raise ApprovalIntegrityError("envelope_mismatch")
    if affirmations is not None:
        current_aff_sha = affirmations_digest(affirmations)
        if not hmac.compare_digest(manifest.affirmations_sha256, current_aff_sha):
            raise ApprovalIntegrityError("affirmations_mismatch")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pb_approval_manifests (
    case_token TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt IN (1, 2)),
    case_id TEXT NOT NULL,
    approved_at_utc TEXT NOT NULL,
    build_commit TEXT,
    raw_customer_text_sha256 TEXT NOT NULL,
    approved_text_sha256 TEXT NOT NULL,
    packet_pdf_sha256 TEXT NOT NULL,
    envelope_sha256 TEXT NOT NULL,
    affirmations_json TEXT NOT NULL,
    affirmations_sha256 TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    provider_letter_id TEXT,
    expected_delivery TEXT,
    dispatched_pdf_sha256 TEXT,
    dispatched_at_utc TEXT,
    PRIMARY KEY (case_token, attempt)
);
"""

# Approval identity is immutable from insertion; only dispatch completion may be added.
# Refuse REPLACE explicitly because its implicit delete can bypass DELETE triggers.
APPROVAL_MANIFEST_TRIGGER_NAMES: tuple[str, ...] = (
    "pb_approval_manifests_approval_immutable",
    "pb_approval_manifests_no_replace",
    "pb_approval_manifests_no_delete",
    "pb_approval_manifests_dispatch_once",
)

APPROVAL_MANIFEST_TRIGGER_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TRIGGER IF NOT EXISTS pb_approval_manifests_approval_immutable
    BEFORE UPDATE ON pb_approval_manifests
    WHEN NEW.case_token IS NOT OLD.case_token OR NEW.attempt IS NOT OLD.attempt
      OR NEW.case_id IS NOT OLD.case_id OR NEW.approved_at_utc IS NOT OLD.approved_at_utc
      OR NEW.build_commit IS NOT OLD.build_commit
      OR NEW.raw_customer_text_sha256 IS NOT OLD.raw_customer_text_sha256
      OR NEW.approved_text_sha256 IS NOT OLD.approved_text_sha256
      OR NEW.packet_pdf_sha256 IS NOT OLD.packet_pdf_sha256
      OR NEW.envelope_sha256 IS NOT OLD.envelope_sha256
      OR NEW.affirmations_json IS NOT OLD.affirmations_json
      OR NEW.affirmations_sha256 IS NOT OLD.affirmations_sha256
      OR NEW.manifest_sha256 IS NOT OLD.manifest_sha256
    BEGIN
        SELECT RAISE(ABORT, 'approval identity is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS pb_approval_manifests_no_replace
    BEFORE INSERT ON pb_approval_manifests
    WHEN EXISTS (SELECT 1 FROM pb_approval_manifests
                 WHERE case_token = NEW.case_token AND attempt = NEW.attempt)
    BEGIN
        SELECT RAISE(ABORT, 'approval manifests are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS pb_approval_manifests_no_delete
    BEFORE DELETE ON pb_approval_manifests
    BEGIN
        SELECT RAISE(ABORT, 'approval manifests are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS pb_approval_manifests_dispatch_once
    BEFORE UPDATE OF provider_letter_id, expected_delivery, dispatched_pdf_sha256, dispatched_at_utc
    ON pb_approval_manifests
    WHEN OLD.dispatched_pdf_sha256 IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'approval manifest dispatch already recorded');
    END
    """,
)


def ensure_approval_manifest_triggers(database: sqlite3.Connection) -> None:
    """Create append-only triggers on pb_approval_manifests without implicit commits."""
    for statement in APPROVAL_MANIFEST_TRIGGER_STATEMENTS:
        database.execute(statement)


def initialize_approval_manifests(database: sqlite3.Connection) -> None:
    version = database.execute(
        "SELECT version FROM pb_store_versions WHERE component='approval_manifests'"
    ).fetchone()
    if version is None:
        database.execute(_SCHEMA)
        database.execute("INSERT INTO pb_store_versions VALUES ('approval_manifests', 1)")
    ensure_approval_manifest_triggers(database)


class ApprovalManifestStoreMixin:
    if TYPE_CHECKING:

        def _require_open(self) -> sqlite3.Connection: ...

    def record_approval_manifest(
        self,
        manifest: ApprovalManifest,
        *,
        manage_transaction: bool = True,
    ) -> ApprovalManifest:
        db = self._require_open()
        existing = self.get_approval_manifest(manifest.case_token, manifest.attempt)
        if existing is not None:
            if not hmac.compare_digest(existing.manifest_sha256, manifest.manifest_sha256):
                raise ApprovalIntegrityError("approval_manifest_conflict")
            return existing
        db.execute(
            "INSERT INTO pb_approval_manifests ("
            "case_token, attempt, case_id, approved_at_utc, build_commit, "
            "raw_customer_text_sha256, approved_text_sha256, packet_pdf_sha256, "
            "envelope_sha256, affirmations_json, affirmations_sha256, manifest_sha256"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                manifest.case_token,
                manifest.attempt,
                manifest.case_id,
                manifest.approved_at_utc,
                manifest.build_commit,
                manifest.raw_customer_text_sha256,
                manifest.approved_text_sha256,
                manifest.packet_pdf_sha256,
                manifest.envelope_sha256,
                json.dumps(
                    [[d, h] for d, h in manifest.affirmations],
                    separators=(",", ":"),
                    ensure_ascii=True,
                ),
                manifest.affirmations_sha256,
                manifest.manifest_sha256,
            ),
        )
        if manage_transaction:
            db.commit()
        return manifest

    def get_approval_manifest(self, case_token: str, attempt: int) -> ApprovalManifest | None:
        db = self._require_open()
        row = db.execute(
            "SELECT case_id, case_token, attempt, approved_at_utc, build_commit, "
            "raw_customer_text_sha256, approved_text_sha256, packet_pdf_sha256, "
            "envelope_sha256, affirmations_json, affirmations_sha256, manifest_sha256, "
            "provider_letter_id, expected_delivery, dispatched_pdf_sha256, dispatched_at_utc "
            "FROM pb_approval_manifests WHERE case_token = ? AND attempt = ?",
            (case_token, attempt),
        ).fetchone()
        if row is None:
            return None
        parsed_affs = tuple((str(d), str(h)) for d, h in json.loads(str(row[9])))
        return ApprovalManifest(
            case_id=str(row[0]),
            case_token=str(row[1]),
            attempt=int(row[2]),
            approved_at_utc=str(row[3]),
            build_commit=None if row[4] is None else str(row[4]),
            raw_customer_text_sha256=str(row[5]),
            approved_text_sha256=str(row[6]),
            packet_pdf_sha256=str(row[7]),
            envelope_sha256=str(row[8]),
            affirmations=parsed_affs,
            affirmations_sha256=str(row[10]),
            manifest_sha256=str(row[11]),
            provider_letter_id=None if row[12] is None else str(row[12]),
            expected_delivery=None if row[13] is None else str(row[13]),
            dispatched_pdf_sha256=None if row[14] is None else str(row[14]),
            dispatched_at_utc=None if row[15] is None else str(row[15]),
        )

    def record_dispatch_manifest(
        self,
        case_token: str,
        attempt: int,
        *,
        provider_letter_id: str,
        expected_delivery: str | None,
        dispatched_pdf: bytes,
        dispatched_at: datetime,
        manage_transaction: bool = True,
    ) -> ApprovalManifest:
        db = self._require_open()
        existing = self.get_approval_manifest(case_token, attempt)
        if existing is None:
            raise ApprovalIntegrityError("missing_approval_manifest")
        dispatched_sha = _sha256_bytes(dispatched_pdf)
        if not hmac.compare_digest(existing.packet_pdf_sha256, dispatched_sha):
            raise ApprovalIntegrityError("dispatched_pdf_mismatch")
        if existing.dispatched_pdf_sha256 is not None:
            if (
                existing.provider_letter_id == provider_letter_id
                and existing.expected_delivery == expected_delivery
                and hmac.compare_digest(existing.dispatched_pdf_sha256, dispatched_sha)
            ):
                return existing
            raise ApprovalIntegrityError("dispatch_manifest_conflict")
        dispatched_utc = _utc_iso(dispatched_at)
        db.execute(
            "UPDATE pb_approval_manifests "
            "SET provider_letter_id = ?, expected_delivery = ?, "
            "dispatched_pdf_sha256 = ?, dispatched_at_utc = ? "
            "WHERE case_token = ? AND attempt = ? AND dispatched_pdf_sha256 IS NULL",
            (
                provider_letter_id,
                expected_delivery,
                dispatched_sha,
                dispatched_utc,
                case_token,
                attempt,
            ),
        )
        if manage_transaction:
            db.commit()
        updated = self.get_approval_manifest(case_token, attempt)
        assert updated is not None
        return updated
