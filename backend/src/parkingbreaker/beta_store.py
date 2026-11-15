"""Durable free-beta admission controls on the existing transaction database.

These methods neither activate providers nor start cases. A trusted qualification seam
supplies an evidence-bound canonical issuing jurisdiction; customer input cannot grant
an entitlement. Each selection closes its batch immediately, even if fewer than five
qualify. Existing admitted work is independent of admission/review state.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.research_records import decode_agency

MAX_BETA_BATCH = 5
BETA_DISCLOSURE_VERSION = 1
BETA_DISCLOSURE_TEXT = (
    "Your selected beta appeal is free. ParkingBreaker is still in development, and errors "
    "may occur. We will ask you to confirm your facts and the exact packet before mailing. "
    "Beta participation does not extend any ticket deadline. This free appeal includes one "
    "distinct mailing attempt; we will continue tracking and reconciling that attempt if a "
    "provider's answer is unclear. We will not automatically start a second appeal or re-mail."
)
BETA_DISCLOSURE_SHA256 = hashlib.sha256(BETA_DISCLOSURE_TEXT.encode()).hexdigest()
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}")
_HASH = re.compile(r"[0-9a-f]{64}")
_DDL = {
    "pb_beta_campaigns": """CREATE TABLE pb_beta_campaigns (
        campaign_id TEXT PRIMARY KEY, first_month_starts_at TEXT NOT NULL,
        first_month_ends_at TEXT NOT NULL, identity_policy_ref TEXT NOT NULL,
        configuration_ref TEXT NOT NULL, created_at TEXT NOT NULL
    )""",
    "pb_beta_jurisdiction_aliases": """CREATE TABLE pb_beta_jurisdiction_aliases (
        campaign_id TEXT NOT NULL REFERENCES pb_beta_campaigns(campaign_id),
        issuer_id TEXT NOT NULL, jurisdiction_key TEXT NOT NULL,
        evidence_sha256 TEXT NOT NULL, recorded_at TEXT NOT NULL,
        PRIMARY KEY (campaign_id, issuer_id)
    )""",
    "pb_beta_candidates": """CREATE TABLE pb_beta_candidates (
        campaign_id TEXT NOT NULL REFERENCES pb_beta_campaigns(campaign_id),
        intake_id TEXT NOT NULL REFERENCES pb_intake_records(intake_id),
        issuer_id TEXT NOT NULL, jurisdiction_key TEXT NOT NULL,
        input_sha256 TEXT NOT NULL, qualification_sha256 TEXT NOT NULL,
        eligible_until TEXT NOT NULL, qualified_at TEXT NOT NULL,
        PRIMARY KEY (campaign_id, intake_id),
        FOREIGN KEY (campaign_id, issuer_id)
            REFERENCES pb_beta_jurisdiction_aliases(campaign_id, issuer_id)
    )""",
    "pb_beta_batches": """CREATE TABLE pb_beta_batches (
        batch_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL
            REFERENCES pb_beta_campaigns(campaign_id),
        approval_ref TEXT NOT NULL UNIQUE, selected_at TEXT NOT NULL,
        review_ref TEXT UNIQUE, reviewed_at TEXT,
        CHECK ((review_ref IS NULL) = (reviewed_at IS NULL))
    )""",
    "pb_beta_entitlements": """CREATE TABLE pb_beta_entitlements (
        entitlement_id TEXT NOT NULL UNIQUE,
        intake_id TEXT PRIMARY KEY REFERENCES pb_intake_records(intake_id),
        batch_id TEXT NOT NULL REFERENCES pb_beta_batches(batch_id),
        campaign_id TEXT NOT NULL REFERENCES pb_beta_campaigns(campaign_id),
        jurisdiction_key TEXT NOT NULL, input_sha256 TEXT NOT NULL,
        qualification_sha256 TEXT NOT NULL, eligible_until TEXT NOT NULL,
        selected_at TEXT NOT NULL, accepted_at TEXT, disclosure_sha256 TEXT,
        case_token TEXT UNIQUE REFERENCES pb_cases(case_token),
        UNIQUE (batch_id, jurisdiction_key), UNIQUE (campaign_id, jurisdiction_key),
        CHECK ((accepted_at IS NULL) = (disclosure_sha256 IS NULL)),
        CHECK (case_token IS NULL OR accepted_at IS NOT NULL)
    )""",
}


class BetaConflict(ValueError):
    """Fixed refusal keys only; no customer facts or source text."""


@dataclass(frozen=True)
class BetaEntitlement:
    entitlement_id: str
    intake_id: str
    batch_id: str
    campaign_id: str
    jurisdiction_key: str
    eligible_until: datetime
    selected_at: datetime
    accepted_at: datetime | None
    case_token: str | None


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...

    def _beta_batch_entitlements(self, batch_id: str) -> tuple[BetaEntitlement, ...]: ...


def _key(value: str) -> str:
    if not isinstance(value, str) or _KEY.fullmatch(value) is None:
        raise ValueError("invalid beta identifier")
    return value


def _digest(value: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValueError("invalid beta evidence digest")
    return value


def _stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("beta timestamps must be aware")
    return value.astimezone(UTC).isoformat()


@contextmanager
def _atomic(db: sqlite3.Connection) -> Iterator[None]:
    if db.in_transaction:
        raise StorageCorruption("beta operation needs its own transaction")
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            rollback_preserving_error(db)
        raise


def initialize_beta(db: sqlite3.Connection) -> None:
    with _atomic(db):
        version = db.execute(
            "SELECT version FROM pb_store_versions WHERE component='free_beta'"
        ).fetchone()
        for table, ddl in _DDL.items():
            found = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if version is None:
                if found is not None:
                    raise StorageCorruption("unversioned beta schema")
                db.execute(ddl)
            elif (
                version != (1,)
                or found is None
                or " ".join(found[0].split()) != " ".join(ddl.split())
            ):
                raise StorageCorruption("malformed beta schema")
        if version is None:
            db.execute("INSERT INTO pb_store_versions VALUES ('free_beta', 1)")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS pb_beta_one_unreviewed_batch "
            "ON pb_beta_batches(campaign_id) WHERE reviewed_at IS NULL"
        )
        db.execute(
            "CREATE TRIGGER IF NOT EXISTS pb_beta_batch_five BEFORE INSERT ON "
            "pb_beta_entitlements WHEN (SELECT COUNT(*) FROM pb_beta_entitlements "
            "WHERE batch_id=NEW.batch_id) >= 5 "
            "BEGIN SELECT RAISE(ABORT, 'beta batch is full'); END"
        )
        for table in ("pb_beta_campaigns", "pb_beta_jurisdiction_aliases"):
            for action in ("UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                    f"BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'beta identity is fixed'); END"
                )


def _input(db: sqlite3.Connection, intake_id: str) -> tuple[str, str, str | None]:
    row = db.execute(
        "SELECT intake_json, prehold_json, agency_json, case_token FROM pb_intake_records "
        "WHERE intake_id=?",
        (intake_id,),
    ).fetchone()
    if row is None or row[2] is None:
        raise BetaConflict("beta_qualified_intake_required")
    offer = json.loads(row[1])
    if not isinstance(offer, dict) or offer.get("kind") != "offer":
        raise BetaConflict("beta_eligible_offer_required")
    issuer = offer.get("issuer")
    if not isinstance(issuer, dict) or not isinstance(issuer.get("issuer_id"), str):
        raise StorageCorruption("invalid beta issuer identity")
    try:
        agency = decode_agency(row[2])
    except (TypeError, ValueError):
        raise StorageCorruption("invalid beta agency record") from None
    if agency.id != issuer["issuer_id"]:
        raise BetaConflict("beta_issuer_identity_changed")
    digest = hashlib.sha256(
        json.dumps(list(row[:3]), ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return issuer["issuer_id"], digest, row[3]


def _no_payment_attempt(db: sqlite3.Connection, intake_id: str) -> None:
    if db.execute(
        "SELECT 1 FROM pb_intake_authorization_attempts WHERE intake_id=? "
        "AND state IN ('pending', 'uncertain', 'authorized') LIMIT 1",
        (intake_id,),
    ).fetchone():
        raise BetaConflict("beta_payment_attempt_exists")


def _entitlement(db: sqlite3.Connection, intake_id: str) -> BetaEntitlement | None:
    row = db.execute(
        "SELECT entitlement_id, intake_id, batch_id, campaign_id, jurisdiction_key, "
        "eligible_until, selected_at, accepted_at, case_token FROM pb_beta_entitlements "
        "WHERE intake_id=?",
        (intake_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        eligible, selected = datetime.fromisoformat(row[5]), datetime.fromisoformat(row[6])
        accepted = None if row[7] is None else datetime.fromisoformat(row[7])
        for value in (eligible, selected, accepted):
            if value is not None:
                _stamp(value)
        return BetaEntitlement(
            _key(row[0]),
            _key(row[1]),
            _key(row[2]),
            _key(row[3]),
            _key(row[4]),
            eligible,
            selected,
            accepted,
            None if row[8] is None else _key(row[8]),
        )
    except (TypeError, ValueError):
        raise StorageCorruption("invalid beta entitlement") from None


class BetaStoreMixin:
    def _beta_acceptance(
        self: _Connected,
        intake_id: str,
        disclosure_sha256: str,
        now: datetime,
    ) -> BetaEntitlement:
        db = self._require_open()
        if not db.in_transaction:
            raise StorageCorruption("beta acceptance requires the case transaction")
        if disclosure_sha256 != BETA_DISCLOSURE_SHA256:
            raise BetaConflict("beta_disclosure_changed")
        entitlement = _entitlement(db, intake_id)
        if entitlement is None:
            raise BetaConflict("beta_entitlement_required")
        if entitlement.case_token is not None:
            return entitlement
        if not entitlement.selected_at <= now < entitlement.eligible_until:
            raise BetaConflict("beta_qualification_expired")
        _, current, case = _input(db, intake_id)
        stored = db.execute(
            "SELECT input_sha256 FROM pb_beta_entitlements WHERE intake_id=?", (intake_id,)
        ).fetchone()
        if case is not None or stored is None or stored[0] != current:
            raise BetaConflict("beta_qualification_changed")
        _no_payment_attempt(db, intake_id)
        return entitlement

    def create_beta_campaign(
        self: _Connected,
        campaign_id: str,
        starts_at: datetime,
        ends_at: datetime,
        identity_policy_ref: str,
        configuration_ref: str,
        now: datetime,
    ) -> None:
        values = (
            _key(campaign_id),
            _stamp(starts_at),
            _stamp(ends_at),
            _key(identity_policy_ref),
            _key(configuration_ref),
            _stamp(now),
        )
        if ends_at <= starts_at:
            raise ValueError("beta first-month interval must be explicit and positive")
        db = self._require_open()
        with _atomic(db):
            existing = db.execute(
                "SELECT * FROM pb_beta_campaigns WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
            if existing is not None:
                if existing[:5] != values[:5]:
                    raise BetaConflict("beta_campaign_identity_changed")
                return
            db.execute("INSERT INTO pb_beta_campaigns VALUES (?, ?, ?, ?, ?, ?)", values)

    def bind_beta_jurisdiction(
        self: _Connected,
        campaign_id: str,
        issuer_id: str,
        jurisdiction_key: str,
        evidence_sha256: str,
        now: datetime,
    ) -> None:
        values = (
            _key(campaign_id),
            _key(issuer_id),
            _key(jurisdiction_key),
            _digest(evidence_sha256),
            _stamp(now),
        )
        db = self._require_open()
        with _atomic(db):
            existing = db.execute(
                "SELECT * FROM pb_beta_jurisdiction_aliases WHERE campaign_id=? AND issuer_id=?",
                (campaign_id, issuer_id),
            ).fetchone()
            if existing is not None:
                if existing[:4] != values[:4]:
                    raise BetaConflict("beta_jurisdiction_identity_changed")
                return
            db.execute("INSERT INTO pb_beta_jurisdiction_aliases VALUES (?, ?, ?, ?, ?)", values)

    def qualify_beta_candidate(
        self: _Connected,
        campaign_id: str,
        intake_id: str,
        qualification_sha256: str,
        eligible_until: datetime,
        now: datetime,
    ) -> None:
        _key(campaign_id)
        _key(intake_id)
        _digest(qualification_sha256)
        if eligible_until <= now:
            raise BetaConflict("beta_qualification_expired")
        db = self._require_open()
        with _atomic(db):
            issuer, digest, case = _input(db, intake_id)
            if case is not None or _entitlement(db, intake_id) is not None:
                raise BetaConflict("beta_intake_already_admitted")
            _no_payment_attempt(db, intake_id)
            alias = db.execute(
                "SELECT jurisdiction_key FROM pb_beta_jurisdiction_aliases "
                "WHERE campaign_id=? AND issuer_id=?",
                (campaign_id, issuer),
            ).fetchone()
            if alias is None:
                raise BetaConflict("beta_jurisdiction_research_required")
            db.execute(
                "INSERT INTO pb_beta_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(campaign_id, intake_id) DO UPDATE SET issuer_id=excluded.issuer_id, "
                "jurisdiction_key=excluded.jurisdiction_key, input_sha256=excluded.input_sha256, "
                "qualification_sha256=excluded.qualification_sha256, "
                "eligible_until=excluded.eligible_until, qualified_at=excluded.qualified_at",
                (
                    campaign_id,
                    intake_id,
                    issuer,
                    alias[0],
                    digest,
                    qualification_sha256,
                    _stamp(eligible_until),
                    _stamp(now),
                ),
            )

    def select_beta_batch(
        self: _Connected,
        campaign_id: str,
        batch_id: str,
        approval_ref: str,
        now: datetime,
        *,
        randbelow: Callable[[int], int] = secrets.randbelow,
    ) -> tuple[BetaEntitlement, ...]:
        _key(campaign_id)
        _key(batch_id)
        _key(approval_ref)
        stamp = _stamp(now)
        db = self._require_open()
        with _atomic(db):
            existing = db.execute(
                "SELECT campaign_id, approval_ref FROM pb_beta_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if existing is not None:
                if existing != (campaign_id, approval_ref):
                    raise BetaConflict("beta_batch_request_reused")
                return self._beta_batch_entitlements(batch_id)
            campaign = db.execute(
                "SELECT first_month_starts_at, first_month_ends_at FROM pb_beta_campaigns "
                "WHERE campaign_id=?",
                (campaign_id,),
            ).fetchone()
            if campaign is None or not (
                datetime.fromisoformat(campaign[0]) <= now < datetime.fromisoformat(campaign[1])
            ):
                raise BetaConflict("beta_campaign_window_closed")
            if db.execute(
                "SELECT 1 FROM pb_beta_batches WHERE campaign_id=? AND reviewed_at IS NULL",
                (campaign_id,),
            ).fetchone():
                raise BetaConflict("beta_batch_review_required")
            groups: dict[str, list[tuple[str, str, str, str]]] = {}
            for intake, jurisdiction, digest, qualification, until in db.execute(
                "SELECT c.intake_id, c.jurisdiction_key, c.input_sha256, "
                "c.qualification_sha256, c.eligible_until FROM pb_beta_candidates c "
                "WHERE c.campaign_id=? AND NOT EXISTS (SELECT 1 FROM pb_beta_entitlements e "
                "WHERE e.intake_id=c.intake_id OR (e.campaign_id=c.campaign_id "
                "AND e.jurisdiction_key=c.jurisdiction_key)) ORDER BY c.intake_id",
                (campaign_id,),
            ):
                if now >= datetime.fromisoformat(until):
                    continue
                try:
                    _, current, case = _input(db, intake)
                    _no_payment_attempt(db, intake)
                except BetaConflict:
                    continue
                if case is None and current == digest:
                    groups.setdefault(jurisdiction, []).append(
                        (intake, digest, qualification, until)
                    )
            chosen: list[tuple[str, str, str, str, str]] = []
            jurisdictions = sorted(groups)
            while jurisdictions and len(chosen) < MAX_BETA_BATCH:
                index = randbelow(len(jurisdictions))
                if type(index) is not int or not 0 <= index < len(jurisdictions):
                    raise ValueError("invalid beta random selection")
                jurisdiction = jurisdictions.pop(index)
                candidates = groups[jurisdiction]
                index = randbelow(len(candidates))
                if type(index) is not int or not 0 <= index < len(candidates):
                    raise ValueError("invalid beta random selection")
                chosen.append((jurisdiction, *candidates[index]))
            if not chosen:
                raise BetaConflict("beta_no_eligible_candidates")
            db.execute(
                "INSERT INTO pb_beta_batches VALUES (?, ?, ?, ?, NULL, NULL)",
                (batch_id, campaign_id, approval_ref, stamp),
            )
            for jurisdiction, intake, digest, qualification, until in chosen:
                db.execute(
                    "INSERT INTO pb_beta_entitlements VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                    (
                        uuid4().hex,
                        intake,
                        batch_id,
                        campaign_id,
                        jurisdiction,
                        digest,
                        qualification,
                        until,
                        stamp,
                    ),
                )
            return self._beta_batch_entitlements(batch_id)

    def _beta_batch_entitlements(self: _Connected, batch_id: str) -> tuple[BetaEntitlement, ...]:
        db = self._require_open()
        result = tuple(
            _entitlement(db, row[0])
            for row in db.execute(
                "SELECT intake_id FROM pb_beta_entitlements WHERE batch_id=? ORDER BY intake_id",
                (batch_id,),
            )
        )
        return tuple(item for item in result if item is not None)

    def review_beta_batch(
        self: _Connected,
        batch_id: str,
        review_ref: str,
        now: datetime,
    ) -> None:
        _key(batch_id)
        _key(review_ref)
        stamp = _stamp(now)
        db = self._require_open()
        with _atomic(db):
            row = db.execute(
                "SELECT selected_at, review_ref FROM pb_beta_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if row is None or now < datetime.fromisoformat(row[0]):
                raise BetaConflict("beta_batch_review_invalid")
            if row[1] is not None:
                if row[1] != review_ref:
                    raise BetaConflict("beta_batch_review_reused")
                return
            db.execute(
                "UPDATE pb_beta_batches SET review_ref=?, reviewed_at=? WHERE batch_id=?",
                (review_ref, stamp, batch_id),
            )

    def beta_entitlement(self: _Connected, intake_id: str) -> BetaEntitlement | None:
        return _entitlement(self._require_open(), intake_id)

    def beta_case_entitlement(self: _Connected, case_token: str) -> BetaEntitlement | None:
        db = self._require_open()
        row = db.execute(
            "SELECT intake_id FROM pb_beta_entitlements WHERE case_token=?", (case_token,)
        ).fetchone()
        return None if row is None else _entitlement(db, row[0])
