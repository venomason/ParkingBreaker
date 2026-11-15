"""Procedural-only research and closed reference injection (audit A014-A019, A082, A095).

The service researches procedure (deadline, address, required items, declarations) and
never the customer's defence, substantive code law or narrative code quotations. Retired
keys stay decodable so old records load, and they can never restart a research search.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from parkingbreaker.appeal.drafting import CustomerTextDrafter, TicketFacts
from parkingbreaker.appeal.letter import render_letter
from parkingbreaker.appeal.references import (
    Reference,
    UnknownReference,
    available_references,
    select_references,
)
from parkingbreaker.research import extractor as extractor_module
from parkingbreaker.research.findings import Finding, FindingStatus
from parkingbreaker.research.questions import (
    ANSWERABLE_KEYS,
    DEFAULT_TIERS,
    RETIRED_KEYS,
    FactKey,
    FactTier,
    build_questions,
)
from parkingbreaker.research.run import RecoveryConfig, run_research
from test_appeal_letter import INPUT
from test_research_run import (
    SEEDS,
    TEST_AGENCY,
    TICKET,
    TickingClock,
    answers,
    fetcher,
)

RECOVERY = RecoveryConfig(retry_delays=())


def test_retired_key_stays_present_and_nice_to_have() -> None:
    # Frozen findings.py indexes DEFAULT_TIERS for every FactKey; the key must exist.
    assert set(DEFAULT_TIERS) == set(FactKey)
    assert DEFAULT_TIERS[FactKey.CODE_EXCEPTIONS] is FactTier.NICE_TO_HAVE
    assert frozenset({FactKey.CODE_EXCEPTIONS}) == RETIRED_KEYS
    assert FactKey.CODE_EXCEPTIONS not in ANSWERABLE_KEYS


def test_build_questions_never_asks_for_defences() -> None:
    questions = build_questions(TICKET)
    assert FactKey.CODE_EXCEPTIONS not in {q.key for q in questions}
    for question in questions:
        assert "defense" not in question.ask.casefold()
        assert "exception" not in question.ask.casefold()


def test_legacy_enum_and_schema_still_decode_the_retired_key() -> None:
    assert FactKey("CODE_EXCEPTIONS") is FactKey.CODE_EXCEPTIONS
    # A saved record carrying the retired key still loads, inertly.
    answer: dict[str, object] = {
        "key": "CODE_EXCEPTIONS",
        "status": "CONFIRMED",
        "value": ["loading passengers"],
        "verbatim_quote": "does not apply to vehicles loading passengers",
        "source_sha256s": [],
    }
    parsed = extractor_module.parse_findings({"findings": [answer]})  # type: ignore[list-item]
    assert [f.key for f in parsed] == [FactKey.CODE_EXCEPTIONS]


def test_available_references_exposes_no_code_or_exceptions() -> None:
    source = "a" * 64
    findings = (
        Finding(
            FactKey.GOVERNING_CODE_SECTION, "Sec. 7.2.22", FindingStatus.CONFIRMED, "q", (source,)
        ),
        Finding(FactKey.CODE_EXCEPTIONS, ["loading"], FindingStatus.CONFIRMED, "q", (source,)),
    )
    assert available_references(findings, ()) == ()


def test_select_references_rejects_any_legacy_selection() -> None:
    # The application now offers nothing, so any saved selection is unknown, never injected.
    with pytest.raises(UnknownReference):
        select_references((), {"code-section"})
    with pytest.raises(UnknownReference):
        select_references((), {"exception-1"})
    handcrafted = (Reference("code-section", FactKey.GOVERNING_CODE_SECTION, "x", "q", ""),)
    assert select_references(handcrafted, ()) == ()


def test_recovery_never_searches_the_retired_key() -> None:
    from parkingbreaker.research.fakes import FakeExtractor, FakeSearcher

    def produce(sources: object) -> list[Finding]:
        return [
            *answers().produce(sources),  # type: ignore[arg-type]
            Finding(FactKey.CODE_EXCEPTIONS, None, FindingStatus.UNKNOWN, None, ()),
        ]

    search = FakeSearcher(lambda request: [])
    result = run_research(
        TICKET,
        TEST_AGENCY,
        SEEDS,
        fetcher(),
        FakeExtractor(produce),
        TickingClock(),
        searcher=search,
        recovery=RECOVERY,
        wait=lambda _: None,
    )
    assert all(FactKey.CODE_EXCEPTIONS not in request.keys for request in search.calls)
    assert FactKey.CODE_EXCEPTIONS not in {f.key for f in result.findings}
    assert result.resolve().ready


def test_customer_statement_stays_apart_from_administrative_facts() -> None:
    notes = "I did not park there; the sign was covered."
    facts = TicketFacts(
        citation_number="SF-998877",
        plate="7ABC123",
        issue_date=INPUT.letter_date,
        violation_code="22500",
        violation_description="Red zone",
    )
    draft = CustomerTextDrafter().draft(notes, (), facts).draft
    letter = render_letter(replace(INPUT, draft=draft))
    body_start = letter.text.index("To Whom It May Concern:")
    assert notes in letter.text[body_start:]
    assert "SF-998877" in letter.text[:body_start]
    assert "SF-998877" not in letter.text[body_start:]
