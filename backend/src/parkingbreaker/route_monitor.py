"""Private commands for enrolling, withdrawing and checking municipal mail routes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from parkingbreaker.address_policy import address_fingerprint
from parkingbreaker.agencies import MailingAddress, SourceScope
from parkingbreaker.http_fetcher import HttpFetcher
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.mail_config import MailClass
from parkingbreaker.research.findings import normalize_whitespace, verify_quote
from parkingbreaker.research.fulfilment import verified_findings
from parkingbreaker.research.procedure import (
    jurisdiction_in_text,
    procedure_sections,
    quote_jurisdiction_matches,
    quote_procedure_section,
    quote_procedure_stage,
    same_stage,
)
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.sources import (
    AuthorityClass,
    Clock,
    Fetcher,
    FetchError,
    FetchResponse,
    SourceRecord,
    fetch_source,
    is_allowed,
)
from parkingbreaker.research.support import address_text, value_supported_by_quote
from parkingbreaker.research_records import decode_agency, decode_research
from parkingbreaker.route_registry import RouteConflict, RouteRevision, RouteScope
from parkingbreaker.transaction_store import RevisionConflict, SqliteTransactionStore


class MonitorConflict(ValueError):
    """Stable operator-facing reason keys."""


@dataclass(frozen=True)
class RouteEvidence:
    scope: RouteScope
    address_fingerprint: str
    source_url: str
    source_quote: str = field(repr=False)
    evidence_sha256: str
    source_text_sha256: str
    first_class_basis: str | None = None


def _text_sha256(text: str) -> str:
    return hashlib.sha256(normalize_whitespace(text).encode()).hexdigest()


def _quote_in_scope(text: str, quote: str, scope: RouteScope) -> bool:
    stage = quote_procedure_stage(text, quote)
    return (
        stage is not None
        and same_stage(stage, scope.stage)
        and quote_jurisdiction_matches(text, quote, scope.issuer)
    )


def _passage(text: str, *items: str) -> str:
    """The contiguous official passage containing all independently guarded quotes."""
    normal = normalize_whitespace(text)
    quotes = tuple(normalize_whitespace(item) for item in items)
    starts = tuple(normal.index(item) for item in quotes)
    end = max(start + len(item) for start, item in zip(starts, quotes, strict=True))
    return normal[min(starts) : end]


def _attention_matches(text: str, address: MailingAddress) -> bool:
    """An explicit label between the recipient and street must carry the full attention."""
    recipient = re.escape(address_text(address.recipient))
    street = re.escape(address_text(address.line1))
    labels = re.findall(
        rf"\b{recipient} (?:attn|attention) ([\w ]{{1,120}}?) {street}\b",
        address_text(text),
    )
    attention = address_text(address.attention or "")
    attention = re.sub(r"^(?:attn|attention) ", "", attention)
    return all(attention == label for label in labels)


_ORDINARY_MAIL = r"(?:ordinary|regular|first[- ]class) mail"
_POSTAL_SERVICE = r"(?:USPS|U\.S\.P\.S\.|U\.S\. Postal Service)"
_SPECIAL_MAIL = rf"(?:{_POSTAL_SERVICE} )?(?:certified|registered) mail"
# An unconditional alternative expressly permits ordinary post. Conditions are checked
# before this replacement, and a requirement elsewhere in the saved sources still vetoes it.
_EITHER_CLASS = re.compile(
    rf"\b(?:{_ORDINARY_MAIL} or (?:by )?{_SPECIAL_MAIL}"
    rf"|{_SPECIAL_MAIL} or (?:by )?{_ORDINARY_MAIL})\b",
    re.I,
)
_CONDITION = re.compile(
    r"\b(?:if|unless|except|provided|subject to|only|instead|rather|but)\b"
    r"|\b(?:approval|permission)\b[^.!?;]{0,65}\b(?:required|needed|necessary)\b"
    r"|\b(?:obtain|receive|seek|requires?)\b[^.!?;]{0,65}\b(?:approval|permission)\b",
    re.I,
)
_STAGE_NAME = (
    r"(?:initial reviews?|first-level reviews?|administrative reviews?"
    r"|administrative hearings?|hearing requests?|second.level(?: reviews?)?"
    r"|delinquency reviews?)"
)
_STAGE_TERM = re.compile(rf"\b{_STAGE_NAME}\b", re.I)


def _mail_statements(text: str) -> Iterator[str]:
    """Preserve wrapped wording and periods in postal abbreviations."""
    text = normalize_whitespace(text)
    abbreviations = {m.end() for m in re.finditer(r"\b(?:[A-Za-z]\.){2,}", text)}
    start = 0
    for boundary in re.finditer(r"(?<=[.!?;])\s+", text):
        if boundary.start() not in abbreviations:
            yield text[start : boundary.start()]
            start = boundary.end()
    if start < len(text):
        yield text[start:]


def _filing_instruction(text: str) -> tuple[str, str] | None:
    """An affirmative contest-mail instruction, never a timing or passive mail mention."""
    inferred = None
    # HTML paragraphs can separate an instruction from a heading without punctuation.
    # A single line break may just be a wrapped PDF line or a <br> inside the instruction.
    for sentence in (
        sentence
        for paragraph in re.split(r"\n\s*\n", text)
        for sentence in _mail_statements(paragraph)
    ):
        filing = normalize_whitespace(sentence)
        if _CONDITION.search(filing) or re.search(
            r"\b(?:online|payments?|pay|refunds?|waivers?|notices?|decisions?|responses?)\b",
            filing,
            re.I,
        ):
            continue
        filing = _EITHER_CLASS.sub("ordinary mail", filing)
        plain, count = re.subn(rf"\b{_ORDINARY_MAIL}\b", "mail", filing, flags=re.I)
        if (
            count <= 1
            and re.match(
                r"^(?:you (?:may|can)|to (?:submit|send|file|request)"
                r"|we accept|the agency accepts)\b",
                plain,
                re.I,
            )
            and value_supported_by_quote(FactKey.MAIL_IN_ACCEPTED, True, plain)
        ):
            if count:
                return sentence.strip(), "explicit_ordinary_mail"
            if inferred is None:
                inferred = sentence.strip(), "inferred_from_unqualified_contest_mail"
    return inferred


def _first_class_restricted(sources: Sequence[SourceRecord], scope: RouteScope) -> bool:
    """No applicable saved official procedure may leave a conflicting mail requirement."""
    restricted = re.compile(
        rf"\b(?:certified|registered)\s+(?:{_POSTAL_SERVICE}\s+)?(?:mail|letters?|post)\b"
        rf"|\b{_POSTAL_SERVICE}\s+(?:certified|registered)\b"
        r"|\bby (?:certified|registered)\b"
        r"|\b(?:return receipts?|(?:delivery|signature) confirmation"
        r"|proof of (?:postage|mailing|delivery)|certificates? of mailing)\b"
        r"|\b(?:do not|does not|cannot|can't|never|no longer|won't|will not)\b"
        r"[^.!?;]{0,90}\b(?:mail|mailed|mailing)\b|\bno (?:mail|mailed)\b"
        r"|\b(?:mail|mailed|mailing)\b[^.!?;]{0,90}"
        r"\b(?:not (?:be )?(?:accepted|allowed|permitted)|cannot be accepted|prohibited)\b"
        rf"|\b(?:no|not|never|cannot|can't|do not|no longer|won't)\b"
        rf"[^.!?;]{{0,90}}\b{_ORDINARY_MAIL}\b"
        rf"|\b{_ORDINARY_MAIL}\b[^.!?;]{{0,65}}"
        r"\b(?:not accepted|not allowed|not permitted|prohibited|ineligible"
        r"|isn't accepted|no longer accepted)\b",
        re.I,
    )
    nonordinary = re.compile(
        r"\b(?:track(?:able|ed|ing)|traceable|courier|hand[- ]deliver\w*|in person"
        r"|online|electronically|portal|website)\b"
        r"|\b(?:by hand|personal delivery)\b",
        re.I,
    )
    required = re.compile(
        r"\b(?:must|shall|required|requires|only|exclusively|have to|has to|need to)\b"
        r"|^(?:submit|send|file|deliver|use)\b",
        re.I,
    )
    for source in sources:
        for section in procedure_sections(source.text):
            lines = section.text.splitlines()
            heading = normalize_whitespace(lines[0]) if lines else ""
            named_heading = heading == section.issuer_heading or re.fullmatch(
                rf"(?:(?:optional|later) )?{_STAGE_NAME}(?: requests?)?[.:]?", heading, re.I
            )
            if (
                named_heading
                and section.stage is not None
                and not same_stage(section.stage, scope.stage)
            ) or (
                section.issuer_heading is not None
                and not jurisdiction_in_text(scope.issuer, section.issuer_heading)
            ):
                continue
            body = "\n".join(lines[1:]) if named_heading else section.text
            for filing in _mail_statements(body):
                # Fold punctuation only for veto matching, never in preserved evidence.
                filing = filing.replace("\u2019", "'").replace("\u2018", "'").replace("\u2011", "-")
                # Exempt only complete payment-only or outgoing-agency clauses. A
                # notice can be a required enclosure, and a mixed clause can also
                # direct the customer to use a special method for the appeal.
                method = rf"(?:by|via) (?:{_ORDINARY_MAIL}|{_SPECIAL_MAIL})"
                if re.fullmatch(
                    r"(?:(?:send|mail|submit|deliver) (?:your |the )?payments? "
                    r"|(?:your |the )?payments? (?:must|shall) be "
                    r"(?:sent|mailed|submitted|delivered) )"
                    rf"(?:{method}|online|electronically|with proof of (?:delivery|mailing))"
                    r"[.!?;]?",
                    filing,
                    re.I,
                ) or re.fullmatch(
                    r"(?:we|the (?:agency|city|department)) (?:will|shall|must) "
                    r"(?:send|mail|deliver) (?:you )?(?:your |the |a )?"
                    r"(?:notices?|decisions?|responses?)"
                    r"(?: about (?:your )?(?:appeals?|contests?|reviews?|requests?))?"
                    rf"(?: {method}| to you)?[.!?;]?",
                    filing,
                    re.I,
                ):
                    continue
                stages = _STAGE_TERM.findall(filing)
                if (
                    stages
                    and not any(same_stage(stage, scope.stage) for stage in stages)
                    and re.match(
                        rf"^(?:(?:for|at|during|when requesting) (?:an? |the )?"
                        rf"{_STAGE_NAME}(?: requests?)?(?: stage)?[, :]|"
                        rf"{_STAGE_NAME}(?: requests?)? "
                        rf"(?:must|shall|requires?|may|can|are required|is required)\b)",
                        filing,
                        re.I,
                    )
                ):
                    continue
                if _CONDITION.search(filing) and re.search(
                    r"\b(?:mail|mailing|post|courier|approval|approved|permission|authorized)\b",
                    filing,
                    re.I,
                ):
                    return True
                ordinary_alternative = _EITHER_CLASS.sub("ordinary mail", filing)
                elective_portal = re.search(
                    r"\b(?:online|electronically|(?:through |via )?(?:the )?portal) "
                    r"or (?:by )?mail\b|\b(?:by )?mail or "
                    r"(?:online|electronically|(?:through |via )?(?:the )?portal)\b",
                    filing,
                    re.I,
                )
                if restricted.search(ordinary_alternative) or (
                    nonordinary.search(filing)
                    and not elective_portal
                    and (
                        required.search(filing)
                        or (
                            re.search(r"\b(?:submit|send|file|deliver|use)\b", filing, re.I)
                            and not re.search(r"\b(?:may|can|optional)\b", filing, re.I)
                        )
                    )
                ):
                    return True
    return False


def _first_class_basis(quote: str | None) -> str | None:
    """Derive the operator label from preserved evidence, including older saved revisions."""
    instruction = _filing_instruction(quote or "")
    return instruction[1] if instruction is not None else None


def case_route_evidence(
    store: SqliteTransactionStore, case_id: str, mail_class: MailClass = MailClass.CERTIFIED
) -> tuple[RouteEvidence, ...]:
    """Derive class-specific destination evidence from the saved case research."""
    record = store.get_intake(case_id)
    saved = store.get_research(case_id)
    if record is None or record.agency_json is None or saved is None:
        raise MonitorConflict("approved_case_research_missing")
    try:
        agency = decode_agency(record.agency_json)
        research = decode_research(saved.research_json)
    except ValueError:
        raise MonitorConflict("saved_case_research_invalid") from None
    issuer, stage = research.manifest.issuer, research.manifest.stage
    if research.ticket.jurisdiction != agency.id or not research.decision.ok:
        raise MonitorConflict("case_route_not_approved")
    scope = RouteScope(issuer, stage, mail_class)
    scopes = agency.source_scopes()
    official = tuple(
        source
        for source in research.sources
        if source.http_status == 200
        and source.authority_class is not AuthorityClass.SECONDARY
        and is_allowed(source.url, scopes)
        and is_allowed(source.final_url, scopes)
        and (source.jurisdiction is None or source.jurisdiction == issuer)
    )
    if mail_class is MailClass.FIRST_CLASS and _first_class_restricted(official, scope):
        raise MonitorConflict("official_contest_passage_unverified")
    findings = verified_findings(research.findings, official, research.ticket)
    addresses = [item.value for item in findings if item.key is FactKey.MAILING_ADDRESS]
    if len(addresses) != 1 or not isinstance(addresses[0], dict):
        raise MonitorConflict("official_contest_address_unverified")
    value = addresses[0]
    address = MailingAddress(
        recipient=str(value["recipient"]),
        attention=str(value["attention"]) if value.get("attention") else None,
        line1=str(value["line1"]),
        line2=str(value["line2"]) if value.get("line2") else None,
        city=str(value["city"]),
        state=str(value["state"]),
        zip_code=str(value["zip_code"]),
    )
    fingerprint = address_fingerprint(address)
    evidence = []
    for source in official:
        local = verified_findings(research.findings, (source,), research.ticket)
        address_quote = next(
            (
                item.verbatim_quote
                for item in local
                if item.key is FactKey.MAILING_ADDRESS and item.value == value
            ),
            None,
        )
        mail_quote = next(
            (
                item.verbatim_quote
                for item in local
                if item.key is FactKey.MAIL_IN_ACCEPTED and item.value is True
            ),
            None,
        )
        if not address_quote or not mail_quote or not _attention_matches(source.text, address):
            continue
        basis = None
        if mail_class is MailClass.FIRST_CLASS:
            section = quote_procedure_section(source.text, address_quote)
            instruction = _filing_instruction(section.text) if section is not None else None
            if instruction is None:
                continue
            passage = _passage(source.text, mail_quote, address_quote, instruction[0])
            basis = _first_class_basis(passage)
            if basis != instruction[1]:
                continue
        else:
            passage = _passage(source.text, mail_quote, address_quote)
        if _quote_in_scope(source.text, passage, scope) and value_supported_by_quote(
            FactKey.MAILING_ADDRESS, value, passage
        ):
            evidence.append(
                RouteEvidence(
                    scope,
                    fingerprint,
                    source.final_url,
                    passage,
                    source.sha256,
                    _text_sha256(source.text),
                    basis,
                )
            )
    if not evidence:
        raise MonitorConflict("official_contest_passage_unverified")
    return tuple(dict.fromkeys(evidence))


@dataclass
class _ExactFetcher:
    """Enrolled sources are exact final URLs. Every redirect requires a new enrollment."""

    url: str
    fetcher: Fetcher
    status: int | None = None

    def get(self, url: str) -> FetchResponse:
        if url != self.url or self.status is not None:
            raise FetchError("route source redirect refused")
        response = self.fetcher.get(url)
        if response.url != url:
            raise FetchError("unexpected route source response URL")
        self.status = response.status
        return response


def _fetch_exact(url: str, fetcher: Fetcher, clock: Clock) -> tuple[SourceRecord | None, str]:
    # Enrollment already checked the final URL against the stored issuer's scopes.
    # The exact-URL guard prevents leaving the approved path or delegated tenant.
    scopes = (SourceScope((urlsplit(url).hostname or "").removesuffix(".")),)
    exact = _ExactFetcher(url, fetcher)
    result = fetch_source(url, AuthorityClass.OFFICIAL_PROCEDURE, scopes, exact, clock)
    if isinstance(result, SourceRecord) and result.http_status == 200:
        return result, ""
    if exact.status in {404, 410}:
        return None, "official_page_missing"
    return None, "official_page_unavailable"


def _changed(source: SourceRecord, scope: RouteScope, quote: str, baseline: str) -> str:
    if not verify_quote(quote, source.text) or not _quote_in_scope(source.text, quote, scope):
        return "official_contest_passage_changed"
    if _text_sha256(source.text) != baseline:
        return "official_page_text_changed"
    return ""


def enroll_route(
    store: SqliteTransactionStore,
    fetcher: Fetcher,
    clock: Clock,
    *,
    case_id: str,
    evidence_sha256: str,
    fingerprint: str,
    valid_until: datetime,
    expected_revision: int,
    command_id: str,
    mail_class: MailClass = MailClass.CERTIFIED,
) -> RouteRevision:
    if valid_until.utcoffset() is None or valid_until <= clock():
        raise MonitorConflict("route_expiry_must_be_in_the_future")
    candidates = [
        item
        for item in case_route_evidence(store, case_id, mail_class)
        if item.evidence_sha256 == evidence_sha256 and item.address_fingerprint == fingerprint
    ]
    if len(candidates) != 1:
        raise MonitorConflict("evidence_and_fingerprint_do_not_identify_one_approved_passage")
    evidence = candidates[0]
    latest, failure = _fetch_exact(evidence.source_url, fetcher, clock)
    if latest is None:
        raise MonitorConflict(failure)
    changed = _changed(latest, evidence.scope, evidence.source_quote, evidence.source_text_sha256)
    if changed:
        raise MonitorConflict(changed)
    if evidence not in case_route_evidence(store, case_id, mail_class):
        raise MonitorConflict("approved_case_research_changed")
    now = clock()
    if valid_until <= now:
        raise MonitorConflict("route_expiry_must_be_in_the_future")
    return store.change_route(
        scope=evidence.scope,
        expected_revision=expected_revision,
        command_id=command_id,
        status="verified",
        address_fingerprint=evidence.address_fingerprint,
        source_url=evidence.source_url,
        source_quote=evidence.source_quote,
        evidence_sha256=evidence.evidence_sha256,
        source_text_sha256=evidence.source_text_sha256,
        valid_until=valid_until,
        now=now,
    )


def _withdraw(
    store: SqliteTransactionStore,
    scope: RouteScope,
    route: RouteRevision | None,
    status: str,
    expected_revision: int,
    command_id: str,
    now: datetime,
) -> RouteRevision:
    return store.change_route(
        scope=scope,
        expected_revision=expected_revision,
        command_id=command_id,
        status=status,
        address_fingerprint=None if route is None else route.address_fingerprint,
        source_url=None if route is None else route.source_url,
        source_quote=None if route is None else route.source_quote,
        evidence_sha256=None if route is None else route.evidence_sha256,
        source_text_sha256=None if route is None else route.source_text_sha256,
        valid_until=None if route is None else route.valid_until,
        now=now,
    )


@dataclass(frozen=True)
class RouteCheck:
    scope: RouteScope
    revision: int
    result: str
    reason: str


def check_routes(
    store: SqliteTransactionStore, fetcher: Fetcher, clock: Clock
) -> Iterator[RouteCheck]:
    for route in store.monitored_routes():
        if route.status == "verified":
            result, reason = _check_route(store, route, fetcher, clock)
            yield RouteCheck(route.scope, route.revision, result, reason)


def _check_route(
    store: SqliteTransactionStore, route: RouteRevision, fetcher: Fetcher, clock: Clock
) -> tuple[str, str]:
    previous_change = store.route_invalidation(route)
    if previous_change is not None:
        return _suspend_changed_route(store, route, previous_change, clock())
    if not route.verified_at(clock()):
        return "expired", "route_authorization_expired"
    if not route.source_url or not route.source_quote or not route.source_text_sha256:
        return "unverified", "approved_source_snapshot_missing"
    fresh, failure = _fetch_exact(route.source_url, fetcher, clock)
    now = clock()
    if not route.verified_at(now):
        return "expired", "route_authorization_expired"
    if fresh is None and failure != "official_page_missing":
        return "unverified", failure
    reason = (
        failure
        if fresh is None
        else _changed(fresh, route.scope, route.source_quote, route.source_text_sha256)
    )
    if not reason:
        if not store.confirm_route_check(route.scope, route.revision, now):
            return "superseded", "route_revision_changed"
        return "unchanged", "official_contest_passage_confirmed"

    store.invalidate_route_check(route, reason, now)
    return _suspend_changed_route(store, route, reason, now)


def _suspend_changed_route(
    store: SqliteTransactionStore, route: RouteRevision, reason: str, now: datetime
) -> tuple[str, str]:
    identity = json.dumps((asdict(route.scope), route.revision), sort_keys=True)
    command = "route-monitor-" + hashlib.sha256(identity.encode()).hexdigest()
    try:
        _withdraw(store, route.scope, route, "suspended", route.revision, command, now)
    except RouteConflict:
        return "superseded", "route_revision_changed"
    except RevisionConflict:
        return "unverified", "submission_ownership_unavailable"
    return "suspended", reason


def _database_path(path: Path) -> Path:
    if not path.is_file():
        raise MonitorConflict("existing_route_database_required")
    resolved = path.resolve(strict=True)
    for parent in {path.parent, resolved.parent}:
        for name in ("RESTORE_QUARANTINE.json", "FAKE_RECOVERY_ONLY"):
            marker = parent / name
            if marker.exists() or marker.is_symlink():
                raise MonitorConflict("route_management_disabled_on_restored_data")
    return resolved


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="parkingbreaker.route_monitor", description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="check each current official route once")
    commands.add_parser("evidence", help="show eligibility and evidence from saved case research")
    commands.add_parser("enroll", help="verify and enroll an exact approved address")
    for name in ("evidence", "enroll"):
        commands.choices[name].add_argument("--case-id", required=True)
    enrollment = commands.choices["enroll"]
    enrollment.add_argument("--address-fingerprint", required=True)
    enrollment.add_argument("--evidence-sha256", required=True)
    enrollment.add_argument("--valid-until", required=True, type=datetime.fromisoformat)
    for name in ("show", "suspend", "revoke"):
        command = commands.add_parser(name)
        command.add_argument("--issuer", required=True)
        command.add_argument("--stage", required=True)
    for name in ("evidence", "enroll", "show", "suspend", "revoke"):
        commands.choices[name].add_argument(
            "--mail-class", type=MailClass, choices=tuple(MailClass), default=MailClass.CERTIFIED
        )
    for name in ("enroll", "suspend", "revoke"):
        command = commands.choices[name]
        command.add_argument("--expected-revision", required=True, type=int)
        command.add_argument("--command-id", required=True)
    return parser


def _route_summary(
    store: SqliteTransactionStore, route: RouteRevision, now: datetime
) -> dict[str, object]:
    return {
        "scope": asdict(route.scope),
        "revision": route.revision,
        "status": route.status,
        "official_check_current": store.route_check_current(route, now),
        "observed_change": store.route_invalidation(route),
        "address_fingerprint": route.address_fingerprint,
        "evidence_sha256": route.evidence_sha256,
        "first_class_basis": (
            _first_class_basis(route.source_quote)
            if route.scope.mail_class == MailClass.FIRST_CLASS
            else None
        ),
        "valid_until": route.valid_until.isoformat() if route.valid_until is not None else None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    def clock() -> datetime:
        return datetime.now(UTC)

    try:
        with SqliteTransactionStore(_database_path(args.database)) as store:
            if args.command == "evidence":
                evidence = case_route_evidence(store, args.case_id, args.mail_class)
                print(json.dumps([asdict(item) for item in evidence]))
                return 0
            if args.command == "check":
                results = list(check_routes(store, HttpFetcher(timeout_s=5.0), clock))
                print(json.dumps([asdict(item) for item in results]))
                return int(any(item.result == "unverified" for item in results))
            if args.command == "enroll":
                route = enroll_route(
                    store,
                    HttpFetcher(timeout_s=5.0),
                    clock,
                    case_id=args.case_id,
                    evidence_sha256=args.evidence_sha256,
                    fingerprint=args.address_fingerprint,
                    mail_class=args.mail_class,
                    valid_until=args.valid_until,
                    expected_revision=args.expected_revision,
                    command_id=args.command_id,
                )
            else:
                scope = RouteScope(args.issuer, args.stage, args.mail_class)
                current = store.current_route(scope)
                if args.command == "show":
                    if current is None:
                        raise MonitorConflict("route_not_enrolled")
                    route = current
                else:
                    route = _withdraw(
                        store,
                        scope,
                        current,
                        "suspended" if args.command == "suspend" else "revoked",
                        args.expected_revision,
                        args.command_id,
                        clock(),
                    )
            print(json.dumps(_route_summary(store, route, clock())))
    except MonitorConflict as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    except RouteConflict:
        print(json.dumps({"error": "route_revision_or_command_conflict"}), file=sys.stderr)
        return 1
    except RevisionConflict:
        print(json.dumps({"error": "submission_ownership_unavailable"}), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error, StorageCorruption):
        print(json.dumps({"error": "route_database_unavailable"}), file=sys.stderr)
        return 1
    except ValueError:
        print(json.dumps({"error": "invalid_route_command"}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
