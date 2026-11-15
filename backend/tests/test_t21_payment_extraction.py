"""Payment evidence uses the existing extraction call and resolved issuer authority."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from parkingbreaker.json_types import JsonValue
from parkingbreaker.llm import Usage
from parkingbreaker.research.extractor import LlmExtractor
from parkingbreaker.research.fakes import FakeLlmClient, html_response
from parkingbreaker.research.payment_effects import PaymentOutcome, evaluate_payment_rule
from parkingbreaker.research.questions import build_questions
from parkingbreaker.research.run import run_research
from parkingbreaker.research_records import research_json
from samples import source
from test_research_run import CONTEST_SEED, CONTEST_URL, TEST_AGENCY, TICKET, TickingClock, fetcher
from test_t21_payment_policy import FINE, HEADING, SCOPE
from test_t21_payment_records import payment_research


def extracted_reply() -> dict[str, JsonValue]:
    payload = json.loads(research_json(payment_research()))["payload"]
    return {"findings": [], "payment_rules": payload["payment_rules"]}


def test_one_existing_model_call_carries_rule_evidence_separately_from_core_findings() -> None:
    client = FakeLlmClient(extracted_reply(), Usage())
    extraction = LlmExtractor(client).extract(build_questions(TICKET), (source(HEADING + FINE),))
    assert len(client.calls) == 1
    assert len(extraction.findings) == 15  # retired defence key
    assert len(extraction.payment_rules) == 1
    assert extraction.payment_rules[0].scope == SCOPE


@pytest.mark.parametrize("bad", [None, "bad", [{}], [{"action": "UNKNOWN"}]])
def test_malformed_present_rule_output_survives_as_unresolved_instead_of_disappearing(
    bad: object,
) -> None:
    reply = extracted_reply()
    reply["payment_rules"] = bad  # type: ignore[assignment]
    extraction = LlmExtractor(FakeLlmClient(reply, Usage())).extract(
        build_questions(TICKET), (source(HEADING + FINE),)
    )
    assert extraction.payment_rules
    assert all(
        evaluate_payment_rule(
            item, (source(HEADING + FINE),), SCOPE, action_occurred=False, answered=True
        ).outcome
        is PaymentOutcome.UNRESOLVED
        for item in extraction.payment_rules
    )


def test_omitted_rule_cannot_erase_a_visible_explicit_payment_effect() -> None:
    extraction = LlmExtractor(FakeLlmClient({"findings": []}, Usage())).extract(
        build_questions(TICKET), (source(HEADING + FINE),)
    )
    assert extraction.payment_rules


def test_unrelated_payment_navigation_does_not_create_a_new_critical_gap() -> None:
    page = source(
        "City Parking Administration decides initial written reviews.\nPay parking citation online"
    )
    extraction = LlmExtractor(FakeLlmClient({"findings": []}, Usage())).extract(
        build_questions(TICKET), (page,)
    )
    assert extraction.payment_rules == ()


def test_a_later_hearing_deposit_does_not_create_an_initial_review_gate() -> None:
    page = source("A hearing deposit is required to request an administrative hearing.")
    extraction = LlmExtractor(FakeLlmClient({"findings": []}, Usage())).extract(
        build_questions(TICKET, stage="first-level review"), (page,)
    )
    assert extraction.payment_rules == ()
    hearing = LlmExtractor(FakeLlmClient({"findings": []}, Usage())).extract(
        build_questions(TICKET, stage="administrative hearing"), (page,)
    )
    assert hearing.payment_rules


def test_research_binds_expected_scope_to_resolved_agency_not_model_labels() -> None:
    transport = fetcher()
    transport.responses = {
        **transport.responses,
        CONTEST_URL: html_response(CONTEST_URL, HEADING + FINE),
    }
    agency = replace(TEST_AGENCY, deciding_body="Resolved office differs from model claim")
    result = run_research(
        TICKET,
        agency,
        (CONTEST_SEED,),
        transport,
        LlmExtractor(FakeLlmClient(extracted_reply(), Usage())),
        TickingClock(),
        wait=lambda _: None,
    )
    assert result.payment_rules
    assert result.payment_scope is not None
    assert result.payment_scope.deciding_body == agency.deciding_body
    assert result.payment_scope != result.payment_rules[0].scope
    assert not result.resolve().ready
