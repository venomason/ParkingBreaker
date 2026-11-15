from __future__ import annotations

from datetime import date

import pytest

from parkingbreaker.appeal.drafting import Draft, DraftSentence, TicketFacts
from parkingbreaker.appeal.proofread import (
    BANNED_PHRASES,
    Problem,
    find_code_citations,
    find_dates,
    find_identifiers,
    find_names,
    find_numbers,
    proofread,
)
from parkingbreaker.appeal.references import Reference
from parkingbreaker.research.questions import FactKey

NOTES = (
    "parked on Van Ness at 8:15am on 10/1/2026. "
    "the street cleaning sign was covered by a tree branch. "
    "my plate is 7ABC123 and I have 2 photos. "
    "I paid $1,250 for a permit from SFMTA."
)
FACTS = TicketFacts(
    citation_number="SF-998877",
    plate="7ABC123",
    issue_date=date(2026, 10, 1),
    violation_code="TC 7.2.22",
    violation_description="Street cleaning",
    location="Van Ness Avenue",
)
SECTION = Reference(
    "code-section",
    FactKey.GOVERNING_CODE_SECTION,
    "SEC. 7.2.22",
    "SEC. 7.2.22. STREET CLEANING.",
    "https://x",
)
EXCEPTION = Reference(
    "exception-1",
    FactKey.CODE_EXCEPTIONS,
    "a vehicle stopped to load or unload passengers",
    "shall not apply to a vehicle stopped to load or unload passengers",
    "https://x",
)


def span(fragment: str) -> str:
    start = NOTES.index(fragment)
    return f"note:{start}-{start + len(fragment)}"


SPAN_PARKED = span("parked on Van Ness at 8:15am on 10/1/2026.")
SPAN_SIGN = span("the street cleaning sign was covered by a tree branch.")
SPAN_PLATE = span("my plate is 7ABC123 and I have 2 photos.")
SPAN_PERMIT = span("I paid $1,250 for a permit from SFMTA.")


def check(text: str, *support: str, refs: tuple[Reference, ...] = (SECTION,)) -> list[Problem]:
    draft = Draft((DraftSentence(text, tuple(support)),))
    return list(proofread(draft, NOTES, refs, FACTS, require_all_notes=False))


def kinds(problems: list[Problem]) -> list[str]:
    return [p.kind for p in problems]


# --- Clean sentences pass ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "support"),
    [
        ("Parked on Van Ness at 8:15am on 10/1/2026.", (SPAN_PARKED,)),
        ("The street cleaning sign was covered by a tree branch.", (SPAN_SIGN,)),
        ("My plate is 7ABC123 and I have 2 photos.", (SPAN_PLATE,)),
        ("I paid $1,250 for a permit from SFMTA.", (SPAN_PERMIT,)),
        ("SF-998877", ("fact:citation_number",)),
        ("SEC. 7.2.22", ("code-section",)),
        ("I contest citation SF-998877 for plate 7ABC123.", ("fact:citation_number", "fact:plate")),
    ],
)
def test_supported_sentences_pass(text: str, support: tuple[str, ...]) -> None:
    assert check(text, *support) == []


@pytest.mark.parametrize(
    ("text", "support"),
    [
        ("I parked on Van Ness at 8:15am on October 1, 2026.", (SPAN_PARKED,)),
        ("I parked on Van Ness on 2026-10-01.", (SPAN_PARKED,)),
        ("My plate is 7ABC123, and I have enclosed 2 photos.", (SPAN_PLATE,)),
        ("I parked on Van Ness Avenue.", ("fact:location",)),
        ("I'm told I've got time.", (SPAN_SIGN,)),
    ],
)
def test_tags_and_matching_tokens_do_not_authorize_rewriting_or_new_assertions(
    text: str, support: tuple[str, ...]
) -> None:
    assert "unsupported assertion" in kinds(check(text, *support))


# --- Support tags --------------------------------------------------------------------------


def test_sentence_without_support_fails() -> None:
    assert kinds(check("I parked on Van Ness.")) == ["unsupported"]


@pytest.mark.parametrize(
    ("tag", "message"),
    [
        ("note:0-0", "outside the notes"),
        ("note:10-5", "outside the notes"),
        (f"note:0-{len(NOTES) + 1}", "outside the notes"),
        ("exception-1", "not a selected reference"),  # available but not selected
        ("made-up", "not a selected reference"),
        ("fact:driver_license", "unknown ticket fact"),
        ("note:abc-def", "not a selected reference"),  # not a note tag, so a reference id
    ],
)
def test_unresolvable_tags_fail(tag: str, message: str) -> None:
    problems = check("The street cleaning sign was covered.", SPAN_SIGN, tag)
    assert kinds(problems) == ["bad tag"]
    assert message in problems[0].detail


def test_support_is_per_sentence_not_global() -> None:
    # 7ABC123 is in the notes, but not in the span this sentence cites.
    assert kinds(check("My plate is 7ABC123.", SPAN_SIGN)) == ["unsupported identifier"]


# --- Facts: dates ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "I parked there on October 2, 2026.",
        "I parked there on 10/2/2026.",
        "I parked there on 2025-10-01.",  # wrong year
        "I parked there on Oct 2.",
        "I parked there on 2 October.",
    ],
)
def test_unsupported_dates_fail(text: str) -> None:
    assert "unsupported date" in kinds(check(text, SPAN_PARKED))


def test_date_without_year_matches_any_year() -> None:
    problems = check("I parked on Van Ness on October 1.", SPAN_PARKED)
    assert "unsupported date" not in kinds(problems)
    assert "unsupported assertion" in kinds(problems)


def test_impossible_dates_are_not_dates() -> None:
    dates, _ = find_dates("on 2/30/2026 and 13/1/2026")
    assert dates == []


def test_find_dates_forms() -> None:
    dates, rest = find_dates("10/1/26, 2026-10-01, Oct. 1st, 2026 and 1st of October")
    assert dates == [(2026, 10, 1), (2026, 10, 1), (2026, 10, 1), (None, 10, 1)]
    assert not any(ch.isdigit() for ch in rest)


# --- Facts: numbers, identifiers, names ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("I parked on Van Ness at 9:15am.", "unsupported number"),
        ("I have 3 photos.", "unsupported number"),
        ("I paid $1,500 for a permit from SFMTA.", "unsupported number"),
        ("I paid $125 for a permit from SFMTA.", "unsupported number"),
        ("My plate is 7ABC124.", "unsupported identifier"),
        ("My plate is 8XYZ999.", "unsupported identifier"),
        ("I parked on Polk Street.", "unsupported name"),
        ("Officer Smith covered the sign.", "unsupported name"),
        ("The sign was covered, as the DPT confirmed.", "unsupported name"),
    ],
)
def test_unsupported_facts_fail(text: str, kind: str) -> None:
    support = (SPAN_PARKED, SPAN_SIGN, SPAN_PLATE, SPAN_PERMIT)
    assert kind in kinds(check(text, *support))


def test_first_word_and_i_are_not_names() -> None:
    assert find_names("Yesterday I parked and I'm sure I'd paid.") == []
    assert find_names("Yesterday Maria parked near SFMTA.") == ["Maria", "SFMTA"]
    assert find_names("Smith's car was there.") == []  # first word
    assert find_names("It was Smith's car.") == ["Smith"]


def test_numbers_and_identifiers() -> None:
    assert find_numbers("at 8:15am, $1,250.00 and 2 photos, 1st time") == [
        "8:15",
        "1250.00",
        "2",
        "1",
    ]
    assert find_identifiers("plate 7ABC123, citation SF-998877, 1st, 8am, ABCD, 1234") == [
        "7ABC123",
        "SF-998877",
    ]


# --- Code citations ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Under CVC 22500 the stop was allowed.",
        "Under Vehicle Code 22500 the stop was allowed.",
        "Under § 22500 the stop was allowed.",
        "Under Section 7.2.23 the stop was allowed.",
        "Under Sec. 7.3 the stop was allowed.",
        "Under TC 7.2.20 the stop was allowed.",
        "The Transportation Code allows it.",
    ],
)
def test_code_citations_not_selected_fail(text: str) -> None:
    assert "code not selected" in kinds(check(text, SPAN_SIGN, "code-section"))


def test_code_citation_needs_a_selected_reference_at_all() -> None:
    assert "code not selected" in kinds(
        check("Under SEC. 7.2.22 the stop was allowed.", SPAN_SIGN, refs=())
    )


def test_selected_exception_text_counts() -> None:
    assert (
        check(
            EXCEPTION.quote,
            "exception-1",
            refs=(SECTION, EXCEPTION),
        )
        == []
    )


def test_find_code_citations() -> None:
    found, rest = find_code_citations("See §§ 40215 and CVC § 22500 and Sec. 7.2.22.")
    assert sorted(n for _, n in found if n) == ["22500", "40215", "7.2.22"]
    assert "40215" not in rest


# --- Banned phrases ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "This is not legal advice.",
        "We guarantee this result.",
        "I will win this appeal.",
        "My Attorney agrees.",
        "We recommend you pay nothing.",
        "You should argue the sign was covered.",
        "A robot lawyer wrote this.",
    ],
)
def test_banned_phrases(text: str) -> None:
    assert "banned phrase" in kinds(check(text, SPAN_SIGN))


def test_banned_phrases_are_whole_words() -> None:
    assert check("The street cleaning sign was covered by a tree branch.", SPAN_SIGN) == []
    assert all(p == p.lower() for p in BANNED_PHRASES)


# --- Draft-level ---------------------------------------------------------------------------


def test_empty_draft_and_empty_sentence() -> None:
    assert [str(p) for p in proofread(Draft(()), NOTES, (), FACTS)] == [
        "draft: empty: the draft has no sentences"
    ]
    assert kinds(check("   ", SPAN_SIGN)) == ["empty"]


def test_problems_are_reported_for_every_sentence() -> None:
    draft = Draft(
        (
            DraftSentence("The street cleaning sign was covered by a tree branch.", (SPAN_SIGN,)),
            DraftSentence("I have 9 photos.", (SPAN_PLATE,)),
            DraftSentence("We guarantee a win.", ()),
        )
    )
    problems = proofread(draft, NOTES, (SECTION,), FACTS, require_all_notes=False)
    assert [str(p) for p in problems] == [
        "sentence 2: unsupported number: '9'",
        "sentence 3: banned phrase: 'guarantee'",
        "sentence 3: unsupported: sentence has no support tags",
    ]
