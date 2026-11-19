"""T21: the untested seams of cross-customer research reuse (architecture-v8 20.18).

Reuse must refuse and let the ticket be researched again when the refetched page's
content type differs from the saved one, when the refetch cannot fit in its time
budget, or when the lookup only finds a malformed foreign record. The lookup itself
is bounded to ten records however many other cases saved research recently.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker import case_service, prehold
from parkingbreaker.api.app import create_app
from parkingbreaker.api.auth import Customer
from parkingbreaker.case_service import CaseService, ResearchDeps
from parkingbreaker.research.fakes import FakeExtractor, FakeFetcher
from parkingbreaker.research.findings import ReleaseDecision
from parkingbreaker.research.questions import Ticket
from parkingbreaker.research.run import ResearchResult, reuse_research, run_research
from parkingbreaker.research.sources import FetchResponse
from parkingbreaker.research_records import decode_research, research_json
from samples import fixture as sample_page
from test_case_service import (
    BOB,
    INTAKE,
    NOW,
    Resolved,
    service,
    stripe,
)
from test_case_service import (
    anyio_backend as anyio_backend,
)
from test_research_run import (
    CONTEST_URL,
    SEEDS,
    TEST_AGENCY,
    answers,
    fetcher,
)
from test_stripe_payment import fixture as stripe_fixture
from test_t21_research_reuse import (
    OTHER,
    attempt,
    authorize_and_research,
    first_research,
    submit_as,
)


class JumpingClock:
    """A clock that advances a fixed step on every reading."""

    def __init__(self, start: datetime, step: timedelta) -> None:
        self.moment = start
        self.step = step

    def __call__(self) -> datetime:
        self.moment += self.step
        return self.moment


def test_content_type_mismatch_researches_again() -> None:
    saved, _ = first_research()
    served_as_plain_text = FetchResponse(
        url=CONTEST_URL,
        status=200,
        headers={"content-type": "text/plain; charset=utf-8"},
        body=sample_page("sfmta_contest.html").encode("utf-8"),
    )
    transport = FakeFetcher({**fetcher().responses, CONTEST_URL: served_as_plain_text})
    assert attempt(saved, OTHER, 2, transport=transport) is None
    assert attempt(saved, OTHER, 2) is not None  # the identical refetch still reuses


def test_refetch_time_budget_abort_researches_again() -> None:
    saved, _ = first_research()
    now = saved.started_at + timedelta(hours=2)
    assert (
        reuse_research(
            [saved],
            OTHER,
            TEST_AGENCY,
            fetcher(),
            lambda: now,
            now,
            remaining_time=timedelta(minutes=5),
        )
        is None
    )  # too little time remains even to start re-fetching
    assert (
        reuse_research(
            [saved],
            OTHER,
            TEST_AGENCY,
            fetcher(),
            JumpingClock(now, timedelta(minutes=13)),
            now,
        )
        is None
    )  # the budget runs out while the saved pages are being re-fetched
    assert (
        reuse_research([saved], OTHER, TEST_AGENCY, fetcher(), lambda: now, now) is not None
    )  # the same refetch with room to spare still reuses


# --- the one service path ----------------------------------------------------------


class EveryTokenItsOwnOwner:
    """Bearer verification is injectable; each synthetic token is one foreign customer."""

    def verify(self, token: str) -> Customer | None:
        return Customer(token) if token.startswith("fake-") else None


async def research_ready_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[AsyncClient, CaseService, FakeExtractor]:
    """A service whose research step runs the real reuse seams over the fixture pages."""
    monkeypatch.setattr(prehold, "resolve_ticket_issuer", lambda *_a, **_k: Resolved(TEST_AGENCY))
    real_run = run_research

    def run(ticket: Ticket, agency: Any, _seeds: Any, *args: Any, **kwargs: Any) -> ResearchResult:
        return real_run(ticket, agency, SEEDS, *args, **kwargs)

    monkeypatch.setattr(case_service, "run_research", run)
    extractor = answers()
    payments, _transport = stripe(stripe_fixture())
    real = service(tmp_path / "pb.sqlite", payments)
    object.__setattr__(
        real._deps, "research", ResearchDeps(fetcher(), extractor, None, lambda _s: None)
    )
    app = create_app(real, verifier=EveryTokenItsOwnOwner())
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://real.test")
    await client.__aenter__()
    return client, real, extractor


async def save_foreign_research(
    client: AsyncClient, real: CaseService, research: str, citation: str, updated_at: datetime
) -> None:
    """Another customer's saved research record, reachable only through the reuse lookup."""
    body = copy.deepcopy(INTAKE)
    body["ticket"]["citation_number"] = citation
    # One workspace per owner, so each foreign record needs its own customer.
    case = await submit_as(client, {"Authorization": f"Bearer fake-{citation.lower()}"}, body)
    real._store.save_research(real._intake(case).intake_id, research, "{}", updated_at, None)


def watch_research_lookups(
    monkeypatch: pytest.MonkeyPatch, real: CaseService
) -> list[tuple[str, ...]]:
    """Record the saved research records that each reuse lookup actually reads."""
    lookups: list[tuple[str, ...]] = []
    recent = real._store.recent_research_json

    def spy(since: datetime, exclude_intake_id: str, limit: int) -> tuple[str, ...]:
        result = recent(since, exclude_intake_id, limit)
        lookups.append(result)
        return result

    monkeypatch.setattr(real._store, "recent_research_json", spy)
    return lookups


@pytest.mark.anyio
async def test_lookup_is_bounded_to_ten_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, extractor = await research_ready_service(tmp_path, monkeypatch)
    try:
        saved, _ = first_research()
        junk = research_json(replace(saved, decision=ReleaseDecision(False, ("junk",))))
        oldest = research_json(replace(saved, decision=ReleaseDecision(False, ("oldest",))))
        for index in range(10):
            await save_foreign_research(
                client, real, junk, f"JUNK-{index:04d}", NOW + timedelta(minutes=1)
            )
        await save_foreign_research(client, real, oldest, "JUNK-OLDEST", NOW - timedelta(minutes=1))
        lookups = watch_research_lookups(monkeypatch, real)
        case = await submit_as(client, BOB, copy.deepcopy(INTAKE))
        await authorize_and_research(client, case, BOB)
        assert len(lookups) == 1
        (read,) = lookups
        assert 0 < len(read) <= 10
        assert set(read) == {junk}  # only the ten newest records are read
        assert oldest not in read  # the eleventh is beyond the bound
        assert extractor.calls == 1  # the case still gets its own fresh research
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_malformed_foreign_record_falls_back_to_fresh_research(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, extractor = await research_ready_service(tmp_path, monkeypatch)
    try:
        malformed = (
            '{"version":1,"kind":"research",',
            '{"version":1,"kind":"answers","payload":{}}',
            '"not-a-research-record"',
        )
        for index, raw in enumerate(malformed):
            await save_foreign_research(
                client, real, raw, f"MALFORMED-{index}", NOW - timedelta(minutes=1)
            )
        lookups = watch_research_lookups(monkeypatch, real)
        case = await submit_as(client, BOB, copy.deepcopy(INTAKE))
        await authorize_and_research(client, case, BOB)
        assert len(lookups) == 1
        (read,) = lookups
        assert set(read) == set(malformed)  # the lookup really saw the broken records
        assert extractor.calls == 1  # ...and researched afresh anyway
        saved = real._store.get_research(case)
        assert saved is not None
        decode_research(saved.research_json)  # the fresh research is a real record
    finally:
        await client.aclose()
