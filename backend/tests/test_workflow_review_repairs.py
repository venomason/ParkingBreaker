"""Synthetic requirements; these do not qualify a real city's workflow."""

from dataclasses import replace

import pytest

from parkingbreaker.research.findings import Finding, FindingStatus, consolidate
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.run import ResearchResult
from parkingbreaker.research.sources import AuthorityClass
from parkingbreaker.research.workflow import unsupported_workflow_requirements
from samples import source
from test_customer_runtime_four_sources import SEATTLE_QUOTE
from test_tier_acceptance import extract_case, model_answers, policy_page, requirement


def election_result(wording: str) -> ResearchResult:
    claims = [f for f in model_answers() if f["key"] != "REQUIRED_ITEMS"]
    claims.append(requirement(FactKey.REQUIRED_ITEMS, wording))
    return extract_case(claims, policy=policy_page(wording))[0]


@pytest.mark.parametrize(
    "wording",
    [
        "Indicate whether you are requesting a mitigation or contested hearing.",
        "You must indicate whether you want a contested or mitigation hearing.",
        "Please check one box: Mitigation Hearing or Contested Hearing.",
        "You are required to select either a contested hearing or a mitigation hearing.",
    ],
)
def test_mandatory_election_variants(wording: str) -> None:
    result = election_result(wording)
    blocked = unsupported_workflow_requirements(result.ticket, result.findings, result.sources)
    assert (FactKey.REQUIRED_ITEMS, wording) in blocked
    assert all(quote == wording for _, quote in blocked)


@pytest.mark.parametrize(
    "wording",
    [
        "You may choose a mitigation hearing or a contested hearing. "
        "You must choose a return address.",
        "You must choose a return address; you may choose mitigation or contested hearing.",
        "You must choose a return address and may choose mitigation or contested hearing.",
        "You are not required to choose a mitigation hearing or a contested hearing.",
        "If you want a hearing, you may choose mitigation or contested hearing.",
    ],
)
def test_optional_track_not_made_mandatory_by_unrelated_action(wording: str) -> None:
    result = election_result(wording)
    assert unsupported_workflow_requirements(result.ticket, result.findings, result.sources) == ()


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("wrap", [False, True])
def test_secondary_equal_value_evidence_keeps_exact_blocking_quote(
    reverse: bool, wrap: bool
) -> None:
    base = extract_case(model_answers())[0]
    item = "Include your citation number."
    quotes = (item, item + " " + SEATTLE_QUOTE)
    pages = tuple(source("Parking Citation Initial Review\n" + q) for q in quotes)
    claims = tuple(
        Finding(
            FactKey.REQUIRED_ITEMS,
            [item],
            FindingStatus.CONFIRMED,
            q,
            (p.sha256,),
            jurisdiction=base.ticket.jurisdiction,
            stage=base.ticket.stage,
        )
        for q, p in zip(quotes, pages, strict=True)
    )
    if reverse:
        claims = tuple(reversed(claims))
    if wrap:
        claims = consolidate(claims)
    assert unsupported_workflow_requirements(base.ticket, claims, pages) == (
        (FactKey.REQUIRED_ITEMS, quotes[1]),
    )


@pytest.mark.parametrize("mutation", ["secondary", "missing", "stage", "issuer"])
def test_each_constituent_needs_its_own_current_authority(mutation: str) -> None:
    base = extract_case(model_answers())[0]
    item = "Include your citation number."
    page = source("Parking Citation Initial Review\n" + item)
    blocked_page = source("Parking Citation Initial Review\n" + item + " " + SEATTLE_QUOTE)
    first = Finding(
        FactKey.REQUIRED_ITEMS,
        [item],
        FindingStatus.CONFIRMED,
        item,
        (page.sha256,),
        jurisdiction=base.ticket.jurisdiction,
        stage=base.ticket.stage,
    )
    second = replace(
        first, verbatim_quote=item + " " + SEATTLE_QUOTE, source_sha256s=(blocked_page.sha256,)
    )
    if mutation == "secondary":
        blocked_page = replace(blocked_page, authority_class=AuthorityClass.SECONDARY)
    if mutation == "stage":
        second = replace(second, stage="scheduled hearing")
    if mutation == "issuer":
        second = replace(second, jurisdiction="different-court")
    pages = (page,) if mutation == "missing" else (page, blocked_page)
    assert unsupported_workflow_requirements(base.ticket, consolidate((first, second)), pages) == ()


def test_scheduled_label_does_not_waive_a_current_election() -> None:
    wording = "You must choose a mitigation hearing or a contested hearing."
    result = election_result(wording)
    ticket = replace(result.ticket, stage="administrative hearing (scheduled hearing)")
    page = source("Parking Citation Administrative Hearing\n" + wording)
    claim = Finding(
        FactKey.REQUIRED_ITEMS,
        [wording],
        FindingStatus.CONFIRMED,
        wording,
        (page.sha256,),
        jurisdiction=ticket.jurisdiction,
        stage=ticket.stage,
    )
    assert unsupported_workflow_requirements(ticket, (claim,), (page,)) == (
        (FactKey.REQUIRED_ITEMS, wording),
    )
