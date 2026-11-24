"""Append-only address instructions on the transaction store's SQLite connection.

``case_id`` is the stable API intake id, before and after authorization, not the
later transaction case token. Every operation requires that intake's actual owner.
The three tables are their own histories: latest means the greatest revision, so
there is no mutable head that could diverge from its history after a crash.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Protocol, cast

from parkingbreaker.address_policy import (
    AddressProvenance,
    ConsentEvent,
    DestinationDecision,
    Disclosure,
    EvidenceKind,
    EvidenceRef,
    address_fingerprint,
    mailing_gate,
)
from parkingbreaker.agencies import MailingAddress
from parkingbreaker.case_state import CaseState
from parkingbreaker.intake_store import IntakeConflict, IntakeRecord, SavedResearch
from parkingbreaker.lob_submission import LetterRequest
from parkingbreaker.mail_attempt_store import StorageCorruption, rollback_preserving_error
from parkingbreaker.research.questions import Ticket
from parkingbreaker.research_records import decode_agency, decode_research
from parkingbreaker.route_registry import RouteRevision, RouteScope
from parkingbreaker.transaction_records import _reject_constant, _unique_object

_TABLES = ("pb_address_revisions", "pb_address_consents", "pb_address_decisions")
_DDL = tuple(
    f"""CREATE TABLE {table} (
        case_id TEXT NOT NULL REFERENCES pb_intake_records(intake_id),
        owner TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        payload_json TEXT NOT NULL,
        PRIMARY KEY (case_id, owner, revision)
    )"""
    for table in _TABLES
)
_LOCK_DDL = """CREATE TABLE pb_address_locks (
    case_id TEXT PRIMARY KEY REFERENCES pb_intake_records(intake_id),
    owner TEXT NOT NULL,
    locked_at TEXT NOT NULL
)"""
_CHOICE_STATES = frozenset({CaseState.RESEARCHING, CaseState.NEEDS_CUSTOMER, CaseState.DRAFTING})


@dataclass(frozen=True)
class CustomerAddressRevision:
    case_id: str
    owner: str
    address: MailingAddress
    revision: int
    created_at: datetime


@dataclass(frozen=True)
class SavedDestinationDecision:
    decision: DestinationDecision
    revision: int


class _Connected(Protocol):
    def _require_open(self) -> sqlite3.Connection: ...


class _RouteBindingStore(Protocol):
    def get_research(self, intake_id: str) -> SavedResearch | None: ...
    def current_route(self, scope: RouteScope) -> RouteRevision | None: ...
    def bind_route(
        self, case_token: str, attempt: int, scope: RouteScope, revision: int
    ) -> None: ...


@contextmanager
def _transaction(database: sqlite3.Connection) -> Iterator[None]:
    try:
        database.execute("BEGIN IMMEDIATE")
        yield
        database.execute("COMMIT")
    except Exception:
        if database.in_transaction:
            rollback_preserving_error(database)
        raise


def initialize_address_store(database: sqlite3.Connection) -> None:
    """Migrate address v1 to v2 with an empty durable lock table; never backfill cases.

    Components have independent version rows. Absence of the address row is the
    previous schema; existing intake and transaction versions remain unchanged.
    """
    with _transaction(database):
        version = database.execute(
            "SELECT version FROM pb_store_versions WHERE component='address'"
        ).fetchone()
        tables = {
            row[0]: " ".join(row[1].upper().split())
            for row in database.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
            if row[0].startswith("pb_address_")
        }
        expected = {
            table: " ".join(ddl.upper().split()) for table, ddl in zip(_TABLES, _DDL, strict=True)
        }
        if version is None:
            if tables:
                raise StorageCorruption("unversioned address schema")
            for ddl in _DDL:
                database.execute(ddl)
            database.execute(_LOCK_DDL)
            database.execute("INSERT INTO pb_store_versions VALUES ('address', 2)")
        elif type(version[0]) is not int or version[0] not in {1, 2}:
            raise StorageCorruption("unsupported address schema")
        else:
            if version == (2,):
                expected["pb_address_locks"] = " ".join(_LOCK_DDL.upper().split())
            if tables != expected:
                raise StorageCorruption("malformed address schema")
            if version == (1,):
                database.execute(_LOCK_DDL)
                database.execute("UPDATE pb_store_versions SET version=2 WHERE component='address'")


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected non-blank text")
    return value


def _revision(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("expected positive revision")
    return value


def _aware(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


def _time(value: object) -> datetime:
    return _aware(datetime.fromisoformat(_text(value)))


def _json_default(value: object) -> str:
    return _aware(value).isoformat()


def _json(value: object) -> str:
    # Same compact, sorted-key, lossless Unicode convention as workspace_json.
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_default,
    )


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("unexpected record fields")
    return cast(dict[str, object], value)


def _keys(
    record: type[MailingAddress | ConsentEvent | DestinationDecision | Disclosure],
) -> set[str]:
    return {field.name for field in fields(record)}


def _optional_text(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("expected optional text")
    return value


def _address(value: object) -> MailingAddress:
    parts = _object(value, _keys(MailingAddress))
    # Store exact strings, including empty fields, whitespace, and None vs "".
    # Postal completeness and provenance are the pure policy's responsibility.
    for key, part in parts.items():
        if not isinstance(part, str) and not (key in {"attention", "line2"} and part is None):
            raise ValueError("invalid address field")
    return MailingAddress(**parts)  # type: ignore[arg-type]


def _consent(value: object) -> ConsentEvent:
    parts = _object(value, _keys(ConsentEvent))
    if parts["accepted"] is not True:
        raise ValueError("consent must be an explicit acceptance")
    return ConsentEvent(
        case_id=_text(parts["case_id"]),
        owner=_text(parts["owner"]),
        address_fingerprint=_text(parts["address_fingerprint"]),
        disclosure_sha256=_text(parts["disclosure_sha256"]),
        address_revision=_revision(parts["address_revision"]),
        accepted=True,
        accepted_at=_time(parts["accepted_at"]),
    )


def _decision(value: object) -> DestinationDecision:
    parts = _object(value, _keys(DestinationDecision))
    evidence = parts["verification_evidence"]
    if not isinstance(evidence, list):
        raise ValueError("expected evidence list")
    refs: list[EvidenceRef] = []
    for item in evidence:
        ref = _object(item, {"evidence_id", "sha256", "kind"})
        refs.append(
            EvidenceRef(
                _text(ref["evidence_id"]), _text(ref["sha256"]), EvidenceKind(_text(ref["kind"]))
            )
        )
    disclosure = parts["disclosure"]
    if disclosure is not None:
        disclosure = _object(disclosure, _keys(Disclosure))
        disclosure = Disclosure(_text(disclosure["version"]), _text(disclosure["sha256"]))
    return DestinationDecision(
        case_id=_text(parts["case_id"]),
        owner=_text(parts["owner"]),
        address=_address(parts["address"]),
        fingerprint=_text(parts["fingerprint"]),
        provenance=AddressProvenance(_text(parts["provenance"])),
        refund_policy_version=_text(parts["refund_policy_version"]),
        decided_at=_time(parts["decided_at"]),
        verification_evidence=tuple(refs),
        failed_verification_code=_optional_text(parts["failed_verification_code"]),
        customer_address_revision=(
            None
            if parts["customer_address_revision"] is None
            else _revision(parts["customer_address_revision"])
        ),
        disclosure=disclosure,
        consent=None if parts["consent"] is None else _consent(parts["consent"]),
    )


def _load(text: object) -> object:
    if not isinstance(text, str):
        raise ValueError("expected JSON text")
    value: object = json.loads(
        text, object_pairs_hook=_unique_object, parse_constant=_reject_constant
    )
    if _json(value) != text:
        raise ValueError("noncanonical record JSON")
    return value


def _require_scope(database: sqlite3.Connection, case_id: str, owner: str) -> None:
    _text(case_id)
    _text(owner)
    row = database.execute(
        "SELECT o.owner FROM pb_intake_records r "
        "JOIN pb_intake_owners o ON o.workspace_token=r.workspace_token WHERE r.intake_id=?",
        (case_id,),
    ).fetchone()
    if row is None or row[0] != owner:
        raise IntakeConflict("address_scope_mismatch")


def _rows(
    database: sqlite3.Connection, table: str, case_id: str, owner: str
) -> Iterator[tuple[int, object]]:
    # Table names are module constants only. Query every owner to detect corrupt
    # scope columns rather than silently hiding records from the actual owner.
    rows = database.execute(
        f"SELECT owner, revision, payload_json FROM {table} WHERE case_id=? ORDER BY revision",
        (case_id,),
    ).fetchall()
    for expected, (stored_owner, revision, text) in enumerate(rows, 1):
        if stored_owner != owner or _revision(revision) != expected:
            raise ValueError("invalid history scope or sequence")
        yield revision, _load(text)


def _case_state(database: sqlite3.Connection, case_id: str) -> str | None:
    row = database.execute(
        "SELECT c.letter_state FROM pb_intake_records r "
        "LEFT JOIN pb_cases c ON c.case_token=r.case_token WHERE r.intake_id=?",
        (case_id,),
    ).fetchone()
    return None if row is None else cast(str | None, row[0])


def _destination_locked(database: sqlite3.Connection, case_id: str) -> bool:
    # Called only inside the writer's BEGIN IMMEDIATE, shared with the draft lock.
    # Existing drafts/states also protect legacy cases without fabricated lock rows.
    state = _case_state(database, case_id)
    return bool(
        database.execute("SELECT 1 FROM pb_address_locks WHERE case_id=?", (case_id,)).fetchone()
        or database.execute(
            "SELECT 1 FROM pb_intake_drafts WHERE intake_id=?", (case_id,)
        ).fetchone()
        or (state is not None and state not in _CHOICE_STATES)
    )


def _remail_correction_open(
    database: sqlite3.Connection,
    case_id: str,
    history: tuple[SavedDestinationDecision, ...],
    decision: DestinationDecision,
) -> bool:
    """Whether a post-lock SYSTEM_VERIFIED supersession is the free re-mail's correction.

    Allowed only while the returned letter's re-mail has not been sent: the case is RETURNED,
    or it is the re-mail attempt (SUBMITTING, attempt 2) with no attempt-2 submission cursor.
    The corrected address must differ from every earlier decision, including the one mailed.
    """
    row = database.execute(
        "SELECT c.letter_state, c.attempt, c.case_token FROM pb_intake_records r "
        "JOIN pb_cases c ON c.case_token=r.case_token WHERE r.intake_id=?",
        (case_id,),
    ).fetchone()
    if row is None or any(decision.fingerprint == h.decision.fingerprint for h in history):
        return False
    state, attempt, token = row
    if state == CaseState.RETURNED:
        return True
    if state != CaseState.SUBMITTING or attempt != 2:
        return False
    return (
        database.execute(
            "SELECT 1 FROM pb_submission_cursors WHERE case_token=? AND attempt=2", (token,)
        ).fetchone()
        is None
    )


def _require_destination_writable(database: sqlite3.Connection, case_id: str) -> None:
    if _destination_locked(database, case_id):
        raise IntakeConflict("destination_locked_after_draft")


def _addresses(
    database: sqlite3.Connection, case_id: str, owner: str
) -> tuple[CustomerAddressRevision, ...]:
    try:
        result = []
        for revision, payload in _rows(database, _TABLES[0], case_id, owner):
            parts = _object(payload, {"address", "created_at"})
            result.append(
                CustomerAddressRevision(
                    case_id, owner, _address(parts["address"]), revision, _time(parts["created_at"])
                )
            )
        return tuple(result)
    except (TypeError, ValueError, OverflowError) as exc:
        raise StorageCorruption("malformed customer address history") from exc


def _bound_address(consent: ConsentEvent, addresses: tuple[CustomerAddressRevision, ...]) -> bool:
    return any(
        consent.address_revision == address.revision
        and consent.address_fingerprint == address_fingerprint(address.address)
        and consent.accepted_at >= address.created_at
        for address in addresses
    )


def _consents(database: sqlite3.Connection, case_id: str, owner: str) -> tuple[ConsentEvent, ...]:
    addresses = _addresses(database, case_id, owner)
    try:
        result = []
        for _, payload in _rows(database, _TABLES[1], case_id, owner):
            event = _consent(payload)
            if (
                event.case_id != case_id
                or event.owner != owner
                or not _bound_address(event, addresses)
            ):
                raise ValueError("consent scope or address mismatch")
            result.append(event)
        return tuple(result)
    except (TypeError, ValueError, OverflowError) as exc:
        raise StorageCorruption("malformed address consent history") from exc


def _decisions(
    database: sqlite3.Connection, case_id: str, owner: str
) -> tuple[SavedDestinationDecision, ...]:
    consents = _consents(database, case_id, owner)
    try:
        result = []
        for revision, payload in _rows(database, _TABLES[2], case_id, owner):
            decision = _decision(payload)
            if decision.case_id != case_id or decision.owner != owner:
                raise ValueError("decision scope mismatch")
            if decision.consent is not None and decision.consent not in consents:
                raise ValueError("decision consent not recorded")
            result.append(SavedDestinationDecision(decision, revision))
        return tuple(result)
    except (TypeError, ValueError, OverflowError) as exc:
        raise StorageCorruption("malformed destination decision history") from exc


def _next_revision(current: int | None, expected: int | None) -> int:
    if expected is not None:
        _revision(expected)
    if current != expected:
        raise IntakeConflict("address_revision_stale")
    return 1 if current is None else current + 1


def research_route_scope(ticket: Ticket, request: LetterRequest) -> RouteScope:
    return RouteScope(
        ticket.jurisdiction or ticket.city_id,
        ticket.stage or "first-level review",
        request.options.mail_class.value,
    )


def prepared_route_scope(
    intake: IntakeRecord, request: LetterRequest, research_ticket: Ticket | None
) -> RouteScope | None:
    """Use the case's resolved issuer and saved ticket stage independently of research."""
    if intake.agency_json is None:
        return None if research_ticket is None else research_route_scope(research_ticket, request)
    issuer = decode_agency(intake.agency_json).id
    payload: object = json.loads(
        intake.intake_json, object_pairs_hook=_unique_object, parse_constant=_reject_constant
    )
    if not isinstance(payload, dict) or not isinstance(ticket := payload.get("ticket"), dict):
        # Old records may have an issuer but no ticket stage. Keep their packet
        # history readable without guessing a route to bind during freeze.
        return None
    stage = ticket.get("stage", "first-level review")
    if not isinstance(stage, str) or not stage.strip():
        return None
    return RouteScope(issuer, stage, request.options.mail_class.value)


class AddressStoreMixin:
    """Owner-scoped address persistence; no policy inference or external actions."""

    def _bind_prepared_route(
        self: _RouteBindingStore,
        case_token: str,
        attempt: int,
        intake: IntakeRecord,
        request: LetterRequest,
    ) -> None:
        """Bind the observed route revision in the same transaction as the new packet."""
        saved = None if intake.agency_json is not None else self.get_research(intake.intake_id)
        ticket = None if saved is None else decode_research(saved.research_json).ticket
        scope = prepared_route_scope(intake, request, ticket)
        if scope is not None:
            route = self.current_route(scope)
            self.bind_route(case_token, attempt, scope, 0 if route is None else route.revision)

    def lock_destination_for_draft(
        self: _Connected,
        case_id: str,
        owner: str,
        now: datetime,
        current_disclosure_sha256: str,
        *,
        required_copy_revision: int | None = None,
        required_copy_research_sha256: str | None = None,
        expected_research_revision: int | None = None,
    ) -> SavedDestinationDecision:
        """Validate and permanently lock the current instruction before any paid drafting.

        Customer address/consent/decision writers take the same SQLite write lock:
        an earlier edit is revalidated here; a later edit is refused in its own transaction.
        A crash leaves the lock intact so recovery uses the same instruction.
        """
        _aware(now)
        database = self._require_open()
        with _transaction(database):
            _require_scope(database, case_id, owner)
            if _case_state(database, case_id) != CaseState.DRAFTING:
                raise IntakeConflict("destination_locked_after_draft")
            if expected_research_revision is not None:
                current_research = database.execute(
                    "SELECT revision FROM pb_intake_research WHERE intake_id=?", (case_id,)
                ).fetchone()
                if current_research is None or current_research[0] != expected_research_revision:
                    raise IntakeConflict("research_revision_stale")
            if required_copy_revision is not None:
                row = database.execute(
                    "SELECT revision FROM pb_required_copies WHERE case_id=? AND owner=?",
                    (case_id, owner),
                ).fetchone()
                if (0 if row is None else row[0]) != required_copy_revision:
                    raise IntakeConflict("required_copies_revision_stale")
                research = database.execute(
                    "SELECT research_json FROM pb_intake_research WHERE intake_id=?", (case_id,)
                ).fetchone()
                if (
                    research is None
                    or hashlib.sha256(research[0].encode()).hexdigest()
                    != required_copy_research_sha256
                ):
                    raise IntakeConflict("required_copies_research_stale")
            history = _decisions(database, case_id, owner)
            if not history:
                raise IntakeConflict("destination_decision_mismatch")
            saved = history[-1]
            addresses = _addresses(database, case_id, owner)
            if not mailing_gate(
                saved.decision,
                _consents(database, case_id, owner),
                case_id=case_id,
                owner=owner,
                mailed_address=saved.decision.address,
                latest_customer_address_revision=addresses[-1].revision if addresses else None,
                current_disclosure_sha256=current_disclosure_sha256,
                now=now,
            ).allowed:
                raise IntakeConflict("destination_decision_stale")
            database.execute(
                "INSERT INTO pb_address_locks VALUES (?, ?, ?) ON CONFLICT(case_id) DO NOTHING",
                (case_id, owner, now.isoformat()),
            )
            return saved

    def has_destination_lock(self: _Connected, case_id: str, owner: str) -> bool:
        """Whether the per-case destination lock row exists (read-only, owner scoped)."""
        database = self._require_open()
        with _transaction(database):
            _require_scope(database, case_id, owner)
            row = database.execute(
                "SELECT owner FROM pb_address_locks WHERE case_id=?", (case_id,)
            ).fetchone()
        if row is None:
            return False
        if row[0] != owner:
            raise StorageCorruption("malformed destination lock scope")
        return True

    def customer_address_revisions(
        self: _Connected, case_id: str, owner: str
    ) -> tuple[CustomerAddressRevision, ...]:
        database = self._require_open()
        with _transaction(database):
            _require_scope(database, case_id, owner)
            return _addresses(database, case_id, owner)

    def save_customer_address(
        self: _Connected,
        case_id: str,
        owner: str,
        address: MailingAddress,
        created_at: datetime,
        expected_revision: int | None,
    ) -> CustomerAddressRevision:
        """Append exact address fields under CAS; None expects no previous revision."""
        _aware(created_at)
        payload = _json({"address": asdict(address), "created_at": created_at})
        _address(asdict(address))
        database = self._require_open()
        with _transaction(database):
            _require_scope(database, case_id, owner)
            _require_destination_writable(database, case_id)
            history = _addresses(database, case_id, owner)
            revision = _next_revision(history[-1].revision if history else None, expected_revision)
            database.execute(
                "INSERT INTO pb_address_revisions VALUES (?, ?, ?, ?)",
                (case_id, owner, revision, payload),
            )
        return CustomerAddressRevision(case_id, owner, address, revision, created_at)

    def address_consent_events(
        self: _Connected, case_id: str, owner: str
    ) -> tuple[ConsentEvent, ...]:
        database = self._require_open()
        with _transaction(database):
            _require_scope(database, case_id, owner)
            return _consents(database, case_id, owner)

    def record_address_consent(
        self: _Connected, case_id: str, owner: str, consent: ConsentEvent
    ) -> ConsentEvent:
        """Append an explicit acceptance of the current, already stored address."""
        payload = _json(asdict(consent))
        event = _consent(_load(payload))  # Revalidate even forged frozen dataclasses.
        if event.case_id != case_id or event.owner != owner:
            raise IntakeConflict("address_scope_mismatch")
        database = self._require_open()
        with _transaction(database):
            _require_scope(database, case_id, owner)
            _require_destination_writable(database, case_id)
            addresses = _addresses(database, case_id, owner)
            if not addresses or not _bound_address(event, addresses[-1:]):
                raise IntakeConflict("address_consent_stale")
            history = _consents(database, case_id, owner)
            database.execute(
                "INSERT INTO pb_address_consents VALUES (?, ?, ?, ?)",
                (case_id, owner, len(history) + 1, payload),
            )
        return event

    def latest_destination_decision(
        self: _Connected, case_id: str, owner: str
    ) -> SavedDestinationDecision | None:
        """Read the last explicit decision; an address edit never silently replaces it."""
        database = self._require_open()
        # The dependent history reads share a snapshot even with concurrent writers.
        with _transaction(database):
            _require_scope(database, case_id, owner)
            history = _decisions(database, case_id, owner)
            return history[-1] if history else None

    def destination_decision_history(
        self: _Connected, case_id: str, owner: str
    ) -> tuple[SavedDestinationDecision, ...]:
        database = self._require_open()
        with _transaction(database):
            _require_scope(database, case_id, owner)
            return _decisions(database, case_id, owner)

    def save_destination_decision(
        self: _Connected,
        case_id: str,
        owner: str,
        decision: DestinationDecision,
        expected_revision: int | None,
    ) -> SavedDestinationDecision:
        """Append a policy decision under CAS, binding only separately recorded consent.

        Before drafting is locked, an explicit write may supersede the current revision.
        Once locked, only system verification of a first decision is allowed, plus the one
        re-mail correction: a SYSTEM_VERIFIED decision with a new fingerprint while the
        returned letter's re-mail has not been sent (see _remail_correction_open).
        """
        payload = _json(asdict(decision))
        decision = _decision(_load(payload))
        if decision.case_id != case_id or decision.owner != owner:
            raise IntakeConflict("address_scope_mismatch")
        database = self._require_open()
        with _transaction(database):
            _require_scope(database, case_id, owner)
            if decision.provenance is AddressProvenance.CUSTOMER_DIRECTED_UNVERIFIED:
                _require_destination_writable(database, case_id)
            history = _decisions(database, case_id, owner)
            revision = _next_revision(history[-1].revision if history else None, expected_revision)
            if (
                decision.provenance is AddressProvenance.SYSTEM_VERIFIED
                and history
                and _destination_locked(database, case_id)
                and not _remail_correction_open(database, case_id, history, decision)
            ):
                raise IntakeConflict("address_revision_stale")
            if decision.consent is not None:
                addresses = _addresses(database, case_id, owner)
                if not addresses or not _bound_address(decision.consent, addresses[-1:]):
                    raise IntakeConflict("address_consent_stale")
                if decision.consent not in _consents(database, case_id, owner):
                    raise IntakeConflict("address_consent_not_recorded")
            database.execute(
                "INSERT INTO pb_address_decisions VALUES (?, ?, ?, ?)",
                (case_id, owner, revision, payload),
            )
        return SavedDestinationDecision(decision, revision)
