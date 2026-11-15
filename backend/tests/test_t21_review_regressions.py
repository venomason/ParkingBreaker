"""Independent-review regressions through actual research/API/SQLite, using synthetic I/O.

These invented pages and model replies are not official-source or model-accuracy evidence.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

import test_t21_payment_scenarios as base
from parkingbreaker.research.fakes import FakeFetcher, FakeLlmClient, html_response
from parkingbreaker.research.findings import Finding, FindingStatus, apply_quote_guard
from parkingbreaker.research.procedure import quote_procedure_stage
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.sources import html_to_text
from parkingbreaker.research_records import decode_research, research_json
from samples import source
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution
from test_t21_stage_sections import mixed_inputs, run_case

pytestmark = pytest.mark.anyio
LOCAL = "If you choose to file online, you must sign the portal form."
CHOICE = "You may instead mail your review and all attachments."
HEADING = "San Francisco Parking Citation Initial Review."


def claim_inputs(
    key: FactKey, quote_kind: str, *, global_rule: str = "", include_absence: bool = True
) -> tuple[FakeFetcher, FakeLlmClient]:
    fetcher, model = base._inputs("first-level review", LOCAL + " " + CHOICE + global_rule, None)
    policy = fetcher.responses[base.POLICY]
    text = html_to_text(policy.body.decode())
    heading = text.splitlines()[0]
    quote = {
        "sentence": LOCAL,
        "heading": heading + "\n" + LOCAL,
        "mail_choice": LOCAL + " " + CHOICE,
        "whole": text,
    }[quote_kind]
    reply = copy.deepcopy(model.reply)
    assert isinstance(reply, dict)
    findings = reply["findings"]
    assert isinstance(findings, list)
    for finding in list(findings):
        assert isinstance(finding, dict)
        if finding["key"] != key.value:
            continue
        claim = copy.deepcopy(finding) if include_absence else finding
        claim.update(
            value=["portal form"],
            verbatim_quote=quote,
            source_sha256s=[hashlib.sha256(policy.body).hexdigest()],
        )
        claim.pop("evidence_basis", None)
        claim.pop("procedure_assessment", None)
        if include_absence:
            findings.append(claim)
    return fetcher, FakeLlmClient(reply)


@pytest.mark.parametrize("key", [FactKey.MANDATORY_FORMS, FactKey.SIGNATURE_REQUIREMENT])
@pytest.mark.parametrize("quote_kind", ["sentence", "heading", "mail_choice", "whole"])
async def test_optional_portal_claim_with_surrounding_text_preserves_mail(
    tmp_path: Path, key: FactKey, quote_kind: str
) -> None:
    current = await run_case(tmp_path, claim_inputs(key, quote_kind))
    try:
        current.reopen()
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        result = decode_research(saved.research_json)
        assert current.real.status(current.case).state == "DRAFTING"
        assert result.resolve().ready
        assert research_json(result) == saved.research_json
        assert len(current.model.calls) == 1
        assert all("capture" not in path for _, path, *_ in current.transport.calls)
    finally:
        current.real._store.close()


async def test_optional_item_mention_cannot_borrow_unrelated_compulsory_wording(
    tmp_path: Path,
) -> None:
    current = await run_case(
        tmp_path,
        claim_inputs(
            FactKey.MANDATORY_FORMS,
            "whole",
            global_rule=" The portal form is described above. Appeals must arrive within 21 days.",
        ),
    )
    try:
        current.reopen()
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        assert decode_research(saved.research_json).resolve().ready
        assert current.real.status(current.case).state == "DRAFTING"
    finally:
        current.real._store.close()


async def test_discarding_optional_claim_cannot_invent_missing_absence_evidence(
    tmp_path: Path,
) -> None:
    current = await run_case(
        tmp_path, claim_inputs(FactKey.MANDATORY_FORMS, "whole", include_absence=False)
    )
    try:
        current.reopen()
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        assert not decode_research(saved.research_json).resolve().ready
    finally:
        current.real._store.close()


@pytest.mark.parametrize(
    "global_rule",
    [
        " You must include the signed portal form with every mailed appeal.",
        " You must include agency form A with every mailed appeal.",
        " The portal form is required for every mailed appeal.",
    ],
)
async def test_optional_quote_filter_preserves_separate_mail_requirements(
    tmp_path: Path, global_rule: str
) -> None:
    current = await run_case(
        tmp_path, claim_inputs(FactKey.MANDATORY_FORMS, "whole", global_rule=global_rule)
    )
    try:
        current.reopen()
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        assert not decode_research(saved.research_json).resolve().ready
        assert all("capture" not in path for _, path, *_ in current.transport.calls)
    finally:
        current.real._store.close()


@pytest.mark.parametrize(
    "rule",
    [
        "All appeals must include agency form A.",
        "Every appeal must include agency form A.",
        "All parking citation appeals must include agency form A.",
        "Every parking citation review must include agency form A.",
        "All contests must include agency form A.",
        "Each appeal must include agency form A.",
    ],
)
async def test_universal_mail_rule_in_later_section_cannot_create_permission(
    tmp_path: Path, rule: str
) -> None:
    current = await run_case(tmp_path, mixed_inputs(rule))
    try:
        current.reopen()
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        assert not decode_research(saved.research_json).resolve().ready
        assert current.real.status(current.case).state != "DRAFTING"
        assert all("capture" not in path for _, path, *_ in current.transport.calls)
    finally:
        current.real._store.close()


@pytest.mark.parametrize("later_heading", ["Oakland", "San Francisco"])
async def test_complete_assessment_cannot_combine_separate_same_stage_sections(
    tmp_path: Path, later_heading: str
) -> None:
    fetcher, model = base._inputs("first-level review", "Payment plans are available.", None)
    old = fetcher.responses[base.PROCEDURE].body.decode()
    old_when = "Contest within 21 calendar days of the citation issue date."
    new_when = "Contest within 30 calendar days of the citation issue date."
    html = old.replace("<p>" + old_when + "</p>", "") + (
        f"<h2>{later_heading} Parking Citation Initial Review.</h2><p>{new_when}</p>"
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
        if finding["verbatim_quote"] == old_when:
            finding["verbatim_quote"] = new_when
        if finding["key"] == FactKey.DEADLINE_DAYS.value:
            finding["value"] = 30
        assessment = finding.get("procedure_assessment")
        if isinstance(assessment, dict):
            finding["verbatim_quote"] = text
            assessment["source_sha256"] = sha
            assessment["procedure_text"] = text
            assessment["when_quote"] = new_when
    fetcher.responses = {**fetcher.responses, base.PROCEDURE: html_response(base.PROCEDURE, html)}
    current = await run_case(tmp_path, (fetcher, FakeLlmClient(reply)))
    try:
        current.reopen()
        saved = current.real._store.get_research(current.case)
        assert saved is not None
        assert not decode_research(saved.research_json).resolve().ready
        assert current.real.status(current.case).state != "DRAFTING"
    finally:
        current.real._store.close()


def test_model_scope_and_fact_cannot_use_separate_sections_with_the_same_stage() -> None:
    quote = "Contest within 30 calendar days of the citation issue date."
    page = source(
        HEADING + "\nContest within 21 days.\nOakland Parking Citation Initial Review.\n" + quote
    )
    claim = Finding(
        FactKey.DEADLINE_DAYS,
        30,
        FindingStatus.CONFIRMED,
        quote,
        (page.sha256,),
        jurisdiction="us-ca-san_francisco",
        stage="first-level review",
        scope_quote=HEADING,
    )
    (guarded,) = apply_quote_guard([claim], [page])
    assert guarded.status is FindingStatus.UNKNOWN


def test_repeated_quote_in_distinct_same_stage_sections_is_ambiguous() -> None:
    quote = "Contest within 21 days of issue."
    text = HEADING + "\n" + quote + "\nOakland Parking Citation Initial Review.\n" + quote
    assert quote_procedure_stage(text, quote) is None
