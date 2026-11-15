"""Durable serviceability blocks and bounded customer challenges. Pure: no I/O, no clock.

Architecture v8 section 20.18: when official evidence shows an issuer and stage cannot be
served (for example an in-person-only first step), a durable block keyed by issuer,
procedural stage and exact reason code records the quoted official text, source and date.
Later tickets for that issuer and stage receive an immediate, uncharged notice with the
exact reason and source, and no research spend. Unavailable sites or inconclusive research
never create a block. Blocks lift only through customer challenges: a challenge runs
automatic research only when it supplies a source not previously checked, at most once per
block per week, within a global daily challenge budget and per-email/per-network limits;
the customer's evidence is a lead and the block lifts only when the official source
confirms. Turned-away volume per block is tracked. No staff review.

Callers pass every time value in; this module never reads the clock. The customer's own
words are never assessed here: a challenge lead's note is carried, never judged.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum

from parkingbreaker.research.procedure import same_stage

CHALLENGE_INTERVAL = timedelta(days=7)


class BlockReason(StrEnum):
    """Exact reason code for why an issuer and stage cannot be served."""

    IN_PERSON_ONLY_FIRST_STEP = "IN_PERSON_ONLY_FIRST_STEP"
    PORTAL_ONLY_FIRST_STEP = "PORTAL_ONLY_FIRST_STEP"


class BlockEvidenceKind(StrEnum):
    """What research established before a block may be created."""

    CONFIRMED_INCOMPATIBLE = "CONFIRMED_INCOMPATIBLE"
    SITE_UNAVAILABLE = "SITE_UNAVAILABLE"
    INCONCLUSIVE = "INCONCLUSIVE"


class CheckOutcome(StrEnum):
    BLOCKED = "BLOCKED"
    CLEAR = "CLEAR"


class ChallengeRefusal(StrEnum):
    SOURCE_ALREADY_CHECKED = "SOURCE_ALREADY_CHECKED"
    RECENT_CHALLENGE = "RECENT_CHALLENGE"
    NETWORK_LIMIT_REACHED = "NETWORK_LIMIT_REACHED"
    EMAIL_LIMIT_REACHED = "EMAIL_LIMIT_REACHED"
    DAILY_BUDGET_EXHAUSTED = "DAILY_BUDGET_EXHAUSTED"


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")


def _require_aware(value: object, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _require_count(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative int")


@dataclass(frozen=True)
class BlockKey:
    """A block is keyed by issuer, procedural stage and exact reason code."""

    issuer: str
    stage: str
    reason: BlockReason

    def __post_init__(self) -> None:
        _require_text(self.issuer, "issuer")
        _require_text(self.stage, "stage")
        if not isinstance(self.reason, BlockReason):
            raise ValueError("reason must be a BlockReason")


@dataclass(frozen=True)
class OfficialEvidence:
    """The quoted official text, its source and the date it was recorded."""

    quote: str
    source: str
    recorded_on: date

    def __post_init__(self) -> None:
        _require_text(self.quote, "quote")
        _require_text(self.source, "source")
        if not isinstance(self.recorded_on, date):
            raise ValueError("recorded_on must be a date")


@dataclass(frozen=True)
class ServiceabilityBlock:
    """One durable block: never created from failed research, lifted only on confirmation."""

    key: BlockKey
    quote: str
    source: str
    recorded_on: date
    created_at: datetime
    checked_sources: frozenset[str] = frozenset()
    last_challenge_at: datetime | None = None
    turned_away_count: int = 0
    lifted: bool = False
    lifted_at: datetime | None = None
    lift_quote: str = ""
    lift_source: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.key, BlockKey):
            raise ValueError("key must be a BlockKey")
        _require_text(self.quote, "quote")
        _require_text(self.source, "source")
        if not isinstance(self.recorded_on, date):
            raise ValueError("recorded_on must be a date")
        _require_aware(self.created_at, "created_at")
        if self.last_challenge_at is not None:
            _require_aware(self.last_challenge_at, "last_challenge_at")
        _require_count(self.turned_away_count, "turned_away_count")
        if not isinstance(self.checked_sources, frozenset):
            raise ValueError("checked_sources must be a frozenset")
        for checked in self.checked_sources:
            _require_text(checked, "checked_sources entry")
        if type(self.lifted) is not bool:
            raise ValueError("lifted must be a bool")
        if self.lifted:
            if self.lifted_at is None:
                raise ValueError("a lifted block requires lifted_at")
            _require_text(self.lift_quote, "lift_quote")
            _require_text(self.lift_source, "lift_source")
        elif self.lifted_at is not None or self.lift_quote or self.lift_source:
            raise ValueError("an unlifted block carries no lift record")
        if self.lifted_at is not None:
            _require_aware(self.lifted_at, "lifted_at")


@dataclass(frozen=True)
class BlockNotice:
    """The immediate, uncharged notice shown when a later ticket hits a block."""

    reason: BlockReason
    quote: str
    source: str
    recorded_on: date

    def __post_init__(self) -> None:
        if not isinstance(self.reason, BlockReason):
            raise ValueError("reason must be a BlockReason")
        _require_text(self.quote, "quote")
        _require_text(self.source, "source")
        if not isinstance(self.recorded_on, date):
            raise ValueError("recorded_on must be a date")


@dataclass(frozen=True)
class ServiceabilityCheck:
    """Whether a ticket may proceed to research, and the notice when it may not."""

    outcome: CheckOutcome
    block: ServiceabilityBlock | None = None
    notice: BlockNotice | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, CheckOutcome):
            raise ValueError("outcome must be a CheckOutcome")
        if self.outcome is CheckOutcome.BLOCKED:
            if self.block is None or self.notice is None:
                raise ValueError("a blocked check requires a block and its notice")
        elif self.block is not None or self.notice is not None:
            raise ValueError("a clear check carries no block or notice")

    @property
    def research_spend_allowed(self) -> bool:
        return self.outcome is CheckOutcome.CLEAR

    @property
    def charge_allowed(self) -> bool:
        return self.outcome is CheckOutcome.CLEAR


@dataclass(frozen=True)
class CustomerLead:
    """A source the customer supplies to challenge a block. The note is their own words:
    carried for the record, never assessed and never authority."""

    source: str
    note: str = ""

    def __post_init__(self) -> None:
        _require_text(self.source, "source")
        if not isinstance(self.note, str):
            raise ValueError("note must be a string")


@dataclass(frozen=True)
class ChallengeResearch:
    """What the challenge's own research run found. Only an official source that confirms
    may lift a block; a quote echoed from the customer's lead is not confirmation."""

    official_source_confirms: bool = False
    quote: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        if type(self.official_source_confirms) is not bool:
            raise ValueError("official_source_confirms must be a bool")
        if not isinstance(self.quote, str) or not isinstance(self.source, str):
            raise ValueError("quote and source must be strings")
        if self.official_source_confirms and (not self.quote.strip() or not self.source.strip()):
            raise ValueError("official_source_confirms requires the confirming quote and source")


@dataclass(frozen=True)
class ChallengeLimits:
    """The global daily challenge budget and its per-email/per-network limits."""

    daily_global: int
    daily_per_email: int
    daily_per_network: int

    def __post_init__(self) -> None:
        _require_count(self.daily_global, "daily_global")
        _require_count(self.daily_per_email, "daily_per_email")
        _require_count(self.daily_per_network, "daily_per_network")


@dataclass(frozen=True)
class ChallengeUsage:
    """Challenges already run today, counted by the caller: global, one email, one network."""

    daily_global: int = 0
    daily_per_email: int = 0
    daily_per_network: int = 0

    def __post_init__(self) -> None:
        _require_count(self.daily_global, "daily_global")
        _require_count(self.daily_per_email, "daily_per_email")
        _require_count(self.daily_per_network, "daily_per_network")


@dataclass(frozen=True)
class ChallengeDecision:
    admitted: bool
    refusal: ChallengeRefusal | None = None

    def __post_init__(self) -> None:
        if type(self.admitted) is not bool:
            raise ValueError("admitted must be a bool")
        if self.admitted == (self.refusal is not None):
            raise ValueError("admitted and refusal must agree")


def create_block(
    key: BlockKey,
    evidence_kind: BlockEvidenceKind,
    evidence: OfficialEvidence | None,
    *,
    created_at: datetime,
) -> ServiceabilityBlock | None:
    """Create a block only from confirmed official evidence of incompatibility.

    An unavailable site or inconclusive research never creates a block: those stop that
    one ticket without charge and leave no durable block behind.
    """
    if not isinstance(key, BlockKey):
        raise ValueError("key must be a BlockKey")
    if not isinstance(evidence_kind, BlockEvidenceKind):
        raise ValueError("evidence_kind must be a BlockEvidenceKind")
    _require_aware(created_at, "created_at")
    if evidence_kind is not BlockEvidenceKind.CONFIRMED_INCOMPATIBLE:
        return None
    if evidence is None:
        raise ValueError("a confirmed block requires its official evidence")
    return ServiceabilityBlock(
        key=key,
        quote=evidence.quote,
        source=evidence.source,
        recorded_on=evidence.recorded_on,
        created_at=created_at,
        checked_sources=frozenset({evidence.source}),
    )


def check_serviceability(
    blocks: Iterable[ServiceabilityBlock], issuer: str, stage: str
) -> ServiceabilityCheck:
    """Look up a live block for this issuer and stage before any research spend."""
    _require_text(issuer, "issuer")
    _require_text(stage, "stage")
    for block in blocks:
        if not isinstance(block, ServiceabilityBlock):
            raise ValueError("blocks must hold ServiceabilityBlock values")
        if block.lifted:
            continue
        if block.key.issuer != issuer or not same_stage(block.key.stage, stage):
            continue
        notice = BlockNotice(
            reason=block.key.reason,
            quote=block.quote,
            source=block.source,
            recorded_on=block.recorded_on,
        )
        return ServiceabilityCheck(outcome=CheckOutcome.BLOCKED, block=block, notice=notice)
    return ServiceabilityCheck(outcome=CheckOutcome.CLEAR)


def record_turn_away(block: ServiceabilityBlock) -> ServiceabilityBlock:
    """Count one more ticket turned away by this block. Pure: the input is unchanged."""
    if not isinstance(block, ServiceabilityBlock):
        raise ValueError("block must be a ServiceabilityBlock")
    if block.lifted:
        raise ValueError("cannot record a turn-away on a lifted block")
    return replace(block, turned_away_count=block.turned_away_count + 1)


def admit_challenge(
    block: ServiceabilityBlock,
    lead: CustomerLead,
    *,
    now: datetime,
    limits: ChallengeLimits,
    usage: ChallengeUsage,
) -> ChallengeDecision:
    """Decide whether a challenge may run its automatic research.

    A challenge runs only on a source not previously checked, at most once per block per
    week, and within the daily budget and per-email/per-network limits.
    """
    if not isinstance(block, ServiceabilityBlock):
        raise ValueError("block must be a ServiceabilityBlock")
    if not isinstance(lead, CustomerLead):
        raise ValueError("lead must be a CustomerLead")
    _require_aware(now, "now")
    if not isinstance(limits, ChallengeLimits):
        raise ValueError("limits must be a ChallengeLimits")
    if not isinstance(usage, ChallengeUsage):
        raise ValueError("usage must be a ChallengeUsage")
    if lead.source in block.checked_sources:
        return ChallengeDecision(admitted=False, refusal=ChallengeRefusal.SOURCE_ALREADY_CHECKED)
    if block.last_challenge_at is not None and now - block.last_challenge_at < CHALLENGE_INTERVAL:
        return ChallengeDecision(admitted=False, refusal=ChallengeRefusal.RECENT_CHALLENGE)
    if usage.daily_per_network >= limits.daily_per_network:
        return ChallengeDecision(admitted=False, refusal=ChallengeRefusal.NETWORK_LIMIT_REACHED)
    if usage.daily_per_email >= limits.daily_per_email:
        return ChallengeDecision(admitted=False, refusal=ChallengeRefusal.EMAIL_LIMIT_REACHED)
    if usage.daily_global >= limits.daily_global:
        return ChallengeDecision(admitted=False, refusal=ChallengeRefusal.DAILY_BUDGET_EXHAUSTED)
    return ChallengeDecision(admitted=True)


def resolve_challenge(
    block: ServiceabilityBlock,
    lead: CustomerLead,
    research: ChallengeResearch,
    *,
    at: datetime,
) -> ServiceabilityBlock:
    """Record a finished challenge and apply the lift rule.

    The lead's source and any source the research checked become previously checked; the
    weekly window restarts. The block lifts only when the official source confirms.
    """
    if not isinstance(block, ServiceabilityBlock):
        raise ValueError("block must be a ServiceabilityBlock")
    if not isinstance(lead, CustomerLead):
        raise ValueError("lead must be a CustomerLead")
    if not isinstance(research, ChallengeResearch):
        raise ValueError("research must be a ChallengeResearch")
    _require_aware(at, "at")
    checked = set(block.checked_sources)
    checked.add(lead.source)
    if research.source.strip():
        checked.add(research.source)
    if not research.official_source_confirms:
        return replace(block, checked_sources=frozenset(checked), last_challenge_at=at)
    return replace(
        block,
        checked_sources=frozenset(checked),
        last_challenge_at=at,
        lifted=True,
        lifted_at=at,
        lift_quote=research.quote,
        lift_source=research.source,
    )
