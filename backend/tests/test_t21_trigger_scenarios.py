"""J14 synthetic deadline-trigger contracts through actual research, ASGI and SQLite.

Official-looking text and model replies below are invented inputs. Issuer/provider
I/O is fake; extraction, source guards, customer answers and date decisions are real.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient

import test_t21_payment_scenarios as base
from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.case_service import CaseService, ResearchDeps
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.mail_config import MailClass
from parkingbreaker.mail_timing import DeadlineRule, latest_safe_send_date
from parkingbreaker.research.extractor import LlmExtractor
from parkingbreaker.research.fakes import FakeFetcher, FakeLlmClient, FakeSearcher, html_response
from parkingbreaker.research.findings import FindingStatus
from parkingbreaker.research.ladder import CustomerQuestion
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.sources import html_to_text
from parkingbreaker.research.support import (
    clock_quote_supported_by_source,
    value_supported_by_quote,
)
from parkingbreaker.research_records import decode_answers, decode_research, research_json
from test_case_service import ALICE, INTAKE, PM, WORKER, Clock, service, stripe, submit
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution
from test_stripe_payment import fixture

pytestmark = pytest.mark.anyio
ISSUE = date(2026, 10, 1)
NOTICE = date(2026, 10, 6)
ISSUE_PERIOD = "Contest within 21 calendar days of the citation issue date."
NOTICE_PERIOD = "Contest within 21 calendar days of the first courtesy notice date."
DISTRACTORS = {
    "ISSUE_DATE": "Keep copies of the first courtesy notice for your records.",
    "NOTICE_DATE": "The citation was issued before the notice.",
}
DEADLINES = {"ISSUE_DATE": date(2026, 10, 22), "NOTICE_DATE": date(2026, 10, 27)}
LATEST = {"ISSUE_DATE": date(2026, 10, 8), "NOTICE_DATE": date(2026, 10, 14)}


def trigger_inputs(
    actual: str, model_value: str, *, same_sentence: bool = False, bare: bool = False
) -> tuple[FakeFetcher, FakeLlmClient]:
    fetcher, model = base._inputs("first-level review", "Payment plans are available.", None)
    period = ISSUE_PERIOD if actual == "ISSUE_DATE" else NOTICE_PERIOD
    extra = DISTRACTORS[actual]
    anchor = period.rstrip(".") if same_sentence else period
    text_quote = (
        anchor + " and " + extra[0].lower() + extra[1:] if same_sentence else period + " " + extra
    )
    html = fetcher.responses[base.PROCEDURE].body.decode().replace(ISSUE_PERIOD, text_quote)
    sha, text = hashlib.sha256(html.encode()).hexdigest(), html_to_text(html)
    reply = copy.deepcopy(model.reply)
    assert isinstance(reply, dict)
    findings = reply["findings"]
    assert isinstance(findings, list)
    for finding in findings:
        assert isinstance(finding, dict)
        finding["source_sha256s"] = [sha]
        if finding["verbatim_quote"] == ISSUE_PERIOD:
            finding["verbatim_quote"] = anchor
        if finding["key"] == FactKey.DEADLINE_CLOCK_START.value:
            finding["value"] = model_value
            finding["verbatim_quote"] = (
                (extra[0].lower() + extra[1:] if same_sentence else extra) if bare else text_quote
            )
        assessment = finding.get("procedure_assessment")
        if isinstance(assessment, dict):
            finding["verbatim_quote"] = text
            assessment["source_sha256"], assessment["procedure_text"] = sha, text
            assessment["when_quote"] = anchor
    fetcher.responses = {**fetcher.responses, base.PROCEDURE: html_response(base.PROCEDURE, html)}
    return fetcher, FakeLlmClient(reply)


async def run_trigger_case(
    tmp_path: Path,
    inputs: tuple[FakeFetcher, FakeLlmClient],
    *,
    send: date = date(2026, 10, 7),
    notice: date | None = NOTICE,
    printed: date | None = None,
) -> tuple[base.Scenario, FakeLobClient]:
    clock = Clock()
    # 5 a.m. Pacific: before Lob's effective cutoff, so ``send`` is the production run date.
    clock.now = datetime.combine(send, datetime.min.time(), UTC).replace(hour=12)
    auth = fixture(expiry=int((clock.now + timedelta(days=6)).timestamp()))
    charge = auth["latest_charge"]
    assert isinstance(charge, dict)
    charge["created"] = int(clock.now.timestamp())
    payments, transport = stripe(auth, copy.deepcopy(auth), fixture("canceled"))
    database = tmp_path / "triggers.sqlite"
    real = service(database, payments, clock)
    fetcher, model = inputs
    lob = FakeLobClient()
    real = CaseService(
        replace(
            real._deps,
            research=ResearchDeps(
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
            ),
            lob=lob,
        )
    )
    body = copy.deepcopy(INTAKE)
    body["ticket"].update(
        issue_date=ISSUE.isoformat(),
        stage="first-level review",
        notice_date=None if notice is None else notice.isoformat(),
        printed_deadline=None if printed is None else printed.isoformat(),
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_app(real)), base_url="http://triggers.test"
    ) as client:
        _, receipt = await submit(client, body)
        case = receipt["case_id"]
        tier = await client.put(f"/cases/{case}/tier", headers=ALICE, json={"tier": "CERTIFIED"})
        assert tier.status_code == 200, tier.text
        response = await client.post(f"/cases/{case}/authorization", headers=ALICE, json=PM)
        assert response.status_code == 200, response.text
        response = await client.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
        )
        assert response.status_code == 200, response.text
    return base.Scenario(real, case, model, transport, database), lob


def assert_saved_replay(current: base.Scenario, inputs: tuple[FakeFetcher, FakeLlmClient]) -> None:
    current.reopen()
    saved = current.real._store.get_research(current.case)
    assert saved is not None
    result = decode_research(saved.research_json)
    assert research_json(result) == saved.research_json
    source = next(page for page in result.sources if page.final_url == base.PROCEDURE)
    assert source.sha256 == hashlib.sha256(inputs[0].responses[base.PROCEDURE].body).hexdigest()
    assert source.text == html_to_text(inputs[0].responses[base.PROCEDURE].body.decode())
    answers = decode_answers(saved.answers_json)
    assert answers.notice_issue_date == NOTICE
    assert CustomerQuestion.NOTICE_ISSUE_DATE in answers.answered
    calls = (list(inputs[0].calls), len(inputs[1].calls), list(current.transport.calls))
    current.reopen()
    current.real.step(current.case, s.WorkerStep(step_id="research"))
    replay = current.real._store.get_research(current.case)
    assert replay is not None
    assert (replay.research_json, replay.answers_json) == (saved.research_json, saved.answers_json)
    assert calls == (list(inputs[0].calls), len(inputs[1].calls), list(current.transport.calls))


@pytest.mark.parametrize("actual", ["ISSUE_DATE", "NOTICE_DATE"])
@pytest.mark.parametrize("same_sentence", [False, True], ids=["separate", "joined"])
@pytest.mark.parametrize("claim", ["truthful", "opposing", "bare", "service"])
async def test_j14_trigger_must_belong_to_deadline(
    tmp_path: Path, actual: str, same_sentence: bool, claim: str
) -> None:
    opposing = "NOTICE_DATE" if actual == "ISSUE_DATE" else "ISSUE_DATE"
    model_value = (
        actual if claim == "truthful" else "SERVICE_DATE" if claim == "service" else opposing
    )
    inputs = trigger_inputs(actual, model_value, same_sentence=same_sentence, bare=claim == "bare")
    current, lob = await run_trigger_case(tmp_path, inputs)
    try:
        assert_saved_replay(current, inputs)
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        result = decode_research(saved.research_json)
        answers = decode_answers(saved.answers_json)
        resolved = result.resolve(answers)
        finding = next(f for f in result.findings if f.key is FactKey.DEADLINE_CLOCK_START)
        if claim == "truthful":
            assert finding.status is FindingStatus.CONFIRMED
            assert finding.value == actual
            assert resolved.ready
            assert resolved.deadline is not None
            assert resolved.deadline.deadline == DEADLINES[actual]
            assert (
                latest_safe_send_date(
                    resolved.deadline.deadline, DeadlineRule.RECEIPT, MailClass.CERTIFIED
                )
                == LATEST[actual]
            )
            assert current.real.status(current.case).state == "DRAFTING"
        else:
            assert finding.status is FindingStatus.UNKNOWN
            assert finding.value is None
            assert not resolved.ready
            assert resolved.deadline is None
            assert current.real.status(current.case).state == "STOPPED_NO_CHARGE"
            current.real.dispatch(current.case)
            current.real.dispatch(current.case)
            assert current.real.status(current.case).payment_state == "RELEASED"
        assert not lob.calls
        assert all("capture" not in path for _, path, *_ in current.transport.calls)
    finally:
        current.real._store.close()


@pytest.mark.parametrize("actual", ["ISSUE_DATE", "NOTICE_DATE"])
async def test_j14_wrong_trigger_at_actual_mailing_boundary(tmp_path: Path, actual: str) -> None:
    opposing = "NOTICE_DATE" if actual == "ISSUE_DATE" else "ISSUE_DATE"
    inputs = trigger_inputs(actual, opposing)
    current, lob = await run_trigger_case(tmp_path, inputs, send=date(2026, 10, 9))
    try:
        assert_saved_replay(current, inputs)
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        resolved = decode_research(saved.research_json).resolve(decode_answers(saved.answers_json))
        assert not resolved.ready
        assert resolved.deadline is None
        assert current.real.status(current.case).state == "STOPPED_NO_CHARGE"
        assert not lob.calls
        assert all("capture" not in path for _, path, *_ in current.transport.calls)
    finally:
        current.real._store.close()


@pytest.mark.parametrize("notice", [NOTICE, None], ids=["explicit", "conservative-fallback"])
@pytest.mark.parametrize("printed", [None, date(2026, 10, 21)])
async def test_j14_explicit_intake_dates_and_earlier_printed_date_are_durable(
    tmp_path: Path, notice: date | None, printed: date | None
) -> None:
    inputs = trigger_inputs("NOTICE_DATE", "NOTICE_DATE")
    current, lob = await run_trigger_case(tmp_path, inputs, notice=notice, printed=printed)
    try:
        current.reopen()
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        answers = decode_answers(saved.answers_json)
        assert answers.notice_issue_date == (notice or ISSUE)
        assert answers.printed_deadline == printed
        resolved = decode_research(saved.research_json).resolve(answers)
        assert resolved.ready
        assert resolved.deadline is not None
        official = (notice or ISSUE) + timedelta(days=21)
        assert resolved.deadline.deadline == min(official, printed or official)
        assert current.real.status(current.case).state == "DRAFTING"
        assert not lob.calls
        assert all("capture" not in path for _, path, *_ in current.transport.calls)
    finally:
        current.real._store.close()


@pytest.mark.parametrize(
    "quote",
    [
        "Late fees begin after 21 days. The period runs from the notice date.",
        "Late fees begin after 21 days. The deadline is calculated from the notice date.",
        "The fine deadline is 21 days. The clock runs from the notice date.",
        "Late fees begin after 21 days. Retain this page for your records. "
        "The deadline is calculated from the notice date.",
    ],
)
@pytest.mark.parametrize("trimmed", [False, True])
async def test_j14_fee_clock_cannot_lend_notice_trigger(
    tmp_path: Path, quote: str, trimmed: bool
) -> None:
    fetcher, model = trigger_inputs("ISSUE_DATE", "NOTICE_DATE")
    original = fetcher.responses[base.PROCEDURE].body.decode()
    html = original.replace(DISTRACTORS["ISSUE_DATE"], quote)
    text, sha = html_to_text(html), hashlib.sha256(html.encode()).hexdigest()
    reply = copy.deepcopy(model.reply)
    assert isinstance(reply, dict)
    findings = reply["findings"]
    assert isinstance(findings, list)
    for finding in findings:
        assert isinstance(finding, dict)
        finding["source_sha256s"] = [sha]
        if finding["key"] == FactKey.DEADLINE_CLOCK_START.value:
            finding["verbatim_quote"] = "from the notice date" if trimmed else quote
        assessment = finding.get("procedure_assessment")
        if isinstance(assessment, dict):
            finding["verbatim_quote"] = text
            assessment["source_sha256"], assessment["procedure_text"] = sha, text
    fetcher.responses = {**fetcher.responses, base.PROCEDURE: html_response(base.PROCEDURE, html)}
    inputs = fetcher, FakeLlmClient(reply)
    current, lob = await run_trigger_case(tmp_path, inputs, send=date(2026, 10, 9))
    try:
        assert_saved_replay(current, inputs)
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        resolved = decode_research(saved.research_json).resolve(decode_answers(saved.answers_json))
        assert not resolved.ready
        assert resolved.deadline is None
        assert current.real.status(current.case).state == "STOPPED_NO_CHARGE"
        assert not lob.calls
        assert all("capture" not in path for _, path, *_ in current.transport.calls)
    finally:
        current.real._store.close()


@pytest.mark.parametrize(
    ("value", "quote", "expected"),
    [
        ("NOTICE_DATE", "Appeal within twenty-one calendar days of the notice date.", True),
        ("ISSUE_DATE", "Contest within 21 business days of the citation issue date.", True),
        ("NOTICE_DATE", "From the notice date, you have 21 days to appeal.", True),
        ("NOTICE_DATE", "The deadline is calculated from the notice date.", True),
        ("NOTICE_DATE", "The deadline is not calculated from the notice date.", False),
        ("NOTICE_DATE", "Payment deadline is calculated from the notice date.", False),
        (
            "NOTICE_DATE",
            "Appeals within 21 days of issue; late fees within 21 days from the notice date.",
            False,
        ),
        ("NOTICE_DATE", "The appeal period is 21 days, beginning on the notice date.", True),
        ("NOTICE_DATE", "You have 21 days to appeal from the notice date.", True),
        ("NOTICE_DATE", "Keep copies of the notice, you have 21 days to appeal.", False),
        ("NOTICE_DATE", "Keep copies within 21 days of the notice for your records.", False),
    ],
)
def test_j14_supported_clock_grammar(value: str, quote: str, expected: bool) -> None:
    assert value_supported_by_quote(FactKey.DEADLINE_CLOCK_START, value, quote) is expected


@pytest.mark.parametrize(
    ("quote", "source", "expected"),
    [
        ("of the citation issue date", ISSUE_PERIOD, True),
        ("of the first courtesy notice", DISTRACTORS["ISSUE_DATE"], False),
        (
            "From the delinquency notice mailing date.",
            "Contest within 14 days from the delinquency notice mailing date. "
            "From the delinquency notice mailing date.",
            True,
        ),
        ("of the first courtesy notice", NOTICE_PERIOD + " " + DISTRACTORS["ISSUE_DATE"], False),
        ("of the first courtesy notice", NOTICE_PERIOD + " " + NOTICE_PERIOD, True),
        ("of the first courtesy notice", "This is not the saved quote.", False),
    ],
)
def test_j14_short_quote_retains_complete_source_meaning(
    quote: str, source: str, expected: bool
) -> None:
    value = "ISSUE_DATE" if "citation issue date" in quote else "NOTICE_DATE"
    assert clock_quote_supported_by_source(value, quote, source) is expected
