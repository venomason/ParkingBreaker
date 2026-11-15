"""Contract tests for the 403 + archive fallback (architecture-v8 16.2, W10).

The five names are the plan's contract tests. The decision logic is pure and
offline: callers pass the block, the archived snapshot, the customer-confirmed
ticket back and today's date in.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from parkingbreaker.research.archive import (
    ArchiveFallbackDecision,
    ArchiveOutcome,
    ArchiveSnapshot,
    ArchiveStopCause,
    FactProvenance,
    LiveSiteBlock,
    TicketBackFacts,
    TicketBackField,
    blocked_site,
    decide_archive_fallback,
)
from parkingbreaker.research.fakes import FakeExtractor, FakeFetcher, html_response
from parkingbreaker.research.run import run_research
from parkingbreaker.research.sources import FetchFailure
from test_research_run import (
    CONTEST_SEED,
    CONTEST_URL,
    TEST_AGENCY,
    TICKET,
    TickingClock,
)

TODAY = date(2026, 10, 22)
SITE = "https://www.sfmta.com/appeals"

BLOCK = LiveSiteBlock(site=SITE, http_status=403)

TICKET_BACK = TicketBackFacts(
    mailing_address="SFMTA Customer Service Center, 1160 Mission St, San Francisco, CA 94103",
    stage="first-level review",
    deadline="21 days from the notice date",
)

DISCLOSURE = (
    "Verified by matching your ticket against the most recent archived copy of "
    f"{SITE}; the live site blocks automated access."
)


def matching_snapshot(captured_on: date) -> ArchiveSnapshot:
    return ArchiveSnapshot(
        site=SITE,
        captured_on=captured_on,
        mailing_address=TICKET_BACK.mailing_address,
        stage=TICKET_BACK.stage,
        deadline=TICKET_BACK.deadline,
    )


def decide(
    snapshot: ArchiveSnapshot | None,
    ticket_back: TicketBackFacts | None = TICKET_BACK,
) -> ArchiveFallbackDecision:
    return decide_archive_fallback(
        block=BLOCK,
        snapshot=snapshot,
        ticket_back=ticket_back,
        today=TODAY,
    )


def test_403_is_never_process_incompatible() -> None:
    # A blocked official site is an access condition, not a process verdict:
    # PROCESS_INCOMPATIBLE is not even a possible outcome, and no block is created.
    assert set(ArchiveOutcome) == {ArchiveOutcome.VERIFIED, ArchiveOutcome.NOT_YET_VERIFIED}
    scenarios: list[ArchiveFallbackDecision] = [
        decide(None),
        decide(matching_snapshot(TODAY - timedelta(days=200))),
        decide(
            ArchiveSnapshot(
                site=SITE,
                captured_on=TODAY - timedelta(days=1),
                mailing_address="Some Other Office, 1 Market St, San Francisco, CA 94105",
                stage=TICKET_BACK.stage,
                deadline=TICKET_BACK.deadline,
            )
        ),
        decide(matching_snapshot(TODAY - timedelta(days=10)), ticket_back=None),
        decide(matching_snapshot(TODAY - timedelta(days=10))),
    ]
    for decision in scenarios:
        assert decision.outcome in {ArchiveOutcome.VERIFIED, ArchiveOutcome.NOT_YET_VERIFIED}
        assert decision.block is None
    # A qualifying snapshot verifies; the 403 alone never stops or blocks the case.
    assert scenarios[-1].outcome is ArchiveOutcome.VERIFIED
    assert scenarios[-1].stop_cause is None


def test_snapshot_older_than_ninety_days_stops_not_yet_verified() -> None:
    stale = decide(matching_snapshot(TODAY - timedelta(days=91)))
    assert stale.outcome is ArchiveOutcome.NOT_YET_VERIFIED
    assert stale.stop_cause is ArchiveStopCause.STALE_SNAPSHOT
    assert stale.facts == ()
    assert stale.disclosure is None


def test_any_mismatch_stops_not_yet_verified() -> None:
    fresh = matching_snapshot(TODAY - timedelta(days=10))
    for mismatched in (
        replace(fresh, mailing_address="Some Other Office, 1 Market St, San Francisco, CA 94105"),
        replace(fresh, stage="hearing officer review"),
        replace(fresh, deadline="10 days from the citation"),
    ):
        decision = decide(mismatched)
        assert decision.outcome is ArchiveOutcome.NOT_YET_VERIFIED
        assert decision.stop_cause is ArchiveStopCause.FACT_MISMATCH
        assert decision.facts == ()
        assert decision.disclosure is None


def test_customer_ticket_back_is_labelled_customer_provided() -> None:
    decision = decide(matching_snapshot(TODAY - timedelta(days=10)))
    assert decision.outcome is ArchiveOutcome.VERIFIED
    assert {fact.field for fact in decision.facts} == set(TicketBackField)
    for fact in decision.facts:
        # Customer-provided, never official.
        assert fact.provenance is FactProvenance.CUSTOMER_PROVIDED
    values = {fact.field: fact.value for fact in decision.facts}
    assert values[TicketBackField.MAILING_ADDRESS] == TICKET_BACK.mailing_address
    assert values[TicketBackField.STAGE] == TICKET_BACK.stage
    assert values[TicketBackField.DEADLINE] == TICKET_BACK.deadline


def test_live_site_block_is_disclosed_verbatim() -> None:
    decision = decide(matching_snapshot(TODAY - timedelta(days=10)))
    assert decision.outcome is ArchiveOutcome.VERIFIED
    assert decision.disclosure == DISCLOSURE


def test_snapshot_at_the_ninety_day_boundary_still_qualifies() -> None:
    edge = decide(matching_snapshot(TODAY - timedelta(days=90)))
    assert edge.outcome is ArchiveOutcome.VERIFIED
    just_over = decide(matching_snapshot(TODAY - timedelta(days=91)))
    assert just_over.outcome is ArchiveOutcome.NOT_YET_VERIFIED
    assert just_over.stop_cause is ArchiveStopCause.STALE_SNAPSHOT
    from_the_future = decide(matching_snapshot(TODAY + timedelta(days=1)))
    assert from_the_future.outcome is ArchiveOutcome.NOT_YET_VERIFIED


def test_missing_piece_stops_not_yet_verified() -> None:
    fresh = matching_snapshot(TODAY - timedelta(days=10))
    assert decide(None).stop_cause is ArchiveStopCause.MISSING_PIECE
    assert decide(fresh, ticket_back=None).stop_cause is ArchiveStopCause.MISSING_PIECE
    assert decide(replace(fresh, deadline=None)).stop_cause is ArchiveStopCause.MISSING_PIECE
    assert decide(replace(fresh, stage="   ")).stop_cause is ArchiveStopCause.MISSING_PIECE
    blank_back = replace(TICKET_BACK, mailing_address="")
    decision = decide(fresh, ticket_back=blank_back)
    assert decision.outcome is ArchiveOutcome.NOT_YET_VERIFIED
    assert decision.stop_cause is ArchiveStopCause.MISSING_PIECE


def test_blocked_site_sees_only_the_403() -> None:
    assert blocked_site(FetchFailure(SITE, "HTTP 403 at https://www.sfmta.com/live")) == (
        LiveSiteBlock(site=SITE, http_status=403)
    )
    assert blocked_site(FetchFailure(SITE, "HTTP 404 at https://www.sfmta.com/live")) is None
    assert blocked_site(FetchFailure(SITE, "fetch error at x: boom")) is None


def test_run_research_records_a_403_as_a_live_site_block() -> None:
    fake_fetcher = FakeFetcher(
        {CONTEST_URL: html_response(CONTEST_URL, "<html></html>", status=403)}
    )
    result = run_research(
        TICKET,
        TEST_AGENCY,
        (CONTEST_SEED,),
        fake_fetcher,
        FakeExtractor(lambda _sources: ()),
        TickingClock(),
        wait=lambda _: None,
    )
    assert any(
        attempt.stage == "live_site_block" and attempt.url == CONTEST_URL
        for attempt in result.attempts
    )
    assert len(result.failures) == 1
    assert blocked_site(result.failures[0]) == LiveSiteBlock(site=CONTEST_URL, http_status=403)
