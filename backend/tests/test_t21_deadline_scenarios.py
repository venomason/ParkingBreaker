"""J12/J13 synthetic contracts through actual research, ASGI and SQLite reopen.

Sources and model replies are invented test inputs, not official-source qualification.
Issuer/provider I/O uses the existing fakes; extraction and policy guards are real.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import timedelta
from pathlib import Path

import pytest

import test_t21_payment_scenarios as base
from parkingbreaker.api import schemas as s
from parkingbreaker.research.fakes import FakeFetcher, FakeLlmClient, html_response
from parkingbreaker.research.findings import FindingStatus
from parkingbreaker.research.ladder import CountingRule
from parkingbreaker.research.questions import ANSWERABLE_KEYS, FactKey
from parkingbreaker.research.resolution import ResearchAnswers
from parkingbreaker.research.run import ResearchResult
from parkingbreaker.research.sources import html_to_text
from parkingbreaker.research_records import decode_answers, decode_research, research_json
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution
from test_t21_stage_sections import run_case

pytestmark = pytest.mark.anyio
PERIOD = "Contest within 21 calendar days of the citation issue date."
RECEIPT = "Your mailed contest must be received by the deadline."
NEGATED_POSTMARK = "Contests must be received by the deadline; postmarks are not accepted."
AFFIRMATIVE_POSTMARK = "Appeals must be postmarked by the deadline."


def deadline_inputs(
    key: FactKey, value: int | str, quote: str
) -> tuple[FakeFetcher, FakeLlmClient]:
    fetcher, model = base._inputs("first-level review", "Payment plans are available.", None)
    old = PERIOD if key is FactKey.DEADLINE_DAYS else RECEIPT
    original = fetcher.responses[base.PROCEDURE].body.decode()
    assert old in original
    html = original.replace(old, quote)
    text = html_to_text(html)
    sha = hashlib.sha256(html.encode()).hexdigest()
    reply = copy.deepcopy(model.reply)
    assert isinstance(reply, dict)
    findings = reply["findings"]
    assert isinstance(findings, list)
    for finding in findings:
        assert isinstance(finding, dict)
        finding["source_sha256s"] = [sha]
        if finding["key"] == key.value:
            finding["value"] = value
            finding["verbatim_quote"] = quote
        assessment = finding.get("procedure_assessment")
        if isinstance(assessment, dict):
            finding["verbatim_quote"] = text
            assessment["source_sha256"] = sha
            assessment["procedure_text"] = text
            # The actual contest-period anchor remains21 days even for the bad
            # model value; completeness never borrows the late-fee period.
            assert assessment["when_quote"] == PERIOD
    fetcher.responses = {**fetcher.responses, base.PROCEDURE: html_response(base.PROCEDURE, html)}
    return fetcher, FakeLlmClient(reply)


def assert_durable_result(
    current: base.Scenario, inputs: tuple[FakeFetcher, FakeLlmClient], key: FactKey, quote: str
) -> tuple[ResearchResult, ResearchAnswers]:
    current.reopen()
    saved = current.real._store.get_research(current.case)
    assert saved is not None
    result = decode_research(saved.research_json)
    answers = decode_answers(saved.answers_json)
    assert research_json(result) == saved.research_json
    source = next(page for page in result.sources if page.final_url == base.PROCEDURE)
    response = inputs[0].responses[base.PROCEDURE]
    assert source.sha256 == hashlib.sha256(response.body).hexdigest()
    assert source.text == html_to_text(response.body.decode())
    assert quote in source.text
    assert any(f.key is key for f in result.findings)
    assert {f.key for f in result.findings} == set(ANSWERABLE_KEYS)
    assert all(f.status is FindingStatus.CONFIRMED for f in result.findings if f.key is not key)
    assert all("capture" not in path for _, path, *_ in current.transport.calls)
    # A second reopen/replay must reuse durable work, without another extraction,
    # source fetch or Stripe mutation.
    calls = (len(current.model.calls), list(inputs[0].calls), list(current.transport.calls))
    current.reopen()
    current.real.step(current.case, s.WorkerStep(step_id="research"))
    replay = current.real._store.get_research(current.case)
    assert replay is not None
    assert replay.research_json == saved.research_json
    assert replay.answers_json == saved.answers_json
    assert calls == (
        len(current.model.calls),
        list(inputs[0].calls),
        list(current.transport.calls),
    )
    return result, answers


@pytest.mark.parametrize(
    "quote",
    [
        PERIOD + " Late fees apply after 45 days.",
        "Late fees begin within 45 days; " + PERIOD,
    ],
)
@pytest.mark.parametrize("days", [21, 45])
async def test_j12_contest_period_cannot_borrow_late_fee_days(
    tmp_path: Path, quote: str, days: int
) -> None:
    inputs = deadline_inputs(FactKey.DEADLINE_DAYS, days, quote)
    current = await run_case(tmp_path, inputs)
    try:
        result, answers = assert_durable_result(current, inputs, FactKey.DEADLINE_DAYS, quote)
        finding = next(f for f in result.findings if f.key is FactKey.DEADLINE_DAYS)
        assert finding.status is (FindingStatus.CONFIRMED if days == 21 else FindingStatus.UNKNOWN)
        resolved = result.resolve(answers)
        if days == 21:
            assert finding.value == 21
            assert finding.verbatim_quote == quote
            assert finding.source_sha256s
            assert resolved.ready
            assert resolved.deadline is not None
            assert resolved.deadline.deadline == result.ticket.issue_date + timedelta(days=21)
            assert current.real.status(current.case).state == "DRAFTING"
        else:
            assert finding.value is None
            assert not resolved.ready
            assert resolved.deadline is None
            assert not resolved.official_deadline_candidates
            # Intake records no printed deadline, and this closed-list question
            # is not a visible customer card; missing timing stops without charge.
            assert answers.printed_deadline is None
            assert current.real.status(current.case).state == "STOPPED_NO_CHARGE"
            assert current.real.status(current.case).payment_state == "RELEASED"
            stop = current.real.status(current.case).stop
            assert stop is not None
            assert stop.family == "NOT_YET_VERIFIED"
            assert FactKey.DEADLINE_DAYS in {key for p in resolved.pending for key in p.keys}
    finally:
        current.real._store.close()


@pytest.mark.parametrize(
    ("quote", "value", "confirmed"),
    [
        (NEGATED_POSTMARK, "RECEIPT", True),
        (NEGATED_POSTMARK, "POSTMARK", False),
        ("Postmarks are not accepted.", "RECEIPT", False),
        ("Postmarks are not accepted.", "POSTMARK", False),
        (AFFIRMATIVE_POSTMARK, "POSTMARK", True),
        (AFFIRMATIVE_POSTMARK, "RECEIPT", False),
    ],
)
async def test_j13_source_counting_rule_cannot_be_invented_or_reversed(
    tmp_path: Path, quote: str, value: str, confirmed: bool
) -> None:
    inputs = deadline_inputs(FactKey.DEADLINE_RULE, value, quote)
    current = await run_case(tmp_path, inputs)
    try:
        result, answers = assert_durable_result(current, inputs, FactKey.DEADLINE_RULE, quote)
        finding = next(f for f in result.findings if f.key is FactKey.DEADLINE_RULE)
        assert finding.status is (FindingStatus.CONFIRMED if confirmed else FindingStatus.UNKNOWN)
        resolved = result.resolve(answers)
        assert resolved.ready is confirmed
        assert resolved.counting is not None
        if confirmed:
            assert finding.value == value
            assert finding.verbatim_quote == quote
            assert finding.source_sha256s
            assert resolved.counting.rule is CountingRule[value]
            assert not resolved.counting.disclose
            assert current.real.status(current.case).state == "DRAFTING"
        else:
            assert finding.value is None
            assert resolved.counting.rule is CountingRule.RECEIPT
            assert resolved.counting.disclose
            assert resolved.timing_disclosures
            assert FactKey.DEADLINE_RULE in {key for p in resolved.pending for key in p.keys}
            assert current.real.status(current.case).state == "STOPPED_NO_CHARGE"
            assert current.real.status(current.case).payment_state == "RELEASED"
            stop = current.real.status(current.case).stop
            assert stop is not None
            assert stop.family == "NOT_YET_VERIFIED"
    finally:
        current.real._store.close()
