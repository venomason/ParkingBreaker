"""J21-J24 synthetic contracts through real extraction, SQLite and customer answers.

Every page and model reply below is invented test data, never official agency evidence
or measured model accuracy. Only issuer discovery and provider I/O use existing fakes;
CaseService invokes the actual research runner and extractor.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.case_service import CaseService, ResearchDeps
from parkingbreaker.json_types import JsonValue
from parkingbreaker.research.extractor import LlmExtractor
from parkingbreaker.research.fakes import FakeFetcher, FakeLlmClient, FakeSearcher, html_response
from parkingbreaker.research.findings import EvidenceBasis
from parkingbreaker.research.ladder import CustomerQuestion
from parkingbreaker.research.payment_effects import PaymentAction, PaymentCondition, PaymentEffect
from parkingbreaker.research.procedure import (
    ProcedureAssessment,
    assessment_data,
    requirement_sentences,
)
from parkingbreaker.research.questions import ANSWERABLE_KEYS, FactKey
from parkingbreaker.research.resolution import ResearchAnswers
from parkingbreaker.research.sources import html_to_text
from parkingbreaker.research_records import decode_answers, decode_research, research_json
from samples import SAN_FRANCISCO
from test_case_service import ALICE, INTAKE, PM, WORKER, service, stripe, submit
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution
from test_stripe_payment import Transport, fixture
from test_t15_recovery import _reopen

pytestmark = pytest.mark.anyio
PROCEDURE = "https://www.sfmta.com/synthetic-j-contracts/procedure"
POLICY = "https://www.sfmta.com/synthetic-j-contracts/conditions"
FINE = "Paying the fine waives your right to request an initial review."
DEPOSIT = "A hearing deposit is required to request an administrative hearing."
PLAN = "Enrolling in a payment plan waives your right to request an initial review."
ADDRESS: dict[str, JsonValue] = {
    "recipient": "SFMTA Customer Service Center",
    "attention": "Citation Review",
    "line1": "11 South Van Ness Avenue",
    "line2": None,
    "city": "San Francisco",
    "state": "CA",
    "zip_code": "94103",
}


def _inputs(
    stage: str, quote: str, action: PaymentAction | None, policy_stage: str | None = None
) -> tuple[FakeFetcher, FakeLlmClient]:
    """Fresh complete stage-specific synthetic procedure; no relabelled old findings."""
    title = "Administrative Hearing" if stage == "administrative hearing" else "Initial Review"
    heading = f"San Francisco {SAN_FRANCISCO.deciding_body} Parking Citation {title}."
    how = "You may contest your parking citation by mail."
    where = (
        "Mail your appeal to SFMTA Customer Service Center ATTN: Citation Review "
        "11 South Van Ness Avenue San Francisco, CA 94103."
    )
    when = "Contest within 21 calendar days of the citation issue date."
    clauses: list[tuple[FactKey, JsonValue, str]] = [
        (FactKey.MAIL_IN_ACCEPTED, True, how),
        (FactKey.GOVERNING_CODE_SECTION, "SEC. 7.2.22", "SEC. 7.2.22. Street cleaning."),
        (FactKey.DEADLINE_DAYS, 21, when),
        (FactKey.DEADLINE_CLOCK_START, "ISSUE_DATE", when),
        (FactKey.DEADLINE_RULE, "RECEIPT", "Your mailed contest must be received by the deadline."),
        (FactKey.MAILING_ADDRESS, ADDRESS, where),
        (FactKey.REQUIRED_ITEMS, ["citation number"], "Include your citation number."),
        (FactKey.FINE_AMOUNT, 90, "The fine for this violation is $90."),
        (FactKey.CODE_EXCEPTIONS, [], "There are no exceptions."),
        (FactKey.DEADLINE_ABSOLUTE, {"applicable": False, "date": None}, when),
    ]
    html = f"<h1>{heading}</h1>" + "".join(f"<p>{text}</p>" for _, _, text in clauses)
    sha = hashlib.sha256(html.encode()).hexdigest()
    text = html_to_text(html)
    assessment = ProcedureAssessment(
        True,
        SAN_FRANCISCO.id,
        stage,
        "parking citation",
        sha,
        text,
        heading,
        how,
        where,
        when,
        "Synthetic complete procedure includes route, address, deadline and inventory.",
    )
    findings: list[dict[str, JsonValue]] = [
        {
            "key": key.value,
            "value": value,
            "status": "CONFIRMED",
            "verbatim_quote": clause,
            "source_sha256s": [sha],
            "jurisdiction": SAN_FRANCISCO.id,
            "stage": stage,
        }
        for key, value, clause in clauses
    ]
    for key in set(ANSWERABLE_KEYS) - {key for key, _, _ in clauses}:
        findings.append(
            {
                "key": key.value,
                "value": [],
                "status": "CONFIRMED",
                "verbatim_quote": text,
                "source_sha256s": [sha],
                "jurisdiction": SAN_FRANCISCO.id,
                "stage": stage,
                "evidence_basis": EvidenceBasis.NONE_STATED_IN_OFFICIAL_PROCEDURE.value,
                "procedure_assessment": cast(JsonValue, assessment_data(assessment)),
            }
        )
    rule_stage = policy_stage or stage
    rule_title = (
        "Administrative Hearing" if rule_stage == "administrative hearing" else "Initial Review"
    )
    policy_html = (
        f"<h1>San Francisco {SAN_FRANCISCO.deciding_body} Parking Citation {rule_title}.</h1>"
        f"<p>{quote}</p>"
    )
    rules: list[JsonValue] = []
    if action is not None:
        deposit = action is PaymentAction.HEARING_DEPOSIT
        rules.append(
            {
                "action": action.value,
                "effect": (
                    PaymentEffect.REQUIRED_DEPOSIT if deposit else PaymentEffect.EXPLICIT_REVIEW_BAR
                ).value,
                "condition": (
                    PaymentCondition.ACTION_REQUIRED
                    if deposit
                    else PaymentCondition.ACTION_OCCURRED
                ).value,
                "scope": {
                    "issuer": SAN_FRANCISCO.id,
                    "deciding_body": SAN_FRANCISCO.deciding_body,
                    "stage": rule_stage,
                },
                "verbatim_quote": quote,
                "scope_quote": html_to_text(policy_html),
                "source_sha256s": [hashlib.sha256(policy_html.encode()).hexdigest()],
                "status": "CONFIRMED",
            }
        )
    return (
        FakeFetcher(
            {PROCEDURE: html_response(PROCEDURE, html), POLICY: html_response(POLICY, policy_html)}
        ),
        FakeLlmClient({"findings": cast(JsonValue, findings), "payment_rules": rules}),
    )


@dataclass
class Scenario:
    real: CaseService
    case: str
    model: FakeLlmClient
    transport: Transport
    database: Path

    def reopen(self) -> None:
        self.real = _reopen(self.real, self.database)


async def _case(
    tmp_path: Path,
    stage: str,
    quote: str,
    action: PaymentAction | None,
    *,
    policy_stage: str | None = None,
) -> Scenario:
    fetcher, model = _inputs(stage, quote, action, policy_stage)
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
                "online_appeal_url": POLICY,
                "mail_acceptance_policy": {"official_source_url": PROCEDURE},
            },
        ),
    )
    real = CaseService(replace(real._deps, research=research))
    body = copy.deepcopy(INTAKE)
    body["ticket"]["stage"] = stage
    async with AsyncClient(
        transport=ASGITransport(app=create_app(real)), base_url="http://real.test"
    ) as client:
        _, receipt = await submit(client, body)
        case = receipt["case_id"]
        chosen = await client.put(f"/cases/{case}/tier", headers=ALICE, json={"tier": "CERTIFIED"})
        assert chosen.status_code == 200, chosen.text
        authorized = await client.post(f"/cases/{case}/authorization", headers=ALICE, json=PM)
        assert authorized.status_code == 200, authorized.text
        response = await client.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
        )
        assert response.status_code == 200, response.text
    saved = real._store.get_research(case)
    assert saved is not None, real.status(case)
    result = decode_research(saved.research_json)
    assert research_json(result) == saved.research_json
    assert {finding.key for finding in result.findings} == set(ANSWERABLE_KEYS)
    assert len(model.calls) == 1
    assert set(fetcher.calls) == {PROCEDURE, POLICY}
    assert all("capture" not in path for _, path, *_ in transport.calls)
    return Scenario(real, case, model, transport, database)


@pytest.mark.parametrize("answer", [True, False, None])
@pytest.mark.parametrize(
    ("scenario_id", "stage", "quote", "action", "question"),
    [
        ("J21", "first-level review", FINE, PaymentAction.FINE_PAYMENT, "PAID_FINE"),
        (
            "J22",
            "administrative hearing",
            DEPOSIT,
            PaymentAction.HEARING_DEPOSIT,
            "PAID_HEARING_DEPOSIT",
        ),
        ("J23", "first-level review", PLAN, PaymentAction.PLAN_ENROLLMENT, "ENTERED_PLAN"),
    ],
    ids=["J21-fine-bar", "J22-hearing-deposit", "J23-plan-bar"],
)
async def test_j21_j22_j23_extraction_and_durable_factual_answers(
    tmp_path: Path,
    scenario_id: str,
    stage: str,
    quote: str,
    action: PaymentAction,
    question: str,
    answer: bool | None,
) -> None:
    current = await _case(tmp_path, stage, quote, action)
    try:
        current.reopen()
        status = current.real.status(current.case)
        assert status.state == "NEEDS_CUSTOMER", status
        assert status.question is not None
        assert status.question.question_id == question
        before = current.real._store.get_research(current.case)
        assert before is not None
        result = decode_research(before.research_json)
        assert result.ticket.stage == stage
        assert result.payment_scope is not None
        assert result.payment_scope.stage == stage
        assert len(result.payment_rules) == 1
        rule = result.payment_rules[0]
        page = next(page for page in result.sources if page.url == POLICY)
        assert rule.verbatim_quote == quote
        assert rule.source_sha256s == (page.sha256,)
        required = result.resolve().fulfilled_requirements
        assert all(quote not in str(item.value) for item in required)
        status = current.real.answer_question(
            current.case,
            s.PaymentAnswerQuestion.model_validate({"question_id": question, "answer": answer}),
        )
        proceeds = answer is True if scenario_id == "J22" else answer is False
        assert status.state == ("DRAFTING" if proceeds else "STOPPED_NO_CHARGE"), status
        current.reopen()
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        assert saved.research_json == before.research_json
        answers = decode_answers(saved.answers_json)
        assert getattr(answers, question.lower()) is answer
        assert CustomerQuestion(question) in answers.answered
        resolved = decode_research(saved.research_json).resolve(answers)
        assert resolved.fulfilled_requirements == required
        if scenario_id == "J22" or answer is None:
            assert all(stop.official_quote is None for stop in resolved.stops)
        elif answer is True:
            assert any(stop.official_quote == quote for stop in resolved.stops)
        assert len(current.model.calls) == 1
        assert all("capture" not in path for _, path, *_ in current.transport.calls)
    finally:
        current.real._store.close()


async def test_j22_later_hearing_deposit_does_not_block_initial_review(tmp_path: Path) -> None:
    current = await _case(
        tmp_path, "first-level review", DEPOSIT, None, policy_stage="administrative hearing"
    )
    try:
        assert current.real.status(current.case).state == "DRAFTING"
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        result = decode_research(saved.research_json)
        assert result.payment_rules == ()
        assert result.resolve().ready
    finally:
        current.real._store.close()


async def test_j24_relief_availability_does_not_invent_enrollment_or_waiver(tmp_path: Path) -> None:
    current = await _case(
        tmp_path,
        "first-level review",
        "Payment plans and financial assistance are available. View the assistance options.",
        None,
    )
    try:
        current.reopen()
        status = current.real.status(current.case)
        assert status.state == "DRAFTING"
        assert status.question is None
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        answers = decode_answers(saved.answers_json)
        assert answers.entered_plan is None
        assert CustomerQuestion.ENTERED_PLAN not in answers.answered
        result = decode_research(saved.research_json)
        assert result.payment_rules == ()
        assert result.resolve(ResearchAnswers()).ready
    finally:
        current.real._store.close()


async def test_omitted_hearing_deposit_evidence_cannot_clear_the_condition(tmp_path: Path) -> None:
    current = await _case(tmp_path, "administrative hearing", DEPOSIT, None)
    try:
        current.reopen()
        assert current.real.status(current.case).state == "STOPPED_NO_CHARGE"
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        result = decode_research(saved.research_json)
        assert result.payment_rules
        assert not result.resolve().ready
        assert all(
            DEPOSIT not in str(item.value) for item in result.resolve().fulfilled_requirements
        )
    finally:
        current.real._store.close()


@pytest.mark.parametrize(
    "instruction",
    [
        "You must attach a receipt for your hearing deposit.",
        "You must include a check payable to the agency.",
        "A hearing deposit is required to request an administrative hearing "
        "and you must enclose a receipt.",
        "The hearing deposit is required and a copy of the notice is mandatory for your appeal.",
    ],
)
def test_payment_related_packet_instructions_are_still_detected(instruction: str) -> None:
    text = f"Parking Citation Administrative Hearing.\n{instruction}"
    assert requirement_sentences(FactKey.TICKET_SPECIFIC_REQUIREMENTS, text) == (instruction,)


def test_hearing_deposit_does_not_hide_an_adjacent_required_form() -> None:
    text = f"Parking Citation Administrative Hearing.\n{DEPOSIT}\nSubmit agency form A."
    assert requirement_sentences(FactKey.TICKET_SPECIFIC_REQUIREMENTS, text) == ()
    assert requirement_sentences(FactKey.MANDATORY_FORMS, text) == ("Submit agency form A.",)
