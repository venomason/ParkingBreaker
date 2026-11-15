"""Labelled synthetic route contracts through actual research, extraction and SQLite.

These are canned agency-style pages/model replies, not official agency evidence or
measured model accuracy. Only issuer discovery and provider I/O use existing fakes.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from parkingbreaker import case_state
from parkingbreaker.research.fakes import FakeExtractor, FakeFetcher
from parkingbreaker.research.findings import FindingStatus
from parkingbreaker.research.questions import ANSWERABLE_KEYS, FactKey
from parkingbreaker.research.resolution import ResearchAnswers, resolution_event
from parkingbreaker.research.run import RecoveryConfig, run_research
from parkingbreaker.research.sources import FetchResponse
from parkingbreaker.research_records import decode_research, research_json
from samples import source
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution
from test_research_records import result_fixture
from test_research_run import CONTEST_SEED, CONTEST_URL, TEST_AGENCY, TICKET, TickingClock
from test_t21_payment_scenarios import _case

pytestmark = pytest.mark.anyio

REQUIRED = (
    "You must register your initial review through the online portal before mailing your appeal.",
    "Before mailing your initial review, you must upload "
    "the signed contest form through the portal.",
    "You must attend an in-person appointment before mailing your initial review.",
    "If mailing your initial review, you must first register in the online portal.",
)
OPTIONAL = (
    "For online submissions, uploads must be under two megabytes. "
    "You may instead mail your review and all attachments.",
    "Online submissions must include PDF evidence. You may instead mail your review.",
    "If you submit your initial review online, you must submit your request electronically.",
    "When submitting online, you must submit your initial review electronically.",
    "You must submit your appeal online or by mail.",
    "Your appeal must be filed by mail or electronically.",
    "If you choose an in-person hearing, you must appear in person.",
    "If you choose to appeal in person, you must attend the hearing.",
    "If you choose to file online, you must sign the portal form.",
    "You must not submit your appeal online.",
    "You must submit your initial review by mail and can pay your fine online.",
)


@pytest.mark.parametrize("quote", REQUIRED)
async def test_j05_j28_supported_route_prerequisite_is_quoted_and_durable(
    tmp_path: Path, quote: str
) -> None:
    current = await _case(tmp_path, "first-level review", quote, None)
    try:
        current.reopen()
        status = current.real.status(current.case)
        assert status.state == "STOPPED_NO_CHARGE", status
        assert status.payment_state == "RELEASED"
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        research = decode_research(saved.research_json)
        for completed in (None, True, False):
            event = resolution_event(
                research.resolve(ResearchAnswers(initial_review_completed=completed))
            )
            assert isinstance(event, case_state.Stopped)
            assert event.reason is case_state.StopReason.PROCESS_INCOMPATIBLE
            assert quote in event.detail
        assert research_json(research) == saved.research_json
        assert len(current.model.calls) == 1
    finally:
        current.real._store.close()


@pytest.mark.parametrize("quote", OPTIONAL)
async def test_j27_j30_optional_route_conditions_do_not_refuse_mail(
    tmp_path: Path, quote: str
) -> None:
    current = await _case(tmp_path, "first-level review", quote, None)
    try:
        current.reopen()
        status = current.real.status(current.case)
        assert status.state == "DRAFTING", status
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        research = decode_research(saved.research_json)
        assert research.resolve().research_complete
        assert len(research.findings) == len(ANSWERABLE_KEYS)
        assert len(current.model.calls) == 1
    finally:
        current.real._store.close()


@pytest.mark.parametrize(
    "quote",
    [
        "For online submissions, uploads must be under two megabytes. "
        "You must include agency form A.",
        "Online submissions must include PDF evidence, "
        "and mailed reviews must include agency form A.",
    ],
)
async def test_optional_channel_cannot_erase_a_global_mail_form(tmp_path: Path, quote: str) -> None:
    current = await _case(tmp_path, "first-level review", quote, None)
    try:
        current.reopen()
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        research = decode_research(saved.research_json)
        form = next(f for f in research.findings if f.key is FactKey.MANDATORY_FORMS)
        assert form.status is FindingStatus.CONFIRMED
        assert "agency form A" in str(form.value)
        assert not research.resolve().ready
    finally:
        current.real._store.close()


async def test_ticket_fact_condition_never_becomes_permission_or_proved_incompatibility(
    tmp_path: Path,
) -> None:
    quote = (
        "If the citation was issued electronically, you must register your initial review "
        "through the online portal before mailing your appeal."
    )
    current = await _case(tmp_path, "first-level review", quote, None)
    try:
        current.reopen()
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        event = resolution_event(decode_research(saved.research_json).resolve())
        assert isinstance(event, case_state.Stopped)
        assert event.reason is case_state.StopReason.NOT_YET_VERIFIED
    finally:
        current.real._store.close()


def test_required_route_cannot_borrow_another_stage_heading() -> None:
    original = result_fixture()
    page = source(
        "San Francisco SFMTA Parking Citation Administrative Hearing.\n"
        "You must register your initial review through the online portal "
        "before mailing your appeal."
    )
    current = replace(original, sources=(*original.sources, page)).reassess()
    event = resolution_event(current.resolve())
    assert not (
        isinstance(event, case_state.Stopped)
        and event.reason is case_state.StopReason.PROCESS_INCOMPATIBLE
    )


def test_j06_blocked_website_stays_unverified_without_incompatibility_claim() -> None:
    fetcher = FakeFetcher({CONTEST_URL: FetchResponse(CONTEST_URL, 403, {}, b"")})
    current = run_research(
        TICKET,
        TEST_AGENCY,
        [CONTEST_SEED],
        fetcher,
        FakeExtractor(lambda _: []),
        TickingClock(),
        wait=lambda _: None,
        recovery=RecoveryConfig(retry_delays=()),
    )
    saved = decode_research(research_json(current))
    event = resolution_event(saved.resolve())
    assert isinstance(event, case_state.Stopped)
    assert event.reason is case_state.StopReason.NOT_YET_VERIFIED
    assert saved.failures
    assert not saved.resolve().ready
