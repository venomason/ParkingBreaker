"""Adversarial source/grammar checks; all text below is synthetic test data."""

from __future__ import annotations

from dataclasses import replace

import pytest

from parkingbreaker import case_state
from parkingbreaker.research.findings import Finding, FindingStatus
from parkingbreaker.research.fulfilment import (
    packet_fulfillment_pending,
    requirement_fulfilled,
    requirement_interpretable,
)
from parkingbreaker.research.questions import EMPTY_PACKET_PLAN, FactKey
from parkingbreaker.research.resolution import resolution_event
from parkingbreaker.research.route_requirements import (
    compulsory_route_stage,
    possible_route_requirement,
    route_local_requirement,
)
from parkingbreaker.research.sources import AuthorityClass
from parkingbreaker.research_records import decode_research, research_json
from samples import source
from test_research_records import result_fixture

HEADING = "San Francisco Parking Citation Initial Review."
RULE = "You must register your initial review through the online portal before mailing your appeal."


@pytest.mark.parametrize(
    "sentence",
    [
        "Submit your initial review only through the online portal.",
        "Initial review requests must be submitted using the online portal.",
        "Your initial review form must be filed electronically.",
        "You must appear in person for the initial review.",
        RULE,
    ],
)
def test_known_route_is_never_fulfilled_or_deferred(sentence: str) -> None:
    quote = HEADING + "\n" + sentence
    assert compulsory_route_stage(sentence) == "first-level review"
    assert possible_route_requirement(sentence)
    for key in (
        FactKey.REQUIRED_ITEMS,
        FactKey.MANDATORY_FORMS,
        FactKey.SIGNATURE_REQUIREMENT,
        FactKey.TICKET_SPECIFIC_REQUIREMENTS,
    ):
        assert requirement_interpretable(key, sentence, quote)
        assert not packet_fulfillment_pending(key, sentence, quote, plan=EMPTY_PACKET_PLAN)
        assert not requirement_fulfilled(
            key, sentence, quote, EMPTY_PACKET_PLAN, initial_review_completed=True
        )


@pytest.mark.parametrize(
    "damage",
    ["missing_heading", "wrong_issuer", "wrong_type", "wrong_stage", "contact", "hash"],
)
def test_unproved_source_claim_is_not_permission_or_proved_incompatibility(damage: str) -> None:
    original = result_fixture()
    text = HEADING + "\n" + RULE
    if damage == "missing_heading":
        text = RULE
    elif damage == "wrong_issuer":
        text = text.replace("San Francisco", "Oakland")
    elif damage == "wrong_type":
        text = text.replace("Parking Citation", "Moving Citation")
    elif damage == "wrong_stage":
        text = text.replace("Initial Review", "Administrative Hearing")
    page = source(text)
    if damage == "contact":
        page = replace(page, authority_class=AuthorityClass.OFFICIAL_CONTACT)
    elif damage == "hash":
        page = replace(page, sha256="invalid")
    current = replace(original, sources=(*original.sources, page)).reassess()
    resolution = current.resolve()
    assert not resolution.ready
    event = resolution_event(resolution)
    if event is not None:
        assert isinstance(event, case_state.Stopped)
        assert event.reason is case_state.StopReason.NOT_YET_VERIFIED
    else:
        assert resolution.pending


@pytest.mark.parametrize(
    "suffix",
    [
        " and all appeals must include agency form A.",
        ", and mailed reviews must include agency form A.",
        "; You must submit agency form A.",
    ],
)
def test_route_local_classifier_cannot_swallow_global_form_clause(suffix: str) -> None:
    sentence = "If you submit online, you must upload PDF evidence" + suffix
    assert not route_local_requirement(sentence)
    original = result_fixture()
    page = source(HEADING + "\n" + sentence)
    current = replace(original, sources=(*original.sources, page)).reassess()
    assert not current.resolve().ready


def test_route_evidence_roundtrip_and_repeated_recheck_are_stable() -> None:
    original = result_fixture()
    current = replace(
        original, sources=(*original.sources, source(HEADING + "\n" + RULE))
    ).reassess()
    for _ in range(5):
        encoded = research_json(current)
        saved = decode_research(encoded)
        assert research_json(saved) == encoded
        event = resolution_event(saved.resolve())
        assert isinstance(event, case_state.Stopped)
        assert event.reason is case_state.StopReason.PROCESS_INCOMPATIBLE
        assert RULE in event.detail
        current = saved.reassess()
        assert research_json(current) == encoded


@pytest.mark.parametrize("key", [FactKey.MANDATORY_FORMS, FactKey.SIGNATURE_REQUIREMENT])
def test_model_cannot_promote_an_explicit_optional_portal_branch_to_global_requirement(
    key: FactKey,
) -> None:
    sentence = "If you choose to file online, you must sign the portal form."
    page = source(HEADING + "\n" + sentence)
    claim = Finding(
        key,
        ["portal form"],
        FindingStatus.CONFIRMED,
        sentence,
        (page.sha256,),
        jurisdiction="us-ca-san_francisco",
        stage="first-level review",
    )
    original = result_fixture()
    current = replace(
        original, sources=(*original.sources, page), findings=(*original.findings, claim)
    ).reassess()
    assert current.resolve().ready
