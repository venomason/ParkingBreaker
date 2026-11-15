"""Contract tests for durable serviceability blocks and bounded customer challenges (T21 W9).

Each named test below is one clause of the owner decision in docs/architecture-v8.md 20.18:
blocks short-circuit later tickets before research spend with an uncharged notice; an
unavailable site or inconclusive research never creates a block; a challenge runs research
only on an unchecked source, at most once per block per week, within the challenge budget;
the block lifts only when the official source confirms; the customer's evidence is a lead,
never authority; turned-away volume is counted per block.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from parkingbreaker.serviceability import (
    BlockEvidenceKind,
    BlockKey,
    BlockNotice,
    BlockReason,
    ChallengeLimits,
    ChallengeRefusal,
    ChallengeResearch,
    ChallengeUsage,
    CheckOutcome,
    CustomerLead,
    OfficialEvidence,
    ServiceabilityBlock,
    admit_challenge,
    check_serviceability,
    create_block,
    record_turn_away,
    resolve_challenge,
)

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
RECORDED_ON = date(2026, 10, 20)
ISSUER = "us-wa-seattle"
STAGE = "first-level review"
QUOTE = "Any person contesting a citation must appear in person for the first review."
SOURCE = "https://seattle.gov/municipal-court/parking/citations"
OTHER_SOURCE = "https://seattle.gov/municipal-court/parking/forms"
THIRD_SOURCE = "https://seattle.gov/municipal-court/parking/fees"

LIMITS = ChallengeLimits(daily_global=10, daily_per_email=5, daily_per_network=5)
NO_USAGE = ChallengeUsage()


def official_block(
    *, reason: BlockReason = BlockReason.IN_PERSON_ONLY_FIRST_STEP, created_at: datetime = NOW
) -> ServiceabilityBlock:
    block = create_block(
        BlockKey(ISSUER, STAGE, reason),
        BlockEvidenceKind.CONFIRMED_INCOMPATIBLE,
        OfficialEvidence(QUOTE, SOURCE, RECORDED_ON),
        created_at=created_at,
    )
    assert block is not None
    return block


def test_block_short_circuits_before_research_spend() -> None:
    block = official_block()
    check = check_serviceability([block], ISSUER, STAGE)
    assert check.outcome is CheckOutcome.BLOCKED
    assert check.block == block
    assert check.notice == BlockNotice(
        reason=BlockReason.IN_PERSON_ONLY_FIRST_STEP,
        quote=QUOTE,
        source=SOURCE,
        recorded_on=RECORDED_ON,
    )
    assert check.research_spend_allowed is False
    assert check.charge_allowed is False
    clear = check_serviceability([block], ISSUER, "administrative hearing")
    assert clear.outcome is CheckOutcome.CLEAR
    assert clear.research_spend_allowed is True


def test_unavailable_site_never_creates_a_block() -> None:
    key = BlockKey(ISSUER, STAGE, BlockReason.IN_PERSON_ONLY_FIRST_STEP)
    official = OfficialEvidence(QUOTE, SOURCE, RECORDED_ON)
    with_evidence = create_block(key, BlockEvidenceKind.SITE_UNAVAILABLE, official, created_at=NOW)
    assert with_evidence is None
    without_evidence = create_block(key, BlockEvidenceKind.SITE_UNAVAILABLE, None, created_at=NOW)
    assert without_evidence is None


def test_inconclusive_research_never_creates_a_block() -> None:
    key = BlockKey(ISSUER, STAGE, BlockReason.IN_PERSON_ONLY_FIRST_STEP)
    official = OfficialEvidence(QUOTE, SOURCE, RECORDED_ON)
    with_evidence = create_block(key, BlockEvidenceKind.INCONCLUSIVE, official, created_at=NOW)
    assert with_evidence is None
    without_evidence = create_block(key, BlockEvidenceKind.INCONCLUSIVE, None, created_at=NOW)
    assert without_evidence is None


def test_challenge_requires_an_unchecked_source() -> None:
    block = official_block()
    repeat = admit_challenge(
        block, CustomerLead(source=SOURCE), now=NOW, limits=LIMITS, usage=NO_USAGE
    )
    assert repeat.admitted is False
    assert repeat.refusal is ChallengeRefusal.SOURCE_ALREADY_CHECKED
    fresh = admit_challenge(
        block, CustomerLead(source=OTHER_SOURCE), now=NOW, limits=LIMITS, usage=NO_USAGE
    )
    assert fresh.admitted is True
    resolved = resolve_challenge(
        block, CustomerLead(source=OTHER_SOURCE), ChallengeResearch(), at=NOW
    )
    after = admit_challenge(
        resolved,
        CustomerLead(source=OTHER_SOURCE),
        now=NOW + timedelta(days=8),
        limits=LIMITS,
        usage=NO_USAGE,
    )
    assert after.admitted is False
    assert after.refusal is ChallengeRefusal.SOURCE_ALREADY_CHECKED


def test_at_most_one_challenge_per_block_per_week() -> None:
    block = official_block()
    first = admit_challenge(
        block, CustomerLead(source=OTHER_SOURCE), now=NOW, limits=LIMITS, usage=NO_USAGE
    )
    assert first.admitted is True
    challenged = resolve_challenge(
        block, CustomerLead(source=OTHER_SOURCE), ChallengeResearch(), at=NOW
    )
    soon_lead = CustomerLead(source=THIRD_SOURCE)
    soon = admit_challenge(
        challenged,
        soon_lead,
        now=NOW + timedelta(days=7) - timedelta(seconds=1),
        limits=LIMITS,
        usage=NO_USAGE,
    )
    assert soon.admitted is False
    assert soon.refusal is ChallengeRefusal.RECENT_CHALLENGE
    later = admit_challenge(
        challenged, soon_lead, now=NOW + timedelta(days=7), limits=LIMITS, usage=NO_USAGE
    )
    assert later.admitted is True


def test_block_lifts_only_when_the_official_source_confirms() -> None:
    block = official_block()
    lead = CustomerLead(source=OTHER_SOURCE)
    unresolved = resolve_challenge(block, lead, ChallengeResearch(), at=NOW)
    assert unresolved.lifted is False
    assert unresolved.lifted_at is None
    assert check_serviceability([unresolved], ISSUER, STAGE).outcome is CheckOutcome.BLOCKED
    lifted = resolve_challenge(
        block,
        lead,
        ChallengeResearch(
            official_source_confirms=True,
            quote="Mail-in first review is accepted for parking citations.",
            source=OTHER_SOURCE,
        ),
        at=NOW,
    )
    assert lifted.lifted is True
    assert lifted.lifted_at == NOW
    assert lifted.lift_source == OTHER_SOURCE
    assert check_serviceability([lifted], ISSUER, STAGE).outcome is CheckOutcome.CLEAR
    with pytest.raises(ValueError, match="quote and source"):
        ChallengeResearch(official_source_confirms=True)


def test_customer_evidence_is_a_lead_not_authority() -> None:
    block = official_block()
    lead = CustomerLead(source=OTHER_SOURCE, note="The clerk said my case is serviceable now.")
    decision = admit_challenge(block, lead, now=NOW, limits=LIMITS, usage=NO_USAGE)
    assert decision.admitted is True
    echoed = resolve_challenge(
        block, lead, ChallengeResearch(quote=lead.note, source=lead.source), at=NOW
    )
    assert echoed.lifted is False
    assert echoed.lifted_at is None
    assert check_serviceability([echoed], ISSUER, STAGE).outcome is CheckOutcome.BLOCKED
    assert OTHER_SOURCE in echoed.checked_sources


def test_turned_away_volume_is_counted() -> None:
    block = official_block()
    first = record_turn_away(block)
    second = record_turn_away(first)
    assert block.turned_away_count == 0
    assert first.turned_away_count == 1
    assert second.turned_away_count == 2
    other = official_block(reason=BlockReason.PORTAL_ONLY_FIRST_STEP)
    assert record_turn_away(other).turned_away_count == 1
    lifted = resolve_challenge(
        block,
        CustomerLead(source=OTHER_SOURCE),
        ChallengeResearch(
            official_source_confirms=True,
            quote="Mail-in first review is accepted for parking citations.",
            source=OTHER_SOURCE,
        ),
        at=NOW,
    )
    with pytest.raises(ValueError, match="lifted"):
        record_turn_away(lifted)


def test_challenge_budget_and_identity_limits_are_enforced() -> None:
    block = official_block()
    lead = CustomerLead(source=OTHER_SOURCE)
    limits = ChallengeLimits(daily_global=3, daily_per_email=2, daily_per_network=1)
    network_exhausted = ChallengeUsage(daily_global=1, daily_per_email=1, daily_per_network=1)
    network = admit_challenge(block, lead, now=NOW, limits=limits, usage=network_exhausted)
    assert network.admitted is False
    assert network.refusal is ChallengeRefusal.NETWORK_LIMIT_REACHED
    email_exhausted = ChallengeUsage(daily_global=1, daily_per_email=2, daily_per_network=0)
    email = admit_challenge(block, lead, now=NOW, limits=limits, usage=email_exhausted)
    assert email.admitted is False
    assert email.refusal is ChallengeRefusal.EMAIL_LIMIT_REACHED
    global_exhausted = ChallengeUsage(daily_global=3, daily_per_email=1, daily_per_network=0)
    daily = admit_challenge(block, lead, now=NOW, limits=limits, usage=global_exhausted)
    assert daily.admitted is False
    assert daily.refusal is ChallengeRefusal.DAILY_BUDGET_EXHAUSTED
