"""Deterministic customer authorship (audit A007-A013, A083, A096).

The customer's typed statement is the letter body: byte-identical in storage, with no
provider call and no silent rewriting. Unsupported glyphs are named, never substituted.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from parkingbreaker.appeal.adoption import adopt, is_adopted
from parkingbreaker.appeal.drafting import (
    CUSTOMER_SPAN_LIMIT,
    CustomerTextDrafter,
    Draft,
    DraftResult,
    DraftSentence,
    TicketFacts,
    customer_spans,
)
from parkingbreaker.appeal.letter import LetterError, printable, render_letter
from parkingbreaker.appeal.references import Reference
from parkingbreaker.appeal.run import DraftingStatus, run_drafting
from test_appeal_letter import INPUT

NOTES = (
    "I did not park in the red zone. The sign was covered by a tree.\n\n"
    "I have two photos   showing the sign, and I was not loading or unloading."
)

FACTS = TicketFacts(
    citation_number="SF-998877",
    plate="7ABC123",
    issue_date=datetime(2026, 10, 1).date(),
    violation_code="22500",
    violation_description="Parking in a red zone",
)
NOW = datetime(2026, 10, 11, 18, 30, tzinfo=UTC)


def _draft(notes: str) -> Draft:
    return CustomerTextDrafter().draft(notes, (), FACTS).draft


def test_customer_text_is_stored_byte_identical() -> None:
    draft = _draft(NOTES)
    joined = "".join(sentence.text for sentence in draft.sentences)
    assert joined == NOTES
    assert hashlib.sha256(joined.encode()).digest() == hashlib.sha256(NOTES.encode()).digest()


def test_distinctive_characters_and_negations_survive() -> None:
    notes = '  I did NOT pay; the meter read "0:00",  not $4.50!  \n\n\nSecond   line.  '
    joined = "".join(sentence.text for sentence in _draft(notes).sentences)
    assert joined == notes


def test_long_paragraph_is_chunked_without_rewriting() -> None:
    notes = "Word " * 400
    spans = customer_spans(notes)
    assert len(spans) > 1
    assert "".join(span.text for span in spans) == notes


@pytest.mark.parametrize("length", [599, 600, 601])
def test_paragraph_boundary_keeps_every_span_inside_the_limit(length: int) -> None:
    notes = "a" * length + "\n\nSecond paragraph."
    spans = customer_spans(notes)
    assert "".join(span.text for span in spans) == notes
    assert all(len(span.text) <= CUSTOMER_SPAN_LIMIT for span in spans)


def test_trailing_paragraph_breaks_do_not_grow_the_last_span() -> None:
    notes = "a" * 600 + "\n\n\n\n"
    spans = customer_spans(notes)
    assert "".join(span.text for span in spans) == notes
    assert all(len(span.text) <= CUSTOMER_SPAN_LIMIT for span in spans)
    assert all(span.text.strip() for span in spans)


@pytest.mark.parametrize("breaks", ["\n\n", "\n\n\n\n", "\n\n\n\n\n\n"])
def test_trailing_breaks_never_create_a_whitespace_only_span(breaks: str) -> None:
    notes = "a" * 598 + breaks
    spans = customer_spans(notes)
    assert "".join(span.text for span in spans) == notes
    assert all(len(span.text) <= CUSTOMER_SPAN_LIMIT for span in spans)
    assert all(span.text.strip() for span in spans)


def test_customer_spans_carry_their_own_provenance() -> None:
    assert all("customer:verbatim" in span.support for span in _draft(NOTES).sentences)


def test_drafter_makes_no_model_calls_and_uses_no_usage() -> None:
    result = CustomerTextDrafter().draft(NOTES, (), FACTS)
    assert result.usage.input_tokens == result.usage.output_tokens == 0
    assert CustomerTextDrafter().deterministic is True


def test_drafting_runs_once_without_a_redraft_loop() -> None:
    drafts: list[str] = []

    class Counting(CustomerTextDrafter):
        def draft(
            self,
            notes: str,
            references: Sequence[Reference],
            facts: TicketFacts,
            problems: Sequence[str] = (),
        ) -> DraftResult:
            drafts.append(notes)
            return super().draft(notes, references, facts, problems)

    result = run_drafting(
        NOTES,
        (),
        FACTS,
        Counting(),
        lambda: NOW,
        NOW,
        lambda: INPUT.agency,
        new_revision=lambda: "rev-1",
    )
    assert result.status is DraftingStatus.READY
    assert result.attempts == 1
    assert drafts == [NOTES]
    assert all(span.id.startswith("rev-1:customer-") for span in result.spans)


def test_unsupported_glyph_is_named_before_approval() -> None:
    notes = "I parked near the caf\u00e9 \u2014 honestly."
    result = run_drafting(
        notes,
        (),
        FACTS,
        CustomerTextDrafter(),
        lambda: NOW,
        NOW,
        lambda: INPUT.agency,
        new_revision=lambda: "rev-2",
    )
    assert result.status is DraftingStatus.CUSTOMER_REVIEW
    details = " ".join(detail for flag in result.flags for detail in flag.details)
    assert "\u2014" in details
    with pytest.raises(LetterError, match="0x2014"):
        printable(notes)


def test_expired_window_still_stops_deterministic_drafting() -> None:
    result = run_drafting(
        NOTES,
        (),
        FACTS,
        CustomerTextDrafter(),
        lambda: NOW + timedelta(hours=3),
        NOW,
        lambda: INPUT.agency,
        new_revision=lambda: "rev-3",
    )
    assert result.status is DraftingStatus.WINDOW_EXPIRED


def test_letter_frame_has_no_stock_dismissal_or_advocacy() -> None:
    letter = render_letter(dataclasses.replace(INPUT, draft=_draft(NOTES)))
    assert "dismiss" not in letter.text.casefold()
    assert NOTES.split("\n\n")[0] in letter.text
    assert "I have two photos   showing the sign" in letter.text


def test_one_character_edit_cannot_legitimize_an_earlier_draft() -> None:
    legacy = Draft((DraftSentence("The meter was broken.", (), 0),))
    letter = render_letter(dataclasses.replace(INPUT, draft=legacy))
    adoption = adopt(letter.text, "customer", lambda: NOW)
    edited = letter.text.replace("broken", "broker")
    assert not is_adopted(adoption, edited)
    assert is_adopted(adoption, letter.text)
