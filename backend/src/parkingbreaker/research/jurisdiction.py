"""Pure DotGov candidate generation for architecture v7 sections 3 and 4.

A candidate is never a resolved agency: identity is finalised later from official-page
evidence establishing the issuer, deciding body and stage. Neither a name score nor
DotGov ownership establishes authority over a ticket. Callers supply CSV text and date;
this module never reads files, the network, clocks or environment variables.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from io import StringIO

from parkingbreaker.agencies import IssuerType, validate_domains

_HEADER = (
    "Domain name",
    "Domain type",
    "Organization name",
    "Suborganization name",
    "City",
    "State",
    "Security contact email",
)
_ISSUER_TYPES = {
    "City": IssuerType.MUNICIPAL,
    "County": IssuerType.COUNTY,
    "State or territory": IssuerType.STATE_AGENCY,
    "Special district": IssuerType.AUTHORITY,
    "Interstate": IssuerType.AUTHORITY,
    "School district": IssuerType.PUBLIC_EDUCATION,
    "Tribal": IssuerType.TRIBAL,
    "Federal - Executive": IssuerType.FEDERAL,
    "Federal - Judicial": IssuerType.FEDERAL,
    "Federal - Legislative": IssuerType.FEDERAL,
    "Federal": IssuerType.FEDERAL,
}
_ABBREVIATIONS = {
    "dept": "department",
    "univ": "university",
    "auth": "authority",
    "twp": "township",
    "co.": "county",
}
_COMPANY_SUFFIXES = frozenset(
    {"company", "corp", "corporation", "inc", "incorporated", "llc", "limited", "ltd"}
)
_FILLERS = frozenset({"the", "of"})
_ADMINISTRATIVE_WORDS = frozenset(
    {"department", "office", "division", "bureau", "services", "agency", "administration"}
)
_MUNICIPAL_TYPES = frozenset({IssuerType.MUNICIPAL, IssuerType.CONSOLIDATED_CITY_COUNTY})
_KIND_TYPES = {
    **dict.fromkeys(("city", "town", "township", "borough", "village"), _MUNICIPAL_TYPES),
    **dict.fromkeys(
        ("county", "parish"),
        frozenset(
            {
                IssuerType.COUNTY,
                IssuerType.CONSOLIDATED_CITY_COUNTY,
            }
        ),
    ),
    **dict.fromkeys(("university", "college", "school"), frozenset({IssuerType.PUBLIC_EDUCATION})),
    **dict.fromkeys(("transit", "airport", "port", "authority"), frozenset({IssuerType.AUTHORITY})),
    **dict.fromkeys(("tribe", "tribal", "nation"), frozenset({IssuerType.TRIBAL})),
    "federal": frozenset({IssuerType.FEDERAL}),
}
# Shared phrases, not generic "department", "district", "park" or "service" alone.
_FEDERAL_SIGNALS = (
    frozenset({"federal"}),
    frozenset({"national", "park", "service"}),
    frozenset({"district", "court"}),
    frozenset({"supreme", "court"}),
    frozenset({"united", "states"}),
    frozenset({"congress"}),
    frozenset({"senate"}),
)
_TOKEN_SCORE = 10
_ADMINISTRATIVE_SCORE = 2
_COVERAGE_BONUS = 8
_ACRONYM_BONUS = 10
_CITY_BONUS = 5
_KIND_BONUS = 3
_KIND_PENALTY = 6
_MIN_SCORE = 15
_MIN_MARGIN = 6


@dataclass(frozen=True)
class DotGovRecord:
    """Directory ownership data only; the security contact email is never retained."""

    domain: str
    domain_type: str
    organization: str
    suborganization: str | None
    city: str
    state: str | None


@dataclass(frozen=True)
class DotGovSnapshot:
    snapshot_date: date
    records: tuple[DotGovRecord, ...]
    skipped_counts: dict[str, int]
    content_sha256: str


def snapshot_is_current(snapshot: DotGovSnapshot, today: date, max_age_days: int = 31) -> bool:
    """Accept only snapshots dated from today back through max_age_days, inclusive.

    A stale snapshot must not be used: the caller ends NOT_YET_VERIFIED rather than
    matching against old data. Future-dated snapshots are also rejected.
    """
    return 0 <= (today - snapshot.snapshot_date).days <= max_age_days


def parse_dotgov_csv(text: str, snapshot_date: date) -> DotGovSnapshot:
    """Parse the exact CISA header in order, discarding emails and counting skipped rows.

    Counts contain only reasons encountered: election_type, unknown_type, invalid_domain
    and malformed_row (wrong field count). Structurally invalid CSV raises ValueError.
    The content hash identifies the exact input text encoded as UTF-8.
    """
    reader = csv.reader(StringIO(text, newline=""), strict=True)
    records: list[DotGovRecord] = []
    skipped: Counter[str] = Counter()
    try:
        header = tuple(next(reader, ()))
        if header != _HEADER:
            missing = list((Counter(_HEADER) - Counter(header)).elements())
            extra = list((Counter(header) - Counter(_HEADER)).elements())
            raise ValueError(
                f"DotGov header: missing columns {missing!r}; extra columns {extra!r}; "
                "columns must match the exact CISA order"
            )
        for row in reader:
            if len(row) != len(_HEADER):
                skipped["malformed_row"] += 1
                continue
            domain_type = row[1].strip()
            if domain_type.endswith(" - Election"):
                skipped["election_type"] += 1
                continue
            if domain_type not in _ISSUER_TYPES:
                skipped["unknown_type"] += 1
                continue
            domain = row[0].strip().lower()
            try:
                validate_domains((domain,))
            except ValueError:
                skipped["invalid_domain"] += 1
                continue
            records.append(
                DotGovRecord(
                    domain=domain,
                    domain_type=row[1],
                    organization=row[2].strip(),
                    suborganization=row[3].strip() or None,
                    city=row[4].strip(),
                    state=row[5].strip().upper() or None,
                )
            )
    except csv.Error as exc:
        raise ValueError(f"invalid DotGov CSV: {exc}") from exc
    return DotGovSnapshot(
        snapshot_date,
        tuple(records),
        dict(skipped),
        hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


@dataclass(frozen=True)
class IssuerQuery:
    """Verbatim printed issuer and customer location, used only to generate candidates."""

    printed_issuer: str
    customer_city: str
    customer_state: str

    def __post_init__(self) -> None:
        if not self.printed_issuer.strip() or not self.customer_city.strip():
            raise ValueError("printed issuer and customer city must not be blank")
        if not re.fullmatch(r"[A-Z]{2}", self.customer_state):
            raise ValueError("customer state must be a two-letter uppercase code")


@dataclass(frozen=True)
class IssuerCandidate:
    """One directory organization, with sorted domains and the first domain's record.

    A candidate is never a resolved agency; official-page evidence finalises identity.
    """

    record: DotGovRecord
    issuer_type: IssuerType
    score: int
    reasons: tuple[str, ...]
    domains: tuple[str, ...]


class MatchOutcome(StrEnum):
    NONE = "NONE"
    SINGLE = "SINGLE"
    AMBIGUOUS = "AMBIGUOUS"


def _normalise(text: str) -> tuple[str, ...]:
    """Candidate-only tokens; retain kind words, including city/town/village prefixes.

    Expand dotted co. as county unless a company suffix follows it. Bare co is left
    alone. Punctuation becomes separators; only 'the' and 'of' are dropped. No aliases
    or identity equivalences are inferred.
    """
    words = re.findall(r"[^\W_]+\.?", text.casefold())
    tokens = [word.removesuffix(".") for word in words]
    result: list[str] = []
    for index, word in enumerate(words):
        token = tokens[index]
        abbreviation = "co." if word == "co." else token
        if abbreviation != "co." or not _COMPANY_SUFFIXES.intersection(tokens[index + 1 :]):
            token = _ABBREVIATIONS.get(abbreviation, token)
        if token not in _FILLERS:
            result.append(token)
    return tuple(result)


def distinctive_tokens(text: str) -> frozenset[str]:
    """Shared name guard tokens: normalise without kind or administrative words.

    This is only an overlap veto, never an identity assessment or alias resolver.
    """
    return frozenset(_normalise(text)) - _KIND_TYPES.keys() - _ADMINISTRATIVE_WORDS


type _OrganizationKey = tuple[tuple[str, ...], tuple[str, ...], str | None, IssuerType]


def _organization_key(record: DotGovRecord, issuer_type: IssuerType) -> _OrganizationKey:
    """Keep parent/suborganization boundaries, kind words, state and issuer type distinct."""
    return (
        _normalise(record.organization),
        _normalise(record.suborganization or ""),
        record.state,
        issuer_type,
    )


def group_dotgov_records(
    snapshot: DotGovSnapshot,
) -> tuple[tuple[IssuerType, tuple[DotGovRecord, ...]], ...]:
    """Shared directory grouping for name-scored and search-derived candidates.

    Retain all organization domains and sort rows so the first is the canonical
    record. Grouping authenticates directory ownership only, never ticket identity.
    """
    groups: dict[_OrganizationKey, list[DotGovRecord]] = {}
    for record in snapshot.records:
        issuer_type = _ISSUER_TYPES.get(record.domain_type.strip())
        if issuer_type is not None:
            groups.setdefault(_organization_key(record, issuer_type), []).append(record)
    return tuple(
        (
            key[3],
            tuple(
                sorted(
                    records,
                    key=lambda row: (
                        row.domain,
                        row.organization,
                        row.suborganization or "",
                        row.city,
                        row.domain_type,
                    ),
                )
            ),
        )
        for key, records in groups.items()
    )


def issuer_candidates(query: IssuerQuery, snapshot: DotGovSnapshot) -> tuple[IssuerCandidate, ...]:
    """Generate candidates, never resolved agencies (v7 sections 3 and 4).

    Scoring: +10 per distinct shared issuer/organization-or-suborganization token,
    excluding kind and administrative words; +2 per shared administrative token;
    +8 when all nonempty distinctive record-name tokens appear in the printed issuer,
    provided no printed kind conflicts with the issuer type; +10 when a domain's first
    label, without hyphens, equals the initials of all normalised printed tokens
    (including kind and administrative words); +5 for an
    exact normalised customer/record city match; +3 per compatible printed kind word,
    -6 per incompatible kind word. Kind mismatches never exclude. Administrative words
    alone cannot satisfy overlap: zero distinctive overlap excludes unless an acronym
    matches. NONE means no score reaches 15; SINGLE requires that minimum and a lead
    of at least 6 over the runner-up
    (if any); otherwise AMBIGUOUS. Court is a name token, never an issuer kind.

    Non-federal rows must match the customer state. Federal rows may cross states only
    with a shared federal signal phrase/token from _FEDERAL_SIGNALS. Missing or weak
    matches need later search; no outcome here establishes private-issuer exclusion.
    Identical normalised organization, suborganization, state and issuer type form one
    candidate. All its domains are retained, sorted; record belongs to the first domain.
    City and acronym bonuses apply once if any member matches. Identity is finalised
    later from official-page evidence.
    """
    printed_tokens = _normalise(query.printed_issuer)
    printed = frozenset(printed_tokens)
    acronym = "".join(token[0] for token in printed_tokens)
    city = _normalise(query.customer_city)
    candidates: list[IssuerCandidate] = []
    for issuer_type, records in group_dotgov_records(snapshot):
        record = records[0]
        organization, suborganization, state, _ = _organization_key(record, issuer_type)
        domains = tuple(sorted({row.domain for row in records}))
        name_tokens = frozenset((*organization, *suborganization))
        shared = printed & name_tokens
        distinctive_name = name_tokens - _KIND_TYPES.keys() - _ADMINISTRATIVE_WORDS
        distinctive = shared & distinctive_name
        administrative = shared & _ADMINISTRATIVE_WORDS
        acronym_matches = bool(acronym) and any(
            domain.split(".", 1)[0].replace("-", "") == acronym for domain in domains
        )
        if not distinctive and not acronym_matches:
            continue
        if issuer_type is IssuerType.FEDERAL:
            if not any(signal <= shared for signal in _FEDERAL_SIGNALS):
                continue
        elif state != query.customer_state:
            continue
        score = _TOKEN_SCORE * len(distinctive) + _ADMINISTRATIVE_SCORE * len(administrative)
        reasons: list[str] = []
        if distinctive:
            reasons.append(
                f"organization/suborganization share tokens: {', '.join(sorted(distinctive))}"
            )
        if administrative:
            reasons.append(
                "organization/suborganization share administrative tokens: "
                + ", ".join(sorted(administrative))
            )
        kinds = sorted(printed & _KIND_TYPES.keys())
        if (
            distinctive_name
            and distinctive_name <= printed
            and all(issuer_type in _KIND_TYPES[kind] for kind in kinds)
        ):
            score += _COVERAGE_BONUS
            reasons.append(
                "printed issuer covers all distinctive organization/suborganization tokens: "
                + ", ".join(sorted(distinctive_name))
            )
        if acronym_matches:
            score += _ACRONYM_BONUS
            reasons.append(f"domain first label matches printed issuer acronym: {acronym}")
        if city and any(city == _normalise(row.city) for row in records):
            score += _CITY_BONUS
            reasons.append("record city matches customer city")
        for kind in kinds:
            if issuer_type in _KIND_TYPES[kind]:
                score += _KIND_BONUS
                reasons.append(f"printed issuer kind '{kind}' agrees with {issuer_type.value}")
            else:
                score -= _KIND_PENALTY
                reasons.append(f"printed issuer kind '{kind}' disagrees with {issuer_type.value}")
        candidates.append(IssuerCandidate(record, issuer_type, score, tuple(reasons), domains))
    return tuple(
        sorted(candidates, key=lambda candidate: (-candidate.score, candidate.record.domain))
    )


def match_outcome(candidates: tuple[IssuerCandidate, ...]) -> MatchOutcome:
    """Compare organizations under issuer_candidates' scoring rules; SINGLE still needs
    official evidence. A candidate is never a resolved agency: identity is finalised
    later from official-page evidence (v7 sections 3 and 4). NONE means search next.
    """
    if not candidates:
        return MatchOutcome.NONE
    by_organization: dict[_OrganizationKey, int] = {}
    for candidate in candidates:
        key = _organization_key(candidate.record, candidate.issuer_type)
        by_organization[key] = max(candidate.score, by_organization.get(key, candidate.score))
    scores = sorted(by_organization.values(), reverse=True)
    if scores[0] < _MIN_SCORE:
        return MatchOutcome.NONE
    if len(scores) > 1 and scores[0] - scores[1] < _MIN_MARGIN:
        return MatchOutcome.AMBIGUOUS
    return MatchOutcome.SINGLE
