"""T21: cross-customer research reuse (architecture-v8 20.18).

Research for an issuer at the same procedural stage is reused for another customer's
ticket for at most 24 hours from the first research, only after every page it relied on
is re-fetched byte-identical. Ticket facts are always re-derived for the new ticket and no
customer data crosses cases.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker import case_service, prehold
from parkingbreaker.api.app import create_app
from parkingbreaker.case_service import CaseService, ResearchDeps
from parkingbreaker.research.fakes import FakeFetcher, html_response
from parkingbreaker.research.findings import Finding, FindingStatus, ReleaseDecision
from parkingbreaker.research.questions import (
    EMPTY_PACKET_PLAN,
    NO_POSITION,
    CustomerPosition,
    FactKey,
    PacketPlan,
    Ticket,
)
from parkingbreaker.research.resolution import ResearchAnswers
from parkingbreaker.research.run import (
    ResearchResult,
    reuse_research,
    run_research,
)
from parkingbreaker.research_records import research_json
from samples import fixture as sample_page
from test_case_service import (
    BOB,
    INTAKE,
    PM,
    PNG_B64,
    Clock,
    Resolved,
    service,
    stripe,
    submit,
)
from test_case_service import (
    anyio_backend as anyio_backend,
)
from test_case_service import (
    fake_resolution as fake_resolution,
)
from test_research_run import (
    CONTEST_URL,
    SEEDS,
    TEST_AGENCY,
    TICKET,
    TickingClock,
    answers,
    fetcher,
)
from test_stripe_payment import fixture as stripe_fixture

K = FactKey
SECRET_POSITION = CustomerPosition(exception_basis="ALICE-SECRET-EXCEPTION")
SECRET_PLAN = replace(
    EMPTY_PACKET_PLAN,
    customer_fields=(("name", "Alice Anderson"),),
    frozen_pdf=b"%PDF-ALICE-PACKET",
)
OTHER = replace(TICKET, city_id="San Francisco CA")  # another case, same research inputs


def first_research(
    *,
    ticket: Ticket = TICKET,
    contest: str = "sfmta_contest.html",
    position: CustomerPosition = NO_POSITION,
    plan: PacketPlan = EMPTY_PACKET_PLAN,
) -> tuple[ResearchResult, Any]:
    extractor = answers()
    result = run_research(
        ticket,
        TEST_AGENCY,
        SEEDS,
        fetcher(contest),
        extractor,
        TickingClock(),
        wait=lambda _: None,
        customer_position=position,
        packet_plan=plan,
    )
    assert result.decision.ok
    return result, extractor


def attempt(
    saved: ResearchResult,
    ticket: Ticket,
    hours: float,
    *,
    transport: FakeFetcher | None = None,
) -> ResearchResult | None:
    return reuse_research(
        [saved],
        ticket,
        TEST_AGENCY,
        transport or fetcher(),
        lambda: saved.started_at + timedelta(hours=hours),
        saved.started_at + timedelta(hours=hours),
    )


def test_other_customer_same_issuer_and_stage_reuses_after_identical_refetch() -> None:
    saved, extractor = first_research()
    transport = fetcher()
    reused = attempt(saved, OTHER, 2, transport=transport)
    assert reused is not None
    assert extractor.calls == 1
    assert sorted(transport.calls) == sorted(a.url for a in saved.manifest.artifacts)
    assert reused.ticket == replace(OTHER, jurisdiction=TEST_AGENCY.id, stage="first-level review")
    assert reused.started_at == saved.started_at  # the 24 hours roll from the first research
    assert [s.sha256 for s in reused.sources] == [s.sha256 for s in saved.sources]
    assert all(s.retrieved_at == saved.started_at + timedelta(hours=2) for s in reused.sources)
    assert reused.decision.ok


def test_identical_ticket_still_reuses() -> None:
    saved, extractor = first_research()
    assert saved.reusable_for(
        saved.ticket,
        saved.started_at + timedelta(hours=1),
        {s.url: s.sha256 for s in saved.sources},
    )
    assert attempt(saved, TICKET, 1) is not None
    assert extractor.calls == 1


def test_twenty_five_hours_after_first_research_researches_again() -> None:
    saved, _ = first_research()
    assert attempt(saved, OTHER, 25) is None
    assert attempt(saved, OTHER, 24) is None
    assert attempt(saved, OTHER, 23.9) is not None
    assert attempt(saved, OTHER, -1) is None


def test_one_changed_page_byte_researches_again() -> None:
    saved, _ = first_research()
    changed = sample_page("sfmta_contest.html") + " "
    transport = FakeFetcher(
        {**fetcher().responses, CONTEST_URL: html_response(CONTEST_URL, changed)}
    )
    assert attempt(saved, OTHER, 2, transport=transport) is None


def test_unreachable_page_researches_again() -> None:
    saved, _ = first_research()
    assert attempt(saved, OTHER, 2, transport=FakeFetcher({})) is None


def test_different_stage_or_issuer_researches_again() -> None:
    saved, _ = first_research()
    assert attempt(saved, replace(OTHER, stage="administrative hearing"), 2) is None
    other_agency = replace(TEST_AGENCY, id="CA:oakland.gov")
    assert (
        reuse_research(
            [saved],
            OTHER,
            other_agency,
            fetcher(),
            TickingClock(),
            saved.started_at + timedelta(hours=2),
        )
        is None
    )


def test_different_violation_researches_again() -> None:
    saved, _ = first_research()
    assert attempt(saved, replace(OTHER, violation_code="TC 8.1.1"), 2) is None


def test_failed_research_is_never_reused() -> None:
    saved, _ = first_research()
    failed = replace(saved, decision=ReleaseDecision(False, ("x",)))
    assert attempt(failed, OTHER, 2) is None


def test_date_specific_absolute_deadline_is_not_carried_to_another_issue_date() -> None:
    saved, _ = first_research()
    absolute = [
        replace(f, value={"applicable": True, "date": "2026-11-01"})
        if f.key is K.DEADLINE_ABSOLUTE
        else f
        for f in saved.findings
    ]
    dated = replace(saved, findings=tuple(absolute))
    assert attempt(dated, replace(OTHER, issue_date=date(2026, 10, 9)), 2) is None
    assert dated.reusable_across(TICKET, dated.started_at + timedelta(hours=2))


def test_different_issue_date_gets_fresh_research_and_its_own_deadline() -> None:
    saved, _ = first_research()
    other = replace(OTHER, issue_date=date(2026, 10, 9))
    assert attempt(saved, other, 2) is None
    fresh, extractor = first_research(ticket=other)
    deadline = fresh.resolve(ResearchAnswers()).deadline
    assert extractor.calls == 1
    assert deadline is not None
    assert deadline.deadline == other.issue_date + timedelta(days=21)


def test_no_customer_data_crosses_to_the_next_customer() -> None:
    saved, _ = first_research(position=SECRET_POSITION)
    saved = replace(saved, packet_plan=SECRET_PLAN)
    stored = research_json(saved)
    for secret in ("ALICE-SECRET-EXCEPTION", "Alice Anderson"):
        assert secret in stored
    reused = attempt(saved, OTHER, 2)
    assert reused is not None
    assert reused.customer_position == NO_POSITION
    assert reused.packet_plan.customer_fields == ()
    assert reused.packet_plan.frozen_pdf is None
    assert reused.cost.cost_cents == 0
    text = research_json(reused)
    for secret in ("ALICE-SECRET-EXCEPTION", "Alice Anderson", "ALICE-PACKET"):
        assert secret not in text
    assert reused.ticket.violation_description == OTHER.violation_description


def test_new_customers_own_inputs_are_applied_not_the_old_ones() -> None:
    saved, _ = first_research(position=SECRET_POSITION)
    bob = CustomerPosition(exception_basis="BOB-EXCEPTION")
    reused = reuse_research(
        [saved],
        OTHER,
        TEST_AGENCY,
        fetcher(),
        lambda: saved.started_at + timedelta(hours=2),
        saved.started_at + timedelta(hours=2),
        customer_position=bob,
    )
    assert reused is not None
    assert reused.customer_position == bob
    assert "ALICE-SECRET-EXCEPTION" not in research_json(reused)


def test_newest_usable_candidate_wins_and_an_unusable_one_is_skipped() -> None:
    saved, _ = first_research()
    broken = replace(saved, decision=ReleaseDecision(False, ("x",)))
    reused = reuse_research(
        [broken, saved],
        OTHER,
        TEST_AGENCY,
        fetcher(),
        lambda: saved.started_at + timedelta(hours=1),
        saved.started_at + timedelta(hours=1),
    )
    assert reused is not None
    assert reused.decision.ok


def finding_keys(result: ResearchResult) -> set[tuple[FactKey, FindingStatus]]:
    return {(f.key, f.status) for f in result.findings}


def test_reused_findings_match_the_original_findings() -> None:
    saved, _ = first_research()
    reused = attempt(saved, OTHER, 2)
    assert reused is not None
    assert finding_keys(reused) == finding_keys(saved)
    assert all(isinstance(f, Finding) for f in reused.findings)


# --- the one service path ----------------------------------------------------------


WORKER = {"Authorization": "Bearer fake-worker"}


async def two_customers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gap: timedelta, *, changed_ticket: bool = False
) -> tuple[AsyncClient, CaseService, Any, list[Ticket], str]:
    monkeypatch.setattr(prehold, "resolve_ticket_issuer", lambda *_a, **_k: Resolved(TEST_AGENCY))
    real_run = run_research
    runs: list[Ticket] = []

    def run(ticket: Ticket, agency: Any, _seeds: Any, *args: Any, **kwargs: Any) -> ResearchResult:
        runs.append(ticket)
        return real_run(ticket, agency, SEEDS, *args, **kwargs)

    monkeypatch.setattr(case_service, "run_research", run)
    extractor = answers()
    clock = Clock()
    payment_responses: list[dict[str, object]] = []
    for index in (1, 2):
        expires = int((clock.now + gap + timedelta(days=5)).timestamp())
        response = stripe_fixture(expiry=expires)
        payment_intent_id = f"pi_T21Customer{index}"
        response["id"] = payment_intent_id
        charge = response["latest_charge"]
        assert isinstance(charge, dict)
        charge["id"] = f"ch_T21Customer{index}"
        charge["payment_intent"] = payment_intent_id
        payment_responses.append(response)
    payments, _transport = stripe(*payment_responses)
    real = service(tmp_path / "pb.sqlite", payments, clock)
    object.__setattr__(
        real._deps, "research", ResearchDeps(fetcher(), extractor, None, lambda _s: None)
    )
    client = AsyncClient(transport=ASGITransport(app=create_app(real)), base_url="http://real.test")
    await client.__aenter__()
    alice_intake = copy.deepcopy(INTAKE)
    alice_intake["ticket"]["stage"] = "first-level review"
    _w, receipt = await submit(client, alice_intake)
    alice_case = receipt["case_id"]
    await authorize_and_research(client, alice_case, {"Authorization": "Bearer fake-alice"})
    clock.now += gap
    bob_intake = copy.deepcopy(INTAKE)
    bob_intake["ticket"].update(
        citation_number="SYN-9999",
        stage="first-level review",
        license_plate="BOB999",
    )
    if changed_ticket:
        bob_intake["ticket"].update(
            issue_date="2026-10-13", violation_description="BOB-DESCRIPTION"
        )
    bob_intake["contact"].update(name="Bobby Other", email="bob@example.test")
    bob_case = await submit_as(client, BOB, bob_intake)
    await authorize_and_research(client, bob_case, BOB)
    return client, real, extractor, runs, bob_case


async def submit_as(client: AsyncClient, headers: dict[str, str], body: dict[str, Any]) -> str:
    workspace = (await client.post("/workspaces", headers=headers, json={"notes": ""})).json()
    uploaded = await client.post(
        f"/workspaces/{workspace['workspace_id']}/photos",
        headers=headers,
        json={"content_type": "image/png", "data_base64": PNG_B64},
    )
    ref = uploaded.json()["photo_ref"]
    body = {**body, "photo_refs": [ref], "citation_photo_ref": ref}
    receipt = await client.post(
        f"/workspaces/{workspace['workspace_id']}/intake", headers=headers, json=body
    )
    assert receipt.status_code == 200, receipt.text
    return str(receipt.json()["case_id"])


async def authorize_and_research(client: AsyncClient, case: str, headers: dict[str, str]) -> None:
    await client.put(f"/cases/{case}/tier", headers=headers, json={"tier": "CERTIFIED"})
    authorized = await client.post(f"/cases/{case}/authorization", headers=headers, json=PM)
    assert authorized.status_code == 200, authorized.text
    step = await client.post(
        f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
    )
    assert step.status_code == 200, step.text


@pytest.mark.anyio
async def test_service_reuses_for_the_second_customer_without_another_llm_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, extractor, runs, bob = await two_customers(
        tmp_path, monkeypatch, timedelta(hours=2)
    )
    try:
        assert extractor.calls == 1
        assert len(runs) == 1
        saved = real._store.get_research(bob)
        assert saved is not None
        text = saved.research_json + saved.answers_json
        for secret in ("SYN-0001", "SYN123", "alex@example.test", "Alex Example"):
            assert secret not in text
        assert '"2026-10-12"' in saved.research_json
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_service_researches_again_after_twenty_five_hours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _real, extractor, runs, _bob = await two_customers(
        tmp_path, monkeypatch, timedelta(hours=25)
    )
    try:
        assert extractor.calls == 2
        assert len(runs) == 2
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_service_researches_changed_ticket_inputs_afresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, extractor, runs, bob = await two_customers(
        tmp_path, monkeypatch, timedelta(hours=2), changed_ticket=True
    )
    try:
        assert extractor.calls == 2
        assert len(runs) == 2
        saved = real._store.get_research(bob)
        assert saved is not None
        assert "BOB-DESCRIPTION" in saved.research_json
        assert '"2026-10-13"' in saved.research_json
        assert '"2026-10-12"' not in saved.research_json
    finally:
        await client.aclose()
