"""Anonymous milestone rows. Transactions write; analysis alone reads and computes rates.

Each (submitted request, milestone) is emitted once by the durable transaction outbox.
REQUEST_RECEIVED includes unresolved, pre-research intakes. Only an explicit jurisdiction
exclusion leaves the research denominator; neither research failure nor another scope exit
can do so. This module never supplies a transaction decision.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields

from parkingbreaker.case_state import HardStop, StopFamily
from parkingbreaker.oplog.digest import (
    CounterMilestone,
    DigestOutcome,
    JurisdictionExclusion,
    LogDigest,
    PhaseReached,
    ResearchStatus,
    _check_enum,
    _check_token,
)


@dataclass(frozen=True)
class CounterKey:
    issuer_token: str = "unknown"
    basis_key_token: str = "unknown"
    ticket_type: str = "unknown"
    stage_token: str = "unknown"
    outcome: DigestOutcome | None = None
    hard_stop: HardStop | None = None
    milestone: CounterMilestone = CounterMilestone.REQUEST_RECEIVED
    jurisdiction_exclusion: JurisdictionExclusion | None = None

    def __post_init__(self) -> None:
        for name in ("issuer_token", "basis_key_token", "ticket_type", "stage_token"):
            _check_token(name, getattr(self, name))
        _check_enum("milestone", self.milestone, CounterMilestone)
        if self.outcome is not None and self.outcome != "SERVICEABLE":
            _check_enum("outcome", self.outcome, StopFamily)
        if self.hard_stop is not None:
            _check_enum("hard_stop", self.hard_stop, HardStop)
        if self.jurisdiction_exclusion is not None:
            _check_enum(
                "jurisdiction_exclusion", self.jurisdiction_exclusion, JurisdictionExclusion
            )
        if self.milestone is CounterMilestone.GENUINELY_UNSUPPORTED:
            expected = (
                StopFamily.NOT_YET_VERIFIED
                if self.jurisdiction_exclusion is JurisdictionExclusion.UNRECORDED_BASIS
                else StopFamily.BUSINESS_EXCLUSION
            )
            if self.jurisdiction_exclusion is None or self.outcome is not expected:
                raise ValueError(
                    "jurisdiction_exclusion: explicit service-policy exclusion required"
                )
        elif self.jurisdiction_exclusion is not None:
            raise ValueError("jurisdiction_exclusion: only valid for genuinely unsupported")
        if (
            self.milestone is CounterMilestone.REQUIRED_FACTS_UNVERIFIED
            and self.outcome is not StopFamily.NOT_YET_VERIFIED
        ):
            raise ValueError("outcome: research failure must remain NOT_YET_VERIFIED")
        if (
            self.milestone in {CounterMilestone.TIER1_PASSED, CounterMilestone.APPEAL_COMPLETED}
            and self.outcome != "SERVICEABLE"
        ):
            raise ValueError("outcome: passed/completed requires SERVICEABLE")

    def to_json(self) -> str:
        """Explicit dataclass allowlist, never a case identifier or customer content."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


def decode_counter_key(payload: str) -> CounterKey:
    data = json.loads(payload)
    if not isinstance(data, dict) or set(data) != {field.name for field in fields(CounterKey)}:
        raise ValueError("counter: unexpected fields")
    data["milestone"] = CounterMilestone(data["milestone"])
    if data["outcome"] not in (None, "SERVICEABLE"):
        data["outcome"] = StopFamily(data["outcome"])
    if data["hard_stop"] is not None:
        data["hard_stop"] = HardStop(data["hard_stop"])
    if data["jurisdiction_exclusion"] is not None:
        data["jurisdiction_exclusion"] = JurisdictionExclusion(data["jurisdiction_exclusion"])
    return CounterKey(**data)


def counter_keys(digest: LogDigest) -> tuple[CounterKey, ...]:
    """Cumulative milestones for one request; the outbox deduplicates each milestone.

    Passing Tier 1 is explicit, never inferred from SERVICEABLE or a missing hard stop.
    MAILED means first confirmed Lob acceptance, rather than delivery or webhook count.
    """
    milestones = [CounterMilestone.REQUEST_RECEIVED]
    if digest.jurisdiction_exclusion is not None:
        milestones.append(CounterMilestone.GENUINELY_UNSUPPORTED)
    elif digest.research_status is ResearchStatus.REQUIRED_FACTS_UNVERIFIED:
        milestones.append(CounterMilestone.REQUIRED_FACTS_UNVERIFIED)
    elif digest.research_status is ResearchStatus.TIER1_PASSED:
        milestones.append(CounterMilestone.TIER1_PASSED)
    if (
        digest.outcome != "SERVICEABLE"
        and digest.jurisdiction_exclusion is None
        and digest.research_status is not ResearchStatus.REQUIRED_FACTS_UNVERIFIED
    ):
        milestones.append(CounterMilestone.OTHER_EXIT)
    if digest.phase_reached is PhaseReached.MAILED:
        milestones.append(CounterMilestone.APPEAL_COMPLETED)
    return tuple(
        CounterKey(
            digest.issuer_token,
            digest.basis_key_token,
            digest.ticket_type,
            digest.stage_token if digest.stage_token is not None else "unknown",
            "SERVICEABLE"
            if milestone in {CounterMilestone.TIER1_PASSED, CounterMilestone.APPEAL_COMPLETED}
            else StopFamily.NOT_YET_VERIFIED
            if milestone is CounterMilestone.REQUIRED_FACTS_UNVERIFIED
            else digest.outcome,
            digest.hard_stop,
            milestone,
            digest.jurisdiction_exclusion
            if milestone is CounterMilestone.GENUINELY_UNSUPPORTED
            else None,
        )
        for milestone in milestones
    )
