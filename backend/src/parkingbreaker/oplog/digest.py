"""The v7 log digest: the only view of a case the log writer AI ever sees. Pure: no I/O.

Built by code from an allowlist (v6 section 9.2, v7 sections 7-8): categories, counts,
durations, lags in days, cost in cents and the ISO week. No personal identifiers, exact
dates, addresses, quotes, URLs, rule content or free text. Agency identity is not PII:
issuer_token encodes only the service basis and first official domain, never agency names.

A digest is built for every intake that reached research, including pre-hold stops
(owner direction 2026-10-13), extending v6's authorization boundary. phase_reached
distinguishes those attempts. The transaction path writes logs/counters, never reads them.

ticket_type is the counter category; violation_category retains the existing log role.
They may be equal. stage_token is a caller-supplied category, never verbatim notice text;
None becomes "unknown" in counters. No stage ontology is inferred here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Literal

from parkingbreaker.agencies import IssuerType, ResolvedAgency
from parkingbreaker.case_state import HardStop, StopFamily
from parkingbreaker.json_types import JsonValue
from parkingbreaker.research.delegation import DelegationOutcome
from parkingbreaker.research.jurisdiction import MatchOutcome
from parkingbreaker.research.veto_codes import VetoCode

_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
_HEX_TO_LETTERS = str.maketrans("0123456789abcdef", "ghijklmnopabcdef")
_LONG_DIGITS = re.compile(r"\d{5,}")
_PLATE_LIKE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9]{2,8}$")
_ISO_WEEK = re.compile(r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$")

DigestOutcome = Literal["SERVICEABLE"] | StopFamily


class ResearchStatus(StrEnum):
    PENDING = "PENDING"
    REQUIRED_FACTS_UNVERIFIED = "REQUIRED_FACTS_UNVERIFIED"
    TIER1_PASSED = "TIER1_PASSED"


class JurisdictionExclusion(StrEnum):
    """Explicit service-policy decisions, never inferred from a research gap."""

    BUSINESS_EXCLUSION = "BUSINESS_EXCLUSION"
    UNRECORDED_BASIS = "UNRECORDED_BASIS"


class CounterMilestone(StrEnum):
    REQUEST_RECEIVED = "REQUEST_RECEIVED"
    GENUINELY_UNSUPPORTED = "GENUINELY_UNSUPPORTED"
    REQUIRED_FACTS_UNVERIFIED = "REQUIRED_FACTS_UNVERIFIED"
    TIER1_PASSED = "TIER1_PASSED"
    APPEAL_COMPLETED = "APPEAL_COMPLETED"
    OTHER_EXIT = "OTHER_EXIT"


class PhaseReached(StrEnum):
    INTAKE = "intake"
    PREHOLD = "prehold"
    RESEARCHING = "researching"
    DRAFTING = "drafting"
    MAILED = "mailed"


def iso_week_of(day: date) -> str:
    """The ISO week string (e.g. '2026-W41') for a date, so callers never pass the date."""
    year, week, _ = day.isocalendar()
    return f"{year:04d}-W{week:02d}"


def _check_token(field: str, value: str) -> None:
    if (
        not isinstance(value, str)
        or not _TOKEN.fullmatch(value)
        or _LONG_DIGITS.search(value)
        or any(_PLATE_LIKE.fullmatch(part) for part in re.split(r"[_-]", value))
    ):
        raise ValueError(f"{field}: not a category token")


def issuer_token(agency: ResolvedAgency) -> str:
    """Category identity from basis key and first official domain; no names or case ids.

    Replace ':' and '.' with '_', then apply the same strict category rule. A token over
    40 characters keeps its first 31 and appends '_' plus 8 letters derived from a
    SHA-256 of the full token, so long real domains still log without colliding.
    """
    token = f"{agency.basis_key}:{agency.official_domains[0]}".replace(":", "_").replace(".", "_")
    if len(token) > 40:
        suffix = hashlib.sha256(token.encode()).hexdigest()[:8].translate(_HEX_TO_LETTERS)
        token = f"{token[:31]}_{suffix}"
    _check_token("issuer_token", token)
    return token


def _check_count(field: str, value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field}: must be a nonnegative integer")


def _check_enum(field: str, value: object, enum: type[StrEnum]) -> None:
    if not isinstance(value, enum):
        raise ValueError(f"{field}: expected {enum.__name__}")


def _pairs(field: str, values: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    for key, count in values.items():
        _check_token(field, key)
        _check_count(field, count)
    return tuple(sorted(values.items()))


@dataclass(frozen=True)
class LogDigest:
    """Allowlisted anonymous facts about one intake that reached research.

    Mapping fields are sorted pairs for immutability/hashability. Veto codes are
    canonicalised even on direct construction. Build one with make_digest.
    """

    issuer_token: str
    issuer_type: IssuerType
    basis_key_token: str
    ticket_type: str
    violation_category: str
    stage_token: str | None
    phase_reached: PhaseReached
    outcome: DigestOutcome
    hard_stop: HardStop | None
    veto_codes: tuple[VetoCode, ...]
    match_outcome: MatchOutcome | None
    delegation_counts: tuple[tuple[DelegationOutcome, int], ...]
    mode: str
    states_reached: tuple[str, ...]
    stage_durations_s: tuple[tuple[str, int], ...]
    research_counts: tuple[tuple[str, int], ...]
    question_types: tuple[str, ...]
    default_types: tuple[str, ...]
    redraft_count: int
    flag_types: tuple[str, ...]
    feasibility_margin_days: int | None
    lob_lag_days: int | None
    usps_lag_days: int | None
    payment_class: str
    remedy_class: str | None
    cost_cents: int
    ai_cost_cents: int
    mail_class: str | None
    iso_week: str
    research_status: ResearchStatus = ResearchStatus.PENDING
    jurisdiction_exclusion: JurisdictionExclusion | None = None

    def __post_init__(self) -> None:
        for name in (
            "issuer_token",
            "basis_key_token",
            "ticket_type",
            "violation_category",
            "mode",
            "payment_class",
        ):
            _check_token(name, getattr(self, name))
        for name in ("stage_token", "remedy_class", "mail_class"):
            value = getattr(self, name)
            if value is not None:
                _check_token(name, value)
        _check_enum("issuer_type", self.issuer_type, IssuerType)
        _check_enum("phase_reached", self.phase_reached, PhaseReached)
        _check_enum("research_status", self.research_status, ResearchStatus)
        if self.research_status is ResearchStatus.REQUIRED_FACTS_UNVERIFIED and (
            self.outcome is not StopFamily.NOT_YET_VERIFIED
            or self.phase_reached is PhaseReached.MAILED
        ):
            raise ValueError("research_status: failed research must remain NOT_YET_VERIFIED")
        if self.jurisdiction_exclusion is not None:
            _check_enum(
                "jurisdiction_exclusion", self.jurisdiction_exclusion, JurisdictionExclusion
            )
            if (
                self.outcome
                is not (
                    StopFamily.NOT_YET_VERIFIED
                    if self.jurisdiction_exclusion is JurisdictionExclusion.UNRECORDED_BASIS
                    else StopFamily.BUSINESS_EXCLUSION
                )
                or self.research_status is not ResearchStatus.PENDING
                or self.phase_reached is PhaseReached.MAILED
            ):
                raise ValueError(
                    "jurisdiction_exclusion: incompatible with research or scope result"
                )
        if self.outcome != "SERVICEABLE":
            _check_enum("outcome", self.outcome, StopFamily)
        for name, enum in (("hard_stop", HardStop), ("match_outcome", MatchOutcome)):
            value = getattr(self, name)
            if value is not None:
                _check_enum(name, value, enum)
        if not isinstance(self.veto_codes, tuple):
            raise ValueError("veto_codes: expected tuple")
        for code in self.veto_codes:
            _check_enum("veto_codes", code, VetoCode)
        object.__setattr__(self, "veto_codes", tuple(sorted(set(self.veto_codes))))
        for name in ("states_reached", "question_types", "default_types", "flag_types"):
            values = getattr(self, name)
            if not isinstance(values, tuple):
                raise ValueError(f"{name}: expected tuple")
            for item in values:
                _check_token(name, item)
        for name in ("stage_durations_s", "research_counts", "delegation_counts"):
            pairs = getattr(self, name)
            if not isinstance(pairs, tuple):
                raise ValueError(f"{name}: expected tuple")
            for pair in pairs:
                if not isinstance(pair, tuple) or len(pair) != 2:
                    raise ValueError(f"{name}: expected key/count pairs")
                key, count = pair
                if name == "delegation_counts":
                    _check_enum(name, key, DelegationOutcome)
                else:
                    _check_token(name, key)
                _check_count(name, count)
            if pairs != tuple(sorted(pairs)) or len(dict(pairs)) != len(pairs):
                raise ValueError(f"{name}: expected sorted unique keys")
        for name in ("redraft_count", "cost_cents", "ai_cost_cents"):
            _check_count(name, getattr(self, name))
        for name in ("lob_lag_days", "usps_lag_days"):
            value = getattr(self, name)
            if value is not None:
                _check_count(name, value)
        if (
            self.feasibility_margin_days is not None
            and type(self.feasibility_margin_days) is not int
        ):
            raise ValueError("feasibility_margin_days: expected integer")
        if self.ai_cost_cents > self.cost_cents:
            raise ValueError("ai_cost_cents: cannot exceed total cost")
        if not isinstance(self.iso_week, str) or not _ISO_WEEK.fullmatch(self.iso_week):
            raise ValueError("iso_week: expected YYYY-Www")

    def to_prompt_json(self) -> str:
        """Deterministic JSON for the log writer's prompt; explicit allowlist only."""
        payload: dict[str, JsonValue] = {
            "issuer_token": self.issuer_token,
            "issuer_type": self.issuer_type.value,
            "basis_key_token": self.basis_key_token,
            "ticket_type": self.ticket_type,
            "violation_category": self.violation_category,
            "stage_token": self.stage_token,
            "phase_reached": self.phase_reached.value,
            "outcome": self.outcome,
            "hard_stop": self.hard_stop.value if self.hard_stop is not None else None,
            "veto_codes": [code.value for code in self.veto_codes],
            "match_outcome": self.match_outcome.value if self.match_outcome is not None else None,
            "delegation_counts": {key.value: count for key, count in self.delegation_counts},
            "mode": self.mode,
            "states_reached": list(self.states_reached),
            "stage_durations_s": dict(self.stage_durations_s),
            "research_counts": dict(self.research_counts),
            "question_types": list(self.question_types),
            "default_types": list(self.default_types),
            "redraft_count": self.redraft_count,
            "flag_types": list(self.flag_types),
            "feasibility_margin_days": self.feasibility_margin_days,
            "lob_lag_days": self.lob_lag_days,
            "usps_lag_days": self.usps_lag_days,
            "payment_class": self.payment_class,
            "remedy_class": self.remedy_class,
            "cost_cents": self.cost_cents,
            "ai_cost_cents": self.ai_cost_cents,
            "mail_class": self.mail_class,
            "iso_week": self.iso_week,
            "research_status": self.research_status.value,
            "jurisdiction_exclusion": (
                self.jurisdiction_exclusion.value if self.jurisdiction_exclusion else None
            ),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def make_digest(
    *,
    issuer_token: str,
    issuer_type: IssuerType,
    basis_key_token: str,
    ticket_type: str,
    violation_category: str,
    stage_token: str | None,
    phase_reached: PhaseReached,
    outcome: DigestOutcome,
    hard_stop: HardStop | None,
    veto_codes: tuple[VetoCode, ...],
    match_outcome: MatchOutcome | None,
    delegation_counts: Mapping[DelegationOutcome, int],
    mode: str,
    states_reached: tuple[str, ...],
    stage_durations_s: Mapping[str, int],
    research_counts: Mapping[str, int],
    question_types: tuple[str, ...],
    default_types: tuple[str, ...],
    redraft_count: int,
    flag_types: tuple[str, ...],
    feasibility_margin_days: int | None,
    lob_lag_days: int | None,
    usps_lag_days: int | None,
    payment_class: str,
    remedy_class: str | None,
    cost_cents: int,
    ai_cost_cents: int,
    mail_class: str | None,
    iso_week: str,
    research_status: ResearchStatus = ResearchStatus.PENDING,
    jurisdiction_exclusion: JurisdictionExclusion | None = None,
) -> LogDigest:
    """Build a digest; the keyword list is the allowlist. Raises ValueError on bad values."""
    for key, count in delegation_counts.items():
        _check_enum("delegation_counts", key, DelegationOutcome)
        _check_count("delegation_counts", count)
    return LogDigest(
        issuer_token=issuer_token,
        issuer_type=issuer_type,
        basis_key_token=basis_key_token,
        ticket_type=ticket_type,
        violation_category=violation_category,
        stage_token=stage_token,
        phase_reached=phase_reached,
        outcome=outcome,
        hard_stop=hard_stop,
        veto_codes=veto_codes,
        match_outcome=match_outcome,
        delegation_counts=tuple(sorted(delegation_counts.items())),
        mode=mode,
        states_reached=tuple(states_reached),
        stage_durations_s=_pairs("stage_durations_s", stage_durations_s),
        research_counts=_pairs("research_counts", research_counts),
        question_types=tuple(question_types),
        default_types=tuple(default_types),
        redraft_count=redraft_count,
        flag_types=tuple(flag_types),
        feasibility_margin_days=feasibility_margin_days,
        lob_lag_days=lob_lag_days,
        usps_lag_days=usps_lag_days,
        payment_class=payment_class,
        remedy_class=remedy_class,
        cost_cents=cost_cents,
        ai_cost_cents=ai_cost_cents,
        mail_class=mail_class,
        iso_week=iso_week,
        research_status=research_status,
        jurisdiction_exclusion=jurisdiction_exclusion,
    )
