"""J09/J25/J26 through real issuer resolution, ASGI, research and SQLite recovery.

All names, directory records, source pages and model replies are synthetic contracts,
not official-source fixtures or measured model accuracy. Only provider I/O is faked;
the issuer resolver, assessors, delegation/source guards and case service are real.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker import prehold
from parkingbreaker.agencies import IssuerType, SourceScope
from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.case_service import CaseService, ResearchDeps
from parkingbreaker.json_types import JsonValue
from parkingbreaker.prehold import PreholdDeps
from parkingbreaker.research.extractor import LlmExtractor
from parkingbreaker.research.fakes import (
    FakeFetcher,
    FakeLlmClient,
    FakeSearcher,
    html_response,
    redirect_response,
)
from parkingbreaker.research.findings import EvidenceBasis
from parkingbreaker.research.issuer_assessment import (
    IssuerAssessment,
    IssuerQuote,
    LlmIssuerAssessor,
)
from parkingbreaker.research.issuer_resolution import resolve_ticket_issuer
from parkingbreaker.research.jurisdiction import DotGovRecord, DotGovSnapshot
from parkingbreaker.research.link_assessment import LlmLinkAssessor
from parkingbreaker.research.procedure import ProcedureAssessment, assessment_data
from parkingbreaker.research.questions import ANSWERABLE_KEYS, FactKey
from parkingbreaker.research.run import RecoveryConfig
from parkingbreaker.research.sources import html_to_text, is_allowed
from parkingbreaker.research_records import decode_agency, decode_research, research_json
from parkingbreaker.service_policy import SERVICE_CONTROL, STATE_BASIS
from test_case_service import ALICE, INTAKE, NOW, PM, WORKER, service, stripe, submit
from test_case_service import anyio_backend as anyio_backend
from test_research_issuer_assessment import reply as issuer_reply
from test_stripe_payment import Transport, fixture
from test_t15_recovery import _reopen

pytestmark = pytest.mark.anyio
STAGE = "first-level review"
CITY = "Westhaven"
HOST = "citations.example.test"
ANCHOR = "Contest this parking citation"


@dataclass(frozen=True)
class Subject:
    scenario: str
    slug: str
    name: str
    body: str
    domain_type: str
    kind: IssuerType

    @property
    def domain(self) -> str:
        return f"westhaven-{self.slug}.gov"

    @property
    def identity(self) -> str:
        return f"CA:{self.domain}"

    @property
    def portal(self) -> str:
        return f"https://{HOST}/{self.slug.upper()}/contest"


SUBJECTS = (
    Subject(
        "J09",
        "campus",
        "Westhaven Campus University",
        "Westhaven Campus Review Board",
        "School district",
        IssuerType.PUBLIC_EDUCATION,
    ),
    Subject(
        "J09",
        "county",
        "Westhaven County Parking Authority",
        "Westhaven County Review Board",
        "County",
        IssuerType.COUNTY,
    ),
    Subject(
        "J25",
        "state-parks",
        "Westhaven State Parks Department",
        "Westhaven Parks Review Bureau",
        "State or territory",
        IssuerType.STATE_AGENCY,
    ),
    Subject(
        "J26",
        "hospital",
        "Westhaven Hospital Authority",
        "Westhaven Hospital Review Board",
        "Special district",
        IssuerType.AUTHORITY,
    ),
)


def procedure(subject: Subject) -> tuple[str, dict[str, JsonValue]]:
    """Build a fresh complete procedure and source-bound reply for this exact subject."""
    heading = f"{subject.name} Parking Citation Initial Review."
    how = "You may contest a parking citation by mail."
    where = f"Mail your appeal to {subject.body}, 100 Example Avenue, Westhaven, CA 94000."
    when = "Contest within 21 calendar days of the citation issue date."
    address: JsonValue = {
        "recipient": subject.body,
        "attention": None,
        "line1": "100 Example Avenue",
        "line2": None,
        "city": CITY,
        "state": "CA",
        "zip_code": "94000",
    }
    clauses: list[tuple[FactKey, JsonValue, str]] = [
        (FactKey.MAIL_IN_ACCEPTED, True, how),
        (FactKey.GOVERNING_CODE_SECTION, "SEC. 1.2", "SEC. 1.2. Synthetic parking rule."),
        (FactKey.DEADLINE_DAYS, 21, when),
        (FactKey.DEADLINE_CLOCK_START, "ISSUE_DATE", when),
        (FactKey.DEADLINE_RULE, "RECEIPT", "Mailed contests must be received by the deadline."),
        (FactKey.MAILING_ADDRESS, address, where),
        (FactKey.REQUIRED_ITEMS, ["citation number"], "Include your citation number."),
        (FactKey.FINE_AMOUNT, 90, "The fine for this violation is $90."),
        (FactKey.CODE_EXCEPTIONS, [], "There are no exceptions."),
        (FactKey.DEADLINE_ABSOLUTE, {"applicable": False, "date": None}, when),
    ]
    html = f"<h1>{heading}</h1>" + "".join(f"<p>{q}</p>" for _, _, q in clauses)
    sha = hashlib.sha256(html.encode()).hexdigest()
    text = html_to_text(html)
    assessment = ProcedureAssessment(
        True,
        subject.identity,
        STAGE,
        "parking citation",
        sha,
        text,
        heading,
        how,
        where,
        when,
        "Synthetic complete procedure gives mail route, address, deadline and inventory.",
    )
    findings: list[JsonValue] = [
        {
            "key": key.value,
            "value": value,
            "status": "CONFIRMED",
            "verbatim_quote": quote,
            "source_sha256s": [sha],
            "jurisdiction": subject.identity,
            "stage": STAGE,
        }
        for key, value, quote in clauses
    ]
    for key in set(ANSWERABLE_KEYS) - {key for key, _, _ in clauses}:
        findings.append(
            {
                "key": key.value,
                "value": [],
                "status": "CONFIRMED",
                "verbatim_quote": text,
                "source_sha256s": [sha],
                "jurisdiction": subject.identity,
                "stage": STAGE,
                "evidence_basis": EvidenceBasis.NONE_STATED_IN_OFFICIAL_PROCEDURE.value,
                "procedure_assessment": cast(JsonValue, assessment_data(assessment)),
            }
        )
    return html, {"findings": findings, "payment_rules": []}


@dataclass
class Scenario:
    subject: Subject
    real: CaseService
    case: str
    database: Path
    fetcher: FakeFetcher
    issuer: FakeLlmClient
    link: FakeLlmClient
    model: FakeLlmClient
    transport: Transport

    def reopen(self) -> None:
        self.real = _reopen(self.real, self.database)


async def start(
    tmp_path: Path, subject: Subject, *, damage: str = "", financial_body: bool = False
) -> Scenario:
    assert prehold.__dict__["resolve_ticket_issuer"] is resolve_ticket_issuer
    root = f"https://{subject.domain}/"
    issuer_quote = f"{subject.name} issues these parking citations."
    body_quote = (
        f"At initial review, {subject.body} does not accept cash payments "
        "but reviews parking appeals."
        if financial_body
        else f"At initial review, {subject.body} decides contests of these citations."
    )
    type_quote = f"{subject.name} is a {subject.kind.value} government issuer."
    collector = "Westhaven Private Parking LLC"
    collector_quote = (
        f"{collector} collects ticket payments for {subject.name} but does not decide appeals."
    )
    html = "".join(f"<p>{q}</p>" for q in (issuer_quote, body_quote, type_quote)) + (
        f'<p><a href="{subject.portal}">{ANCHOR}</a></p>'
    )
    if subject.scenario == "J26":
        html += f"<p>{collector_quote}</p>"
    sha = hashlib.sha256(html.encode()).hexdigest()
    assessment = IssuerAssessment(
        1,
        subject.name,
        subject.kind,
        "Westhaven City Review Board"
        if damage == "wrong_body"
        else collector
        if damage == "private_collector"
        else subject.body,
        "CA",
        False,
        IssuerQuote(issuer_quote, sha),
        IssuerQuote(collector_quote if damage == "private_collector" else body_quote, sha),
        IssuerQuote(type_quote, sha),
    )
    issuer = FakeLlmClient(issuer_reply(assessment))
    link = FakeLlmClient(
        {
            "links": [
                {
                    "index": 1,
                    "purpose": "CONTEST",
                    "belongs_to_issuer": True,
                    "stage": STAGE,
                    "verbatim_quote": ANCHOR,
                }
            ]
        }
    )
    portal_html, model_reply = procedure(subject)
    model = FakeLlmClient(model_reply)
    bad = {
        "wrong_tenant": f"https://{HOST}/CITY/contest",
        "case_changed": subject.portal.replace(subject.slug.upper(), subject.slug),
        "prefix_sibling": subject.portal + "-other",
        "redirect": f"https://{HOST}/CITY/contest",
    }.get(damage)
    responses = {
        root: html_response(root, html),
        subject.portal: html_response(subject.portal, portal_html),
    }
    if bad is not None:
        # Even a perfectly formed page/model reply outside the saved scope cannot win.
        responses[bad] = html_response(bad, portal_html)
        responses[subject.portal] = (
            redirect_response(subject.portal, bad)
            if damage == "redirect"
            else html_response(subject.portal, "", status=403)
        )
    fetcher = FakeFetcher(responses)
    searcher = FakeSearcher(lambda _: ())
    record = DotGovRecord(subject.domain, subject.domain_type, subject.name, None, CITY, "CA")
    city_record = DotGovRecord("westhaven.gov", "City", "City of Westhaven", None, CITY, "CA")
    snapshot = DotGovSnapshot(NOW.date(), (record, city_record), {}, "a" * 64)
    seed = bad or subject.portal
    # Include an untrusted same-host city seed even in the positive case. Exact path
    # authorization still belongs to the saved agency scopes and fetch_source.
    known = (
        {"status": "active", "online_appeal_url": f"https://{HOST}/CITY/contest"},
        {"status": "active", "online_appeal_url": seed},
    )
    payments, transport = stripe(fixture())
    database = tmp_path / "pb.sqlite"
    real = service(database, payments)
    real = CaseService(
        replace(
            real._deps,
            prehold=PreholdDeps(
                snapshot,
                searcher,
                fetcher,
                LlmIssuerAssessor(issuer),
                LlmLinkAssessor(link),
                lambda: NOW,
                25,
                STATE_BASIS,
                SERVICE_CONTROL,
            ),
            research=ResearchDeps(
                fetcher,
                LlmExtractor(model),
                searcher,
                lambda _: None,
                recovery=RecoveryConfig(retry_delays=()),
                known_records=known,
            ),
        )
    )
    body = copy.deepcopy(INTAKE)
    body["ticket"].update(issuer_hint=subject.name, issued_city=CITY, stage=STAGE)
    async with AsyncClient(
        transport=ASGITransport(app=create_app(real)), base_url="http://issuer.test"
    ) as client:
        _, receipt = await submit(client, body)
    return Scenario(
        subject, real, receipt["case_id"], database, fetcher, issuer, link, model, transport
    )


async def research(current: Scenario) -> None:
    # Reopening before authorization/research exercises the real agency_json handoff.
    current.reopen()
    async with AsyncClient(
        transport=ASGITransport(app=create_app(current.real)), base_url="http://issuer.test"
    ) as client:
        tier = await client.put(
            f"/cases/{current.case}/tier", headers=ALICE, json={"tier": "CERTIFIED"}
        )
        assert tier.status_code == 200, tier.text
        auth = await client.post(f"/cases/{current.case}/authorization", headers=ALICE, json=PM)
        assert auth.status_code == 200, auth.text
        step = await client.post(
            f"/internal/cases/{current.case}/step", headers=WORKER, json={"step_id": "research"}
        )
        assert step.status_code == 200, step.text


@pytest.mark.parametrize("subject", SUBJECTS, ids=lambda s: s.scenario + "-" + s.slug)
async def test_actual_issuer_and_exact_delegation_survive_both_restarts(
    tmp_path: Path, subject: Subject
) -> None:
    current = await start(tmp_path, subject)
    try:
        agency_raw = current.real._intake(current.case).agency_json
        assert agency_raw is not None
        agency = decode_agency(agency_raw)
        assert (agency.id, agency.name, agency.issuer_type, agency.deciding_body) == (
            subject.identity,
            subject.name,
            subject.kind,
            subject.body,
        )
        assert agency.official_domains == (subject.domain,)
        assert agency.delegated_scopes == (SourceScope(HOST, f"/{subject.slug.upper()}/contest"),)
        assert not is_allowed(f"https://{HOST}/CITY/contest", agency.source_scopes())
        await research(current)
        current.reopen()
        assert current.real._intake(current.case).agency_json == agency_raw
        assert current.real.status(current.case).state == "DRAFTING"
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        result = decode_research(saved.research_json)
        assert result.resolve().ready
        address = result.resolve().mailing_address
        assert address is not None
        assert address.value == {
            "recipient": subject.body,
            "attention": None,
            "line1": "100 Example Avenue",
            "line2": None,
            "city": CITY,
            "state": "CA",
            "zip_code": "94000",
        }
        assert research_json(result) == saved.research_json
        assert {f.key for f in result.findings} == set(ANSWERABLE_KEYS)
        assert all(s.final_url == subject.portal for s in result.sources)
        assert f"https://{HOST}/CITY/contest" not in current.fetcher.calls
        assert len(current.issuer.calls) == len(current.link.calls) == len(current.model.calls) == 1
        calls = list(current.fetcher.calls)
        current.real.step(current.case, s.WorkerStep(step_id="research"))
        assert current.fetcher.calls == calls
        assert len(current.model.calls) == 1
        assert all("capture" not in path for _, path, *_ in current.transport.calls)
    finally:
        current.real._store.close()


@pytest.mark.parametrize("subject", SUBJECTS, ids=lambda s: s.scenario + "-" + s.slug)
@pytest.mark.parametrize("damage", ["wrong_tenant", "case_changed", "prefix_sibling", "redirect"])
async def test_wrong_tenant_cannot_lend_complete_procedure_despite_truthful_model_quotes(
    tmp_path: Path, subject: Subject, damage: str
) -> None:
    current = await start(tmp_path, subject, damage=damage)
    try:
        assert current.real._intake(current.case).agency_json is not None
        await research(current)
        current.reopen()
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        result = decode_research(saved.research_json)
        assert not result.resolve().ready
        status = current.real.status(current.case)
        assert status.state == "STOPPED_NO_CHARGE"
        assert status.payment_state == "RELEASED"
        assert status.stop is not None
        assert status.stop.family == "NOT_YET_VERIFIED"
        assert all(
            url.startswith(f"https://{subject.domain}/") or url == subject.portal
            for url in current.fetcher.calls
        )
        assert result.failures
        assert research_json(result) == saved.research_json
        assert all("capture" not in path for _, path, *_ in current.transport.calls)
    finally:
        current.real._store.close()


@pytest.mark.parametrize("subject", SUBJECTS, ids=lambda s: s.scenario + "-" + s.slug)
async def test_nearby_city_deciding_body_cannot_borrow_another_issuer_quote(
    tmp_path: Path, subject: Subject
) -> None:
    current = await start(tmp_path, subject, damage="wrong_body")
    try:
        current.reopen()
        record = current.real._intake(current.case)
        assert record.agency_json is None
        assert current.real.status(current.case).state == "STOPPED_NO_CHARGE"
        async with AsyncClient(
            transport=ASGITransport(app=create_app(current.real)), base_url="http://issuer.test"
        ) as client:
            offer = await client.get(f"/cases/{current.case}/pre-hold", headers=ALICE)
            assert offer.status_code == 200
            assert offer.json()["kind"] == "stop"
            assert offer.json()["family"] == "NOT_YET_VERIFIED"
        assert len(current.issuer.calls) == 1
        assert not current.link.calls
        assert not current.model.calls
        assert not current.transport.calls
        assert current.real._store.get_research(current.case) is None
    finally:
        current.real._store.close()


async def test_public_hospital_review_body_can_also_refuse_cash_payments(tmp_path: Path) -> None:
    current = await start(tmp_path, SUBJECTS[-1], financial_body=True)
    try:
        assert current.real._intake(current.case).agency_json is not None
        await research(current)
        current.reopen()
        assert current.real.status(current.case).state == "DRAFTING"
        assert all("capture" not in path for _, path, *_ in current.transport.calls)
    finally:
        current.real._store.close()


async def test_public_hospital_private_collector_is_not_the_deciding_body(tmp_path: Path) -> None:
    current = await start(tmp_path, SUBJECTS[-1], damage="private_collector")
    try:
        current.reopen()
        assert current.real._intake(current.case).agency_json is None
        status = current.real.status(current.case)
        assert status.state == "STOPPED_NO_CHARGE"
        assert status.stop is not None
        assert status.stop.family == "NOT_YET_VERIFIED"
        assert not current.model.calls
        assert not current.transport.calls
    finally:
        current.real._store.close()
