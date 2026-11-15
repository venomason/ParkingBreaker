"""Bounded independent-review regressions; all pages/model replies are synthetic."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

import test_t21_payment_scenarios as base
from parkingbreaker.research.fakes import FakeLlmClient, html_response
from parkingbreaker.research.findings import Finding, FindingStatus, apply_quote_guard
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.sources import html_to_text
from parkingbreaker.research_records import decode_research, research_json
from samples import source
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution
from test_research_records import result_fixture
from test_t21_stage_sections import mixed_inputs, run_case

pytestmark = pytest.mark.anyio
HEADING = "San Francisco Parking Citation Initial Review."


@pytest.mark.parametrize("scope", ["absent", "foreign", "local"])
async def test_complete_local_procedure_cannot_authorize_a_foreign_sections_fact(
    tmp_path: Path, scope: str
) -> None:
    fetcher, model = base._inputs("first-level review", "Payment plans are available.", None)
    foreign_heading = "Oakland Parking Citation Initial Review."
    quote = "Contest within 30 calendar days of the citation issue date."
    html = fetcher.responses[base.PROCEDURE].body.decode() + (
        f"<h2>{foreign_heading}</h2><p>{quote}</p>"
    )
    text = html_to_text(html)
    sha = hashlib.sha256(html.encode()).hexdigest()
    reply = copy.deepcopy(model.reply)
    assert isinstance(reply, dict)
    findings = reply["findings"]
    assert isinstance(findings, list)
    for finding in findings:
        assert isinstance(finding, dict)
        finding["source_sha256s"] = [sha]
        if finding["key"] == FactKey.DEADLINE_DAYS.value:
            finding["value"] = 30
            finding["verbatim_quote"] = quote
            if scope != "absent":
                finding["scope_quote"] = (
                    foreign_heading if scope == "foreign" else text.splitlines()[0]
                )
        assessment = finding.get("procedure_assessment")
        if isinstance(assessment, dict):
            finding["verbatim_quote"] = text
            assessment["source_sha256"] = sha
            assessment["procedure_text"] = text
            # All completeness anchors remain in the valid local procedure. Only
            # the independent fact is laundered from the foreign same-stage section.
    fetcher.responses = {**fetcher.responses, base.PROCEDURE: html_response(base.PROCEDURE, html)}
    current = await run_case(tmp_path, (fetcher, FakeLlmClient(reply)))
    try:
        current.reopen()
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        result = decode_research(saved.research_json)
        assert not result.resolve().ready
        status = current.real.status(current.case)
        assert status.state == "STOPPED_NO_CHARGE"
        assert status.stop is not None
        assert status.stop.family == "NOT_YET_VERIFIED"
        assert research_json(result) == saved.research_json
        assert all("capture" not in path for _, path, *_ in current.transport.calls)
    finally:
        current.real._store.close()


@pytest.mark.parametrize("separator", [". ", "; ", ": "])
def test_optional_item_cannot_borrow_a_requirement_from_another_clause(separator: str) -> None:
    text = (
        HEADING + "\nIf you choose to file online, you must sign the portal form.\n"
        "The portal form is described above" + separator + "Appeals must arrive within 21 days."
    )
    page = source(text)
    claim = Finding(
        FactKey.MANDATORY_FORMS,
        ["portal form"],
        FindingStatus.CONFIRMED,
        text,
        (page.sha256,),
        jurisdiction="us-ca-san_francisco",
        stage="first-level review",
    )
    original = result_fixture()
    current = replace(
        original, sources=(*original.sources, page), findings=(*original.findings, claim)
    ).reassess()
    for _ in range(3):
        encoded = research_json(current)
        current = decode_research(encoded)
        assert current.resolve().ready
        assert research_json(current) == encoded
        current = current.reassess()


@pytest.mark.parametrize(
    ("rule", "ready"),
    [
        ("Every appeal for an administrative hearing must include agency form H.", True),
        ("Every administrative hearing appeal must include agency form H.", True),
        ("Every appeal must include agency form A.", False),
        ("Every appeal for an initial review must include agency form A.", False),
        (
            "Every appeal for an administrative hearing must include agency form H "
            "and all initial reviews must include agency form A.",
            False,
        ),
        (
            "Every appeal for an administrative hearing must include agency form H; "
            "All initial reviews must include agency form A.",
            False,
        ),
        (
            "Every appeal for an administrative hearing must include agency form H: "
            "All initial reviews must include agency form A.",
            False,
        ),
    ],
)
async def test_stage_qualified_universal_rule_does_not_become_global(
    tmp_path: Path, rule: str, ready: bool
) -> None:
    current = await run_case(tmp_path, mixed_inputs(rule))
    try:
        current.reopen()
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        result = decode_research(saved.research_json)
        assert result.resolve().ready is ready
        if ready:
            assert current.real.status(current.case).state == "DRAFTING"
        assert all("capture" not in path for _, path, *_ in current.transport.calls)
    finally:
        current.real._store.close()


@pytest.mark.parametrize("issuer", ["San Francisco", "Oakland"])
def test_generic_stage_heading_inherits_explicit_source_issuer(issuer: str) -> None:
    quote = "Contest within 30 calendar days of the citation issue date."
    page = source(
        f"{issuer} Parking Citation Initial Review.\nContest within 21 days.\n"
        "Administrative Hearing.\n" + quote
    )
    claim = Finding(
        FactKey.DEADLINE_DAYS,
        30,
        FindingStatus.CONFIRMED,
        quote,
        (page.sha256,),
        jurisdiction="us-ca-san_francisco",
        stage="administrative hearing",
        scope_quote="Administrative Hearing.",
    )
    (guarded,) = apply_quote_guard([claim], [page])
    assert guarded.status is (
        FindingStatus.CONFIRMED if issuer == "San Francisco" else FindingStatus.UNKNOWN
    )


def test_ambiguous_heading_cannot_carry_issuer_scope_into_a_later_generic_stage() -> None:
    quote = "Contest within 30 calendar days of the citation issue date."
    page = source(
        HEADING + "\nContest within 21 days.\nOakland Initial Review Instructions\n"
        "Administrative Hearing.\n" + quote
    )
    claim = Finding(
        FactKey.DEADLINE_DAYS,
        30,
        FindingStatus.CONFIRMED,
        quote,
        (page.sha256,),
        jurisdiction="us-ca-san_francisco",
        stage="administrative hearing",
    )
    (guarded,) = apply_quote_guard([claim], [page])
    assert guarded.status is FindingStatus.UNKNOWN


@pytest.mark.parametrize(
    "title",
    ["form A: Initial Review Request", "form A; Initial Review Request"],
)
@pytest.mark.parametrize("damage", [None, "quote", "hash"])
def test_punctuation_without_optional_statements_preserves_required_form(
    title: str, damage: str | None
) -> None:
    text = HEADING + "\nYou must include " + title + "."
    page = source(text)
    claim = Finding(
        FactKey.MANDATORY_FORMS,
        [title],
        FindingStatus.CONFIRMED,
        text if damage != "quote" else text.replace("must", "shall"),
        (page.sha256,) if damage != "hash" else ("unmatched",),
        jurisdiction="us-ca-san_francisco",
        stage="first-level review",
    )
    (guarded,) = apply_quote_guard([claim], [page])
    assert guarded.status is (FindingStatus.CONFIRMED if damage is None else FindingStatus.UNKNOWN)
    if damage is None:
        assert guarded.value == [title]
        assert guarded.verbatim_quote == text
        assert guarded.source_sha256s == (page.sha256,)
