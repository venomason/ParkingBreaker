"""Four-source gap regression analogues, with explicit modelled interpretation.

No paid model, real customer or live source. Generic complete synthetic procedure
plus labelled gap mutations exercise the real SQLite/CaseService/payment/mail path.
The Seattle scheduling sentence is exact saved official text; interpretation into a
REQUIRED_ITEMS is hand-authored. This does not qualify an actual city's route.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from item2_helpers import FORM_RULE
from parkingbreaker.api import schemas as s
from parkingbreaker.case_state import CaseState
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.workflow import unsupported_workflow_requirements
from test_case_service import (
    approve_with_preview,
    drafting_case,
    fake_resolution,
    researched_case,
    stripe,
)
from test_effect_dispatch import payment_fixture
from test_tier_acceptance import extract_case, model_answers, policy_page, requirement

ROOT = Path(__file__).resolve().parents[2]
SEATTLE_QUOTE = (
    "Written statements sent to the court without an initial hearing scheduled will not be read. "
    "Instead, a hearing will be set and you will be sent the appropriate written statement form."
)
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fake_resolution")]
assert fake_resolution


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize("case_id", ["B01", "B03", "B06", "B15", "deposit_stage"])
async def test_four_source_gap_analogues_cannot_freeze_charge_or_mail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case_id: str
) -> None:
    findings = model_answers()
    quote = None
    if case_id == "B01":
        # Synthetic mandatory form, not an invented actual LADOT form.
        findings = [f for f in findings if f["key"] not in {"MAILING_ADDRESS", "MANDATORY_FORMS"}]
        findings.append(requirement(FactKey.MANDATORY_FORMS, FORM_RULE))
        quote = FORM_RULE
    elif case_id == "B03":
        assert (
            SEATTLE_QUOTE
            in (
                ROOT
                / "build-kit/evidence/bench-pages"
                / "d6c88c1ed5cbe5fcf13da401321749070edf1cd374bbbe140889cace4afb4c3f.txt"
            ).read_text()
        )
        findings = [f for f in findings if f["key"] != "REQUIRED_ITEMS"]
        findings.append(requirement(FactKey.REQUIRED_ITEMS, SEATTLE_QUOTE))
        quote = SEATTLE_QUOTE
    elif case_id == "deposit_stage":
        quote = "You must enclose an original money order for the hearing deposit."
        findings = [f for f in findings if f["key"] != "REQUIRED_ITEMS"]
        findings.append(requirement(FactKey.REQUIRED_ITEMS, quote))
    else:
        # No payment period or benchmark placeholder supplies contest permission/timing.
        missing = {
            "MAIL_IN_ACCEPTED",
            "MAILING_ADDRESS",
            "DEADLINE_DAYS",
            "DEADLINE_CLOCK_START",
            "DEADLINE_ABSOLUTE",
        }
        findings = [f for f in findings if f["key"] not in missing]
    result, _llm, _searcher = extract_case(findings, policy=policy_page(quote) if quote else None)
    client, real, case = await researched_case(tmp_path, monkeypatch, result)
    payments, transport = stripe(payment_fixture(), payment_fixture("canceled"))
    lob = FakeLobClient([LobResponse(200, "must_not_send", evidence=LetterEvidence.PRODUCTION)])
    object.__setattr__(real._deps, "payments", payments)
    object.__setattr__(real._deps, "lob", lob)
    try:
        status = real.step(case, s.WorkerStep(step_id="research"))
        assert status.state is CaseState.STOPPED_NO_CHARGE
        assert status.stop is not None
        assert status.destination_choice is None
        if case_id in {"B01", "B03", "deposit_stage"}:
            assert status.stop.hard_stop is not None
            assert status.stop.hard_stop.value == "S3"
        if case_id == "B03":
            assert status.stop.family.value == "PROCESS_INCOMPATIBLE"
            assert status.stop.official_quote == SEATTLE_QUOTE
            assert f'The official instructions say: "{SEATTLE_QUOTE}"' in status.stop.message
        for _ in range(3):
            real.advance_due()
        record = real._intake(case)
        assert real._store.get_draft(case) is None
        assert real._store.get(record.case_token or "", 1) is None
        assert real._store.get_approval_manifest(record.case_token or "", 1) is None
        assert not lob.calls
        assert not any(call[1].endswith("/capture") for call in transport.calls)
        assert any(call[1].endswith("/cancel") for call in transport.calls)
        assert real.status(case).payment_state is PaymentState.RELEASED
    finally:
        await client.aclose()
        real._store.close()


async def test_supported_first_stage_keeps_single_fake_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, case = await drafting_case(tmp_path, monkeypatch)
    try:
        real.step(case, s.WorkerStep(step_id="draft"))
        draft = real.draft(case)
        adoption = real.adopt_text(
            case, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256)
        )
        real.approve(case, approve_with_preview(real, case, adoption.adopted_sha256))
        payments, transport = stripe(payment_fixture(), payment_fixture("succeeded"))
        lob = FakeLobClient([LobResponse(200, "ltr_supported", evidence=LetterEvidence.PRODUCTION)])
        object.__setattr__(real._deps, "payments", payments)
        object.__setattr__(real._deps, "lob", lob)
        real.dispatch(case)
        assert real.status(case).state is CaseState.IN_TRANSIT
        assert len(lob.calls) == 1
        assert real.status(case).payment_state is PaymentState.CAPTURE_PENDING
        real.dispatch(case)
        assert real.status(case).payment_state is PaymentState.CAPTURED
        assert len([call for call in transport.calls if call[1].endswith("/capture")]) == 1
    finally:
        await client.aclose()
        real._store.close()


async def test_scheduled_stage_does_not_reuse_initial_request_as_its_rule() -> None:
    findings = model_answers()
    findings = [f for f in findings if f["key"] != "REQUIRED_ITEMS"]
    findings.append(requirement(FactKey.REQUIRED_ITEMS, SEATTLE_QUOTE))
    result, _llm, _searcher = extract_case(findings, policy=policy_page(SEATTLE_QUOTE))
    assert unsupported_workflow_requirements(result.ticket, result.findings, result.sources)
    # A different stage cannot reuse initial-stage findings as verified requirements.
    scheduled = replace(result.ticket, stage="scheduled hearing")
    assert unsupported_workflow_requirements(scheduled, result.findings, result.sources) == ()
    assert not replace(result, ticket=scheduled).reassess().resolve().ready


def test_austin_saved_payment_wording_is_not_contest_evidence() -> None:
    from parkingbreaker.address_policy import payment_addresses_in_text

    page = (
        ROOT
        / "build-kit/evidence/bench-pages"
        / ("5230c2239d3b3aa4b3d0b735a91454bee6440175e5d44701a7b96e2c7a07d136.txt")
    )
    assert ("po box 2135", "78768") in payment_addresses_in_text(page.read_text())
    # Explicitly shared wording is a synthetic positive control, not Austin evidence.
    assert not payment_addresses_in_text(
        "Mail payments and appeals to Parking Office, PO Box 22, Testville, CA 94601."
    )


@pytest.mark.parametrize("mutation", ["secondary", "missing_source", "different_stage"])
def test_workflow_limit_requires_guarded_current_official_context(mutation: str) -> None:
    from parkingbreaker.research.sources import AuthorityClass

    fs = [f for f in model_answers() if f["key"] != "REQUIRED_ITEMS"]
    fs.append(requirement(FactKey.REQUIRED_ITEMS, SEATTLE_QUOTE))
    result, _llm, _searcher = extract_case(fs, policy=policy_page(SEATTLE_QUOTE))
    assert unsupported_workflow_requirements(result.ticket, result.findings, result.sources)
    if mutation == "different_stage":
        result = replace(result, ticket=replace(result.ticket, stage="scheduled hearing"))
    else:
        cited = next(f for f in result.findings if f.key is FactKey.REQUIRED_ITEMS).source_sha256s
        sources = tuple(
            replace(page, authority_class=AuthorityClass.SECONDARY)
            if page.sha256 in cited
            else page
            for page in result.sources
        )
        if mutation == "missing_source":
            sources = tuple(p for p in sources if p.sha256 not in cited)
        result = replace(result, sources=sources)
    assert unsupported_workflow_requirements(result.ticket, result.findings, result.sources) == ()
    assert not result.resolve().ready


@pytest.mark.parametrize(
    ("wording", "blocked"),
    [
        ("You must choose either a mitigation hearing or a contested hearing.", True),
        ("You may choose a mitigation hearing or a contested hearing.", False),
        ("Check the box to choose either a mitigation hearing or a contested hearing.", True),
    ],
)
def test_workflow_election_must_be_required_not_an_optional_alternative(
    wording: str, blocked: bool
) -> None:
    fs = [f for f in model_answers() if f["key"] != "REQUIRED_ITEMS"]
    fs.append(requirement(FactKey.REQUIRED_ITEMS, wording))
    result, _llm, _searcher = extract_case(fs, policy=policy_page(wording))
    assert (
        bool(unsupported_workflow_requirements(result.ticket, result.findings, result.sources))
        is blocked
    )
