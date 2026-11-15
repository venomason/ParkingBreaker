"""Synthetic mixed-stage pages through the actual extractor, researcher and SQLite.

No text below is official agency evidence or measured model accuracy. Existing issuer
and provider fakes supply I/O; policy guards and durable research are the real code.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient

import test_t21_payment_scenarios as base
from parkingbreaker import case_state
from parkingbreaker.api.app import create_app
from parkingbreaker.case_service import CaseService, ResearchDeps
from parkingbreaker.research.extractor import LlmExtractor
from parkingbreaker.research.fakes import FakeFetcher, FakeLlmClient, FakeSearcher, html_response
from parkingbreaker.research.findings import Finding, FindingStatus, apply_quote_guard
from parkingbreaker.research.procedure import quote_procedure_stage
from parkingbreaker.research.questions import ANSWERABLE_KEYS, FactKey
from parkingbreaker.research.resolution import ResearchAnswers, resolution_event
from parkingbreaker.research.run import RecoveryConfig, run_research
from parkingbreaker.research.sources import html_to_text
from parkingbreaker.research_records import decode_research, research_json
from test_case_service import ALICE, INTAKE, PM, WORKER, service, stripe, submit
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution
from test_research_run import CONTEST_SEED, CONTEST_URL, TEST_AGENCY, TICKET, TickingClock
from test_stripe_payment import fixture

pytestmark = pytest.mark.anyio


def mixed_inputs(
    later: str, *, heading: bool = True, current_rule: str = "", stage: str = "first-level review"
) -> tuple[FakeFetcher, FakeLlmClient]:
    fetcher, model = base._inputs(stage, "Payment plans are available.", None)
    old = fetcher.responses[base.PROCEDURE]
    html = old.body.decode()
    if current_rule:
        html += f"<p>{current_rule}</p>"
    html += ("<h2>Administrative Hearing.</h2>" if heading else "") + f"<p>{later}</p>"
    sha = hashlib.sha256(html.encode()).hexdigest()
    text = html_to_text(html)
    reply = copy.deepcopy(model.reply)
    assert isinstance(reply, dict)
    findings = reply["findings"]
    assert isinstance(findings, list)
    for finding in findings:
        assert isinstance(finding, dict)
        finding["source_sha256s"] = [sha]
        if finding.get("evidence_basis") is not None:
            finding["verbatim_quote"] = text
            assessment = finding["procedure_assessment"]
            assert isinstance(assessment, dict)
            assessment["source_sha256"] = sha
            assessment["procedure_text"] = text
    fetcher.responses = {**fetcher.responses, base.PROCEDURE: html_response(base.PROCEDURE, html)}
    return fetcher, FakeLlmClient(reply)


async def run_case(
    tmp_path: Path, inputs: tuple[FakeFetcher, FakeLlmClient], stage: str = "first-level review"
) -> base.Scenario:
    fetcher, model = inputs
    payments, transport = stripe(fixture())
    database = tmp_path / "pb.sqlite"
    real = service(database, payments)
    research = ResearchDeps(
        fetcher,
        LlmExtractor(model),
        FakeSearcher(lambda _: ()),
        lambda _: None,
        known_records=(
            {
                "status": "active",
                "online_appeal_url": base.POLICY,
                "mail_acceptance_policy": {"official_source_url": base.PROCEDURE},
            },
        ),
    )
    real = CaseService(replace(real._deps, research=research))
    body = copy.deepcopy(INTAKE)
    body["ticket"]["stage"] = stage
    async with AsyncClient(
        transport=ASGITransport(app=create_app(real)), base_url="http://stage.test"
    ) as client:
        _, receipt = await submit(client, body)
        case = receipt["case_id"]
        tier = await client.put(f"/cases/{case}/tier", headers=ALICE, json={"tier": "CERTIFIED"})
        assert tier.status_code == 200, tier.text
        auth = await client.post(f"/cases/{case}/authorization", headers=ALICE, json=PM)
        assert auth.status_code == 200, auth.text
        response = await client.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
        )
        assert response.status_code == 200, response.text
    assert all("capture" not in path for _, path, *_ in transport.calls)
    return base.Scenario(real, case, model, transport, database)


@pytest.mark.parametrize("heading", [True, False])
@pytest.mark.parametrize(
    "later",
    [
        "You may request an optional administrative hearing after the initial review.",
        "A hearing deposit is required to request an administrative hearing.",
    ],
)
async def test_j30_later_hearing_on_same_page_does_not_refuse_initial_mail(
    tmp_path: Path, later: str, heading: bool
) -> None:
    inputs = mixed_inputs(later, heading=heading)
    current = await run_case(tmp_path, inputs)
    try:
        current.reopen()
        assert current.real.status(current.case).state == "DRAFTING"
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        research = decode_research(saved.research_json)
        assert research.resolve().ready
        assert research_json(research) == saved.research_json
        assert research.payment_rules == ()
        assert len(research.findings) == len(ANSWERABLE_KEYS)
    finally:
        current.real._store.close()


async def test_later_section_cannot_hide_a_current_stage_portal_prerequisite(
    tmp_path: Path,
) -> None:
    rule = (
        "You must register your initial review through the online portal "
        "before mailing your appeal."
    )
    inputs = mixed_inputs("You may request an optional administrative hearing.", current_rule=rule)
    current = await run_case(tmp_path, inputs)
    try:
        current.reopen()
        assert current.real.status(current.case).state == "STOPPED_NO_CHARGE"
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        assert not decode_research(saved.research_json).resolve().ready
    finally:
        current.real._store.close()


def test_j29_proved_no_mail_route_survives_omitted_model_claims_and_roundtrip() -> None:
    rule = "The administrative hearing is available in person only."
    html = "<h1>San Francisco Parking Citation Administrative Hearing.</h1><p>" + rule + "</p>"
    ticket = replace(TICKET, stage="administrative hearing")
    result = run_research(
        ticket,
        TEST_AGENCY,
        [CONTEST_SEED],
        FakeFetcher({CONTEST_URL: html_response(CONTEST_URL, html)}),
        LlmExtractor(FakeLlmClient({"findings": []})),
        TickingClock(),
        wait=lambda _: None,
        recovery=RecoveryConfig(retry_delays=()),
    )
    saved = decode_research(research_json(result))
    resolution = saved.resolve()
    assert not resolution.ready
    assert any(stop.official_quote and rule in stop.official_quote for stop in resolution.stops)
    event = resolution_event(resolution)
    assert isinstance(event, case_state.Stopped)
    assert event.reason is case_state.StopReason.PROCESS_INCOMPATIBLE
    assert rule in event.detail


async def test_hearing_prior_initial_review_cannot_disappear_as_another_stage(
    tmp_path: Path,
) -> None:
    inputs = mixed_inputs(
        "Payment plans are available.",
        heading=False,
        current_rule="You must first request an initial review.",
        stage="administrative hearing",
    )
    current = await run_case(tmp_path, inputs, "administrative hearing")
    try:
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        research = decode_research(saved.research_json)
        assert not research.resolve().ready
        assert research.resolve(ResearchAnswers(initial_review_completed=True)).ready
    finally:
        current.real._store.close()


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        ("Contest within 21 days of issue.", "first-level review"),
        ("Contest within 30 days of the initial decision.", "administrative hearing"),
        ("Contest within 21 days of issue.\nAdministrative Hearing.", None),
    ],
)
def test_quote_must_stay_within_one_explicit_stage_section(
    quote: str, expected: str | None
) -> None:
    text = (
        "San Francisco Parking Citation Initial Review.\nContest within 21 days of issue.\n"
        "Administrative Hearing.\nContest within 30 days of the initial decision."
    )
    assert quote_procedure_stage(text, quote) == expected


def test_model_cannot_label_a_later_deadline_as_initial_review() -> None:
    from samples import source

    page = source(
        "San Francisco Parking Citation Initial Review.\nContest within 21 days of issue.\n"
        "Administrative Hearing.\nContest within 30 days of the citation issue date."
    )
    claim = Finding(
        FactKey.DEADLINE_DAYS,
        30,
        FindingStatus.CONFIRMED,
        "Contest within 30 days of the citation issue date.",
        (page.sha256,),
        jurisdiction="us-ca-san_francisco",
        stage="first-level review",
        scope_quote="San Francisco Parking Citation Initial Review.",
    )
    (guarded,) = apply_quote_guard([claim], [page])
    assert guarded.status is FindingStatus.UNKNOWN


async def test_later_section_cannot_hide_a_rule_explicitly_applying_to_all_appeals(
    tmp_path: Path,
) -> None:
    current = await run_case(tmp_path, mixed_inputs("All appeals must include agency form A."))
    try:
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        assert not decode_research(saved.research_json).resolve().ready
    finally:
        current.real._store.close()


@pytest.mark.parametrize(("heading", "ready"), [(True, True), (False, False)])
async def test_bare_form_rule_needs_its_own_later_stage_section(
    tmp_path: Path, heading: bool, ready: bool
) -> None:
    current = await run_case(
        tmp_path, mixed_inputs("You must include agency form H.", heading=heading)
    )
    try:
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        assert decode_research(saved.research_json).resolve().ready is ready
    finally:
        current.real._store.close()


@pytest.mark.parametrize("damage", ["borrowed_how", "trimmed_page"])
async def test_complete_procedure_claim_cannot_borrow_or_discard_another_section(
    tmp_path: Path, damage: str
) -> None:
    later = "You may send your initial review by mail."
    inputs = mixed_inputs(later)
    reply = inputs[1].reply
    assert isinstance(reply, dict)
    findings = reply["findings"]
    assert isinstance(findings, list)
    for finding in findings:
        assert isinstance(finding, dict)
        assessment = finding.get("procedure_assessment")
        if not isinstance(assessment, dict):
            continue
        if damage == "borrowed_how":
            assessment["how_quote"] = later
        else:
            text = assessment["procedure_text"]
            assert isinstance(text, str)
            assessment["procedure_text"] = text.split("Administrative Hearing.")[0].strip()
    current = await run_case(tmp_path, inputs)
    try:
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        assert not decode_research(saved.research_json).resolve().ready
    finally:
        current.real._store.close()


async def test_reference_to_prior_review_cannot_erase_a_global_form_rule(tmp_path: Path) -> None:
    inputs = mixed_inputs(
        "Payment plans are available.",
        heading=False,
        current_rule="All appeals must include agency form A and the initial review decision.",
        stage="administrative hearing",
    )
    current = await run_case(tmp_path, inputs, "administrative hearing")
    try:
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        research = decode_research(saved.research_json)
        assert not research.resolve().ready
        assert not research.resolve(ResearchAnswers(initial_review_completed=True)).ready
    finally:
        current.real._store.close()


@pytest.mark.parametrize(
    "heading",
    [
        "Administrative Hearing Instructions",
        "Second-level",
        "Hearing request procedure",
    ],
)
def test_unrecognized_stage_heading_cannot_lend_earlier_scope(heading: str) -> None:
    text = (
        "San Francisco Parking Citation Initial Review.\nContest within 21 days.\n"
        + heading
        + "\nContest within 30 days."
    )
    assert quote_procedure_stage(text, "Contest within 30 days.") is None
