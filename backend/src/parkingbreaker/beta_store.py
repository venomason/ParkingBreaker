"""Durable free-beta admission controls on the existing transaction database.

These methods neither activate providers nor start cases. A trusted qualification seam
supplies an evidence-bound canonical issuing jurisdiction. The First-Class pilot binds
that qualification to one enrolled mail route and limits selections across its batches.
Existing admitted work is independent of admission/review state.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import uuid4

from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.mail_config import MailClass
from parkingbreaker.research_records import decode_agency
from parkingbreaker.route_registry import RouteRevision, RouteScope
from parkingbreaker.service_phase import ServicePhase
from parkingbreaker.service_policy import intake_customer_stop

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
FIRST_CLASS_PILOT_DURATION = timedelta(days=14)
FIRST_CLASS_PILOT_CAPACITY = 5
FIRST_CLASS_PILOT_DISCLOSURE_VERSION = 2
FIRST_CLASS_PILOT_DISCLOSURE_TEXT = (
    "Your selected pilot appeal is free. It includes one mailing by ordinary USPS "
    "First-Class Mail. Tracking may be limited, and a delivery scan may be unavailable. "
    "Certified Mail receipts and recipient signatures are unavailable. ParkingBreaker is "
    "still in development, and errors may occur. We will ask you to confirm your facts "
    "and the exact packet before mailing. Pilot participation does not extend any ticket "
    "deadline. If the mailing provider's answer is unclear, we will reconcile that same "
    "attempt. We will not automatically start a second appeal or re-mail. We will ask "
    "what happened to your filing. You may receive two feedback emails, around days 14 "
    "and 30 after documented mailing. You can report no response or skip follow-up. "
    "Each email includes a link to stop reminders. There is no later charge."
)
FIRST_CLASS_PILOT_DISCLOSURE_SHA256 = hashlib.sha256(
    FIRST_CLASS_PILOT_DISCLOSURE_TEXT.encode()
).hexdigest()
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}")
_HASH = re.compile(r"[0-9a-f]{64}")
_V1_DDL = {
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
_PILOT_COLUMNS = (
    "pilot_issuer_id TEXT",
    "pilot_stage TEXT CHECK ((pilot_issuer_id IS NULL) = (pilot_stage IS NULL))",
)
_DDL = {
    **_V1_DDL,
    "pb_beta_campaigns": (
        _V1_DDL["pb_beta_campaigns"].removesuffix(")") + ", " + ", ".join(_PILOT_COLUMNS) + ")"
    ),
    "pb_beta_entitlements": _V1_DDL["pb_beta_entitlements"].replace(
        "        UNIQUE (batch_id, jurisdiction_key), UNIQUE (campaign_id, jurisdiction_key),\n",
        "",
    ),
}
_PILOT_SCHEMA = {
    "pb_beta_entitlements_campaign": (
        "CREATE INDEX pb_beta_entitlements_campaign ON "
        "pb_beta_entitlements(campaign_id, jurisdiction_key)"
    ),
    "pb_beta_entitlements_batch": (
        "CREATE INDEX pb_beta_entitlements_batch ON "
        "pb_beta_entitlements(batch_id, jurisdiction_key)"
    ),
    "pb_beta_pilot_singleton": """CREATE TRIGGER pb_beta_pilot_singleton BEFORE INSERT ON
        pb_beta_campaigns WHEN EXISTS (SELECT 1 FROM pb_beta_campaigns WHERE
        campaign_id=NEW.campaign_id OR (pilot_issuer_id IS NOT NULL
        AND NEW.pilot_issuer_id IS NOT NULL))
        BEGIN SELECT RAISE(ABORT, 'beta campaign identity is fixed'); END""",
    "pb_beta_entitlement_insert": f"""CREATE TRIGGER pb_beta_entitlement_insert BEFORE INSERT ON
        pb_beta_entitlements BEGIN
        SELECT RAISE(ABORT, 'beta entitlement identity is fixed') WHERE EXISTS (
            SELECT 1 FROM pb_beta_entitlements WHERE intake_id=NEW.intake_id
            OR entitlement_id=NEW.entitlement_id OR case_token=NEW.case_token);
        SELECT RAISE(ABORT, 'beta batch campaign mismatch') WHERE NOT EXISTS (
            SELECT 1 FROM pb_beta_batches WHERE batch_id=NEW.batch_id
            AND campaign_id=NEW.campaign_id);
        SELECT RAISE(ABORT, 'beta jurisdiction already selected') WHERE EXISTS (
            SELECT 1 FROM pb_beta_campaigns WHERE campaign_id=NEW.campaign_id
            AND pilot_issuer_id IS NULL) AND EXISTS (
            SELECT 1 FROM pb_beta_entitlements WHERE jurisdiction_key=NEW.jurisdiction_key
            AND (campaign_id=NEW.campaign_id OR batch_id=NEW.batch_id));
        SELECT RAISE(ABORT, 'first-class pilot issuer mismatch') WHERE EXISTS (
            SELECT 1 FROM pb_beta_campaigns WHERE campaign_id=NEW.campaign_id
            AND pilot_issuer_id<>NEW.jurisdiction_key);
        SELECT RAISE(ABORT, 'first-class pilot is full') WHERE EXISTS (
            SELECT 1 FROM pb_beta_campaigns WHERE campaign_id=NEW.campaign_id
            AND pilot_issuer_id IS NOT NULL) AND (SELECT COUNT(*) FROM pb_beta_entitlements
            WHERE campaign_id=NEW.campaign_id) >= {FIRST_CLASS_PILOT_CAPACITY};
        END""",
    "pb_beta_entitlement_identity": """CREATE TRIGGER pb_beta_entitlement_identity BEFORE UPDATE OF
        entitlement_id, intake_id, batch_id, campaign_id, jurisdiction_key, input_sha256,
        qualification_sha256, eligible_until, selected_at ON pb_beta_entitlements
        BEGIN SELECT RAISE(ABORT, 'beta entitlement identity is fixed'); END""",
    "pb_beta_pilot_consume": """CREATE TRIGGER pb_beta_pilot_consume BEFORE DELETE ON
        pb_beta_entitlements WHEN EXISTS (SELECT 1 FROM pb_beta_campaigns WHERE
        campaign_id=OLD.campaign_id AND pilot_issuer_id IS NOT NULL)
        BEGIN SELECT RAISE(ABORT, 'first-class pilot selections are final'); END""",
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
    pilot_scope: RouteScope | None = None

    @property
    def tier(self) -> Literal["CERTIFIED", "STANDARD"]:
        return "STANDARD" if self.pilot_scope is not None else "CERTIFIED"

    @property
    def disclosure_version(self) -> int:
        return (
            FIRST_CLASS_PILOT_DISCLOSURE_VERSION
            if self.pilot_scope is not None
            else BETA_DISCLOSURE_VERSION
        )

    @property
    def disclosure_text(self) -> str:
        return (
            FIRST_CLASS_PILOT_DISCLOSURE_TEXT
            if self.pilot_scope is not None
            else BETA_DISCLOSURE_TEXT
        )

    @property
    def disclosure_sha256(self) -> str:
        return (
            FIRST_CLASS_PILOT_DISCLOSURE_SHA256
            if self.pilot_scope is not None
            else BETA_DISCLOSURE_SHA256
        )


@dataclass(frozen=True)
class FirstClassPilot:
    campaign_id: str
    scope: RouteScope
    starts_at: datetime
    ends_at: datetime
    identity_policy_ref: str
    configuration_ref: str
    evidence_sha256: str
    selected_count: int


@dataclass(frozen=True)
class _BetaInput:
    issuer_id: str
    stage: str | None
    digest: str
    case_token: str | None
    intake_json: str
    owner: str


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...

    def _beta_batch_entitlements(self, batch_id: str) -> tuple[BetaEntitlement, ...]: ...

    def current_route(self, scope: RouteScope) -> RouteRevision | None: ...

    def route_check_current(self, route: RouteRevision, now: datetime) -> bool: ...

    def intake_paused(self) -> bool: ...

    def effective_service_phase(self, fallback: ServicePhase) -> ServicePhase: ...


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


def _draw(size: int, randbelow: Callable[[int], int]) -> int:
    index = randbelow(size)
    if type(index) is not int or not 0 <= index < size:
        raise ValueError("invalid beta random selection")
    return index


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


def _check_tables(db: sqlite3.Connection, tables: dict[str, str], *, install: bool = False) -> None:
    for table, ddl in tables.items():
        found = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if install:
            if found is not None:
                raise StorageCorruption("unversioned beta schema")
            db.execute(ddl)
        elif found is None or " ".join(found[0].split()) != " ".join(ddl.split()):
            raise StorageCorruption("malformed beta schema")


def _migrate_pilot(db: sqlite3.Connection) -> None:
    _check_tables(db, _V1_DDL)
    for column in _PILOT_COLUMNS:
        db.execute(f"ALTER TABLE pb_beta_campaigns ADD COLUMN {column}")
    db.execute("ALTER TABLE pb_beta_entitlements RENAME TO pb_beta_entitlements_v1")
    db.execute(_DDL["pb_beta_entitlements"])
    db.execute("INSERT INTO pb_beta_entitlements SELECT * FROM pb_beta_entitlements_v1")
    db.execute("DROP TABLE pb_beta_entitlements_v1")
    db.execute("UPDATE pb_store_versions SET version=2 WHERE component='free_beta'")


def initialize_beta(db: sqlite3.Connection) -> None:
    with _atomic(db):
        version = db.execute(
            "SELECT version FROM pb_store_versions WHERE component='free_beta'"
        ).fetchone()
        if version is None:
            _check_tables(db, _DDL, install=True)
            db.execute("INSERT INTO pb_store_versions VALUES ('free_beta', 2)")
        elif version == (1,):
            _migrate_pilot(db)
            _check_tables(db, _DDL)
        elif version == (2,):
            _check_tables(db, _DDL)
        else:
            raise StorageCorruption("unsupported beta schema")
        for name, ddl in _PILOT_SCHEMA.items():
            found = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
            if version != (2,):
                if found is not None:
                    raise StorageCorruption("unversioned first-class pilot schema")
                db.execute(ddl)
            elif found != (ddl,):
                raise StorageCorruption("malformed first-class pilot schema")
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


def _input(db: sqlite3.Connection, intake_id: str) -> _BetaInput:
    row = db.execute(
        "SELECT r.intake_json, r.prehold_json, r.agency_json, r.case_token, o.owner "
        "FROM pb_intake_records r JOIN pb_intake_owners o USING (workspace_token) "
        "WHERE r.intake_id=?",
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
    stage = issuer.get("stage")
    return _BetaInput(
        issuer["issuer_id"],
        stage if isinstance(stage, str) else None,
        digest,
        row[3],
        row[0],
        row[4],
    )


def _verify_pilot_route(store: _Connected, scope: RouteScope, now: datetime) -> RouteRevision:
    if store.intake_paused():
        raise BetaConflict("intake_paused")
    route = store.current_route(scope)
    if route is None or not store.route_check_current(route, now):
        raise BetaConflict("first_class_pilot_route_unavailable")
    return route


def _citation_key(citation_number: str) -> str:
    return "".join(
        char
        for char in citation_number.casefold()
        if not char.isspace() and unicodedata.category(char) != "Pd"
    )


def _check_pilot_ticket(intake_json: str, now: datetime) -> str:
    from parkingbreaker.api.schemas import SubmitIntake

    try:
        intake = SubmitIntake.model_validate_json(intake_json)
    except ValueError:
        raise StorageCorruption("invalid pilot intake") from None
    ticket = intake.ticket
    citation_key = _citation_key(ticket.citation_number)
    if (
        not citation_key
        or ticket.issuer_kind != "GOVERNMENT"
        or (ticket.printed_deadline is not None and ticket.printed_deadline < now.date())
        or intake_customer_stop(
            ticket.issue_date,
            now.date(),
            intake.rental_company_paid,
            mail_class=MailClass.FIRST_CLASS,
        )
        is not None
    ):
        raise BetaConflict("first_class_pilot_intake_ineligible")
    return citation_key


def _duplicate_pilot_selection(
    db: sqlite3.Connection, campaign_id: str, owner: str, citation_key: str
) -> bool:
    for selected_owner, citation in db.execute(
        "SELECT o.owner, json_extract(r.intake_json, '$.ticket.citation_number') "
        "FROM pb_beta_entitlements e JOIN pb_intake_records r USING (intake_id) "
        "JOIN pb_intake_owners o USING (workspace_token) WHERE e.campaign_id=?",
        (campaign_id,),
    ):
        if not isinstance(citation, str):
            raise StorageCorruption("invalid selected pilot citation")
        if selected_owner == owner or _citation_key(citation) == citation_key:
            return True
    return False


def _check_pilot_scope(scope: RouteScope, issuer: str, stage: str | None) -> None:
    if stage is None:
        raise BetaConflict("first_class_pilot_scope_mismatch")
    try:
        intake_scope = RouteScope(issuer, stage, MailClass.FIRST_CLASS)
    except ValueError:
        raise BetaConflict("first_class_pilot_scope_mismatch") from None
    if intake_scope != scope:
        raise BetaConflict("first_class_pilot_scope_mismatch")


def _first_class_pilot(
    db: sqlite3.Connection, campaign_id: str | None = None
) -> FirstClassPilot | None:
    row = db.execute(
        "SELECT c.campaign_id, c.pilot_issuer_id, c.pilot_stage, c.first_month_starts_at, "
        "c.first_month_ends_at, c.identity_policy_ref, c.configuration_ref, a.evidence_sha256, "
        "(SELECT COUNT(*) FROM pb_beta_entitlements e WHERE e.campaign_id=c.campaign_id) "
        "FROM pb_beta_campaigns c LEFT JOIN pb_beta_jurisdiction_aliases a "
        "ON a.campaign_id=c.campaign_id AND a.issuer_id=c.pilot_issuer_id "
        "WHERE c.pilot_issuer_id IS NOT NULL AND (? IS NULL OR c.campaign_id=?)",
        (campaign_id, campaign_id),
    ).fetchone()
    if row is None:
        return None
    try:
        starts, ends = datetime.fromisoformat(row[3]), datetime.fromisoformat(row[4])
        if (
            _stamp(starts) != row[3]
            or _stamp(ends) != row[4]
            or ends - starts != FIRST_CLASS_PILOT_DURATION
            or not 0 <= row[8] <= FIRST_CLASS_PILOT_CAPACITY
        ):
            raise ValueError
        return FirstClassPilot(
            _key(row[0]),
            RouteScope(_key(row[1]), row[2], MailClass.FIRST_CLASS),
            starts,
            ends,
            _key(row[5]),
            _key(row[6]),
            _digest(row[7]),
            row[8],
        )
    except (TypeError, ValueError):
        raise StorageCorruption("invalid first-class pilot") from None


def _no_payment_attempt(
    db: sqlite3.Connection, intake_id: str, *, any_previous: bool = False
) -> None:
    if db.execute(
        "SELECT 1 FROM pb_intake_authorization_attempts WHERE intake_id=? "
        "AND (? OR state IN ('pending', 'uncertain', 'authorized')) LIMIT 1",
        (intake_id, any_previous),
    ).fetchone():
        raise BetaConflict("beta_payment_attempt_exists")


def _entitlement(db: sqlite3.Connection, intake_id: str) -> BetaEntitlement | None:
    row = db.execute(
        "SELECT e.entitlement_id, e.intake_id, e.batch_id, e.campaign_id, e.jurisdiction_key, "
        "e.eligible_until, e.selected_at, e.accepted_at, e.case_token, c.pilot_issuer_id, "
        "c.pilot_stage FROM pb_beta_entitlements e JOIN pb_beta_campaigns c USING (campaign_id) "
        "WHERE e.intake_id=?",
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
            None if row[9] is None else RouteScope(_key(row[9]), row[10], MailClass.FIRST_CLASS),
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
        entitlement = _entitlement(db, intake_id)
        if entitlement is None:
            raise BetaConflict("beta_entitlement_required")
        if disclosure_sha256 != entitlement.disclosure_sha256:
            raise BetaConflict("beta_disclosure_changed")
        if entitlement.case_token is not None:
            return entitlement
        if not entitlement.selected_at <= now < entitlement.eligible_until:
            raise BetaConflict("beta_qualification_expired")
        snapshot = _input(db, intake_id)
        stored = db.execute(
            "SELECT input_sha256 FROM pb_beta_entitlements WHERE intake_id=?", (intake_id,)
        ).fetchone()
        if snapshot.case_token is not None or stored is None or stored[0] != snapshot.digest:
            raise BetaConflict("beta_qualification_changed")
        _no_payment_attempt(db, intake_id, any_previous=entitlement.pilot_scope is not None)
        if entitlement.pilot_scope is not None:
            _check_pilot_ticket(snapshot.intake_json, now)
            _check_pilot_scope(entitlement.pilot_scope, snapshot.issuer_id, snapshot.stage)
            _verify_pilot_route(self, entitlement.pilot_scope, now)
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
                if existing[:5] != values[:5] or existing[6] is not None:
                    raise BetaConflict("beta_campaign_identity_changed")
                return
            db.execute(
                "INSERT INTO pb_beta_campaigns VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)", values
            )

    def first_class_pilot_status(self: _Connected) -> FirstClassPilot | None:
        return _first_class_pilot(self._require_open())

    def open_first_class_pilot(
        self: _Connected,
        campaign_id: str,
        scope: RouteScope,
        identity_policy_ref: str,
        configuration_ref: str,
        evidence_sha256: str,
        now: datetime,
    ) -> FirstClassPilot:
        """Open the one 14-day pilot from exact evidence for its enrolled route."""
        _key(campaign_id)
        _key(scope.issuer)
        _key(identity_policy_ref)
        _key(configuration_ref)
        _digest(evidence_sha256)
        if scope.mail_class != MailClass.FIRST_CLASS:
            raise BetaConflict("first_class_pilot_scope_required")
        stamp = _stamp(now)
        ends = now.astimezone(UTC) + FIRST_CLASS_PILOT_DURATION
        db = self._require_open()
        with _atomic(db):
            existing = _first_class_pilot(db)
            if existing is not None:
                if (
                    existing.campaign_id != campaign_id
                    or existing.scope != scope
                    or existing.identity_policy_ref != identity_policy_ref
                    or existing.configuration_ref != configuration_ref
                    or existing.evidence_sha256 != evidence_sha256
                ):
                    raise BetaConflict("first_class_pilot_identity_changed")
                return existing
            if db.execute(
                "SELECT 1 FROM pb_beta_campaigns WHERE campaign_id=?", (campaign_id,)
            ).fetchone():
                raise BetaConflict("beta_campaign_identity_changed")
            route = _verify_pilot_route(self, scope, now)
            if route.evidence_sha256 != evidence_sha256:
                raise BetaConflict("first_class_pilot_route_evidence_changed")
            db.execute(
                "INSERT INTO pb_beta_campaigns VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    campaign_id,
                    stamp,
                    _stamp(ends),
                    identity_policy_ref,
                    configuration_ref,
                    stamp,
                    scope.issuer,
                    scope.stage,
                ),
            )
            db.execute(
                "INSERT INTO pb_beta_jurisdiction_aliases VALUES (?, ?, ?, ?, ?)",
                (campaign_id, scope.issuer, scope.issuer, evidence_sha256, stamp),
            )
            return FirstClassPilot(
                campaign_id,
                scope,
                now.astimezone(UTC),
                ends,
                identity_policy_ref,
                configuration_ref,
                evidence_sha256,
                0,
            )

    def offer_first_class_pilot(
        self: _Connected,
        intake_id: str,
        now: datetime,
        *,
        configured_phase: ServicePhase = ServicePhase.CONTROLLED_FREE_BETA,
    ) -> BetaEntitlement | None:
        """Apply the approved pilot policy to a saved intake, without starting a case."""
        _key(intake_id)
        stamp = _stamp(now)
        db = self._require_open()
        with _atomic(db):
            enrolled = _entitlement(db, intake_id)
            if enrolled is not None:
                return enrolled if enrolled.pilot_scope is not None else None
            if self.effective_service_phase(configured_phase) != ServicePhase.CONTROLLED_FREE_BETA:
                return None
            pilot = _first_class_pilot(db)
            if (
                pilot is None
                or not pilot.starts_at <= now < pilot.ends_at
                or pilot.selected_count >= FIRST_CLASS_PILOT_CAPACITY
            ):
                return None
            try:
                snapshot = _input(db, intake_id)
                if snapshot.case_token is not None:
                    return None
                _no_payment_attempt(db, intake_id, any_previous=True)
                issuer = snapshot.issuer_id
                _check_pilot_scope(pilot.scope, issuer, snapshot.stage)
                citation = _check_pilot_ticket(snapshot.intake_json, now)
                route = _verify_pilot_route(self, pilot.scope, now)
            except BetaConflict:
                return None
            if _duplicate_pilot_selection(db, pilot.campaign_id, snapshot.owner, citation):
                return None
            qualification = hashlib.sha256(
                json.dumps(
                    [
                        pilot.campaign_id,
                        pilot.identity_policy_ref,
                        pilot.configuration_ref,
                        intake_id,
                        snapshot.digest,
                        route.revision,
                        route.evidence_sha256,
                    ],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            until = _stamp(pilot.ends_at)
            db.execute(
                "INSERT INTO pb_beta_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pilot.campaign_id,
                    intake_id,
                    issuer,
                    issuer,
                    snapshot.digest,
                    qualification,
                    until,
                    stamp,
                ),
            )
            batch = f"pilot-{uuid4().hex}"
            # Pilot selection applies its stored enrollment approval to each eligible intake.
            db.execute(
                "INSERT INTO pb_beta_batches VALUES (?, ?, ?, ?, ?, ?)",
                (
                    batch,
                    pilot.campaign_id,
                    f"pilot-approval/{qualification}",
                    stamp,
                    f"pilot-policy-check/{qualification}",
                    stamp,
                ),
            )
            db.execute(
                "INSERT INTO pb_beta_entitlements VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                (
                    uuid4().hex,
                    intake_id,
                    batch,
                    pilot.campaign_id,
                    issuer,
                    snapshot.digest,
                    qualification,
                    until,
                    stamp,
                ),
            )
            return _entitlement(db, intake_id)

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
            if _first_class_pilot(db, campaign_id) is not None:
                raise BetaConflict("first_class_pilot_scope_mismatch")
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
        stamp = _stamp(now)
        _stamp(eligible_until)
        if eligible_until <= now:
            raise BetaConflict("beta_qualification_expired")
        db = self._require_open()
        with _atomic(db):
            if _first_class_pilot(db, campaign_id) is not None:
                raise BetaConflict("first_class_pilot_uses_automatic_qualification")
            snapshot = _input(db, intake_id)
            if snapshot.case_token is not None or _entitlement(db, intake_id) is not None:
                raise BetaConflict("beta_intake_already_admitted")
            _no_payment_attempt(db, intake_id)
            alias = db.execute(
                "SELECT jurisdiction_key FROM pb_beta_jurisdiction_aliases "
                "WHERE campaign_id=? AND issuer_id=?",
                (campaign_id, snapshot.issuer_id),
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
                    snapshot.issuer_id,
                    alias[0],
                    snapshot.digest,
                    qualification_sha256,
                    _stamp(eligible_until),
                    stamp,
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
            if _first_class_pilot(db, campaign_id) is not None:
                raise BetaConflict("first_class_pilot_uses_automatic_selection")
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
                    snapshot = _input(db, intake)
                    _no_payment_attempt(db, intake)
                except BetaConflict:
                    continue
                if snapshot.case_token is None and snapshot.digest == digest:
                    groups.setdefault(jurisdiction, []).append(
                        (intake, digest, qualification, until)
                    )
            chosen: list[tuple[str, str, str, str, str]] = []
            jurisdictions = sorted(groups)
            while jurisdictions and len(chosen) < MAX_BETA_BATCH:
                jurisdiction = jurisdictions.pop(_draw(len(jurisdictions), randbelow))
                candidates = groups[jurisdiction]
                chosen.append((jurisdiction, *candidates[_draw(len(candidates), randbelow)]))
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
