from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime

import pytest

from parkingbreaker.appeal.adoption import (
    FINAL_SCREEN_TEXT,
    AdoptionError,
    adopt,
    is_adopted,
    text_sha256,
)
from parkingbreaker.appeal.drafting import Draft, DraftSentence
from parkingbreaker.appeal.letter import (
    DEFAULT_SALUTATION,
    LetterError,
    LetterInput,
    Sender,
    body_paragraphs,
    printable,
    render_letter,
)
from parkingbreaker.research.fakes import fixed_clock
from samples import CONTEST_ADDRESS, SAN_FRANCISCO

DRAFT = Draft(
    (
        DraftSentence("I parked on Van Ness.", ("note:0-5",), 0),
        DraftSentence("The sign was covered.", ("note:0-5",), 0),
        DraftSentence("I have enclosed  two photos.", ("note:0-5",), 1),
    )
)
INPUT = LetterInput(
    sender=Sender("Jane Doe", ("1 Main St", "Apt 2", "Oakland, CA 94601")),
    letter_date=date(2026, 10, 11),
    agency=SAN_FRANCISCO,
    mailing_address=CONTEST_ADDRESS,
    citation_number="SF-998877",
    plate="7ABC123",
    draft=DRAFT,
)
NOT_CA = dataclasses.replace(SAN_FRANCISCO, state="NV", basis_key="NV")


def test_letter_frame_in_order() -> None:
    letter = render_letter(INPUT)
    assert letter.declarations_part is None
    assert letter.text == letter.letter_part
    assert letter.text == "\n\n".join(
        [
            "Jane Doe\n1 Main St\nApt 2\nOakland, CA 94601",
            "October 11, 2026",
            "San Francisco Municipal Transportation Agency\nATTN: Citation Review\n"
            "11 South Van Ness Avenue\nSan Francisco, CA 94103",
            "Re: Citation SF-998877, Plate 7ABC123",
            DEFAULT_SALUTATION,
            "I parked on Van Ness. The sign was covered.",
            "I have enclosed two photos.",
            "Sincerely,\n\n\nJane Doe",
        ]
    )


def test_closing_sentence_is_one_explicit_input() -> None:
    custom = dataclasses.replace(INPUT, closing_sentence="Please reduce the fine.")
    text = render_letter(custom).text
    assert "Please reduce the fine." in text
    assert "respectfully request that this citation be dismissed" not in text.casefold()
    with pytest.raises(LetterError, match="closing sentence"):
        render_letter(dataclasses.replace(INPUT, closing_sentence="  "))


def test_paragraph_breaks_preserve_sentence_order_even_with_repeated_labels() -> None:
    draft = Draft(
        (
            DraftSentence("B1.", (), 2),
            DraftSentence("A1.", (), 0),
            DraftSentence("B2.", (), 2),
        )
    )
    assert body_paragraphs(draft) == ["B1.", "A1.", "B2."]


@pytest.mark.parametrize("state", ["CA", "NV"])
@pytest.mark.parametrize("affirmed", [True, False])
def test_no_default_declaration_even_when_hardship_affirmed(state: str, affirmed: bool) -> None:
    agency = dataclasses.replace(SAN_FRANCISCO, state=state, basis_key=state)
    letter = render_letter(dataclasses.replace(INPUT, agency=agency, hardship_affirmed=affirmed))
    assert letter.declarations_part is None
    assert "declaration" not in letter.text.lower()
    assert "penalty of perjury" not in letter.text.lower()
    assert "sworn" not in letter.text.lower()
    assert "\f" not in letter.text


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"sender": Sender(" ", ("1 Main St",))}, "sender name"),
        ({"sender": Sender("Jane", ("  ",))}, "sender address"),
        ({"citation_number": ""}, "citation number"),
        ({"plate": ""}, "plate"),
        ({"draft": Draft(())}, "no sentences"),
    ],
)
def test_required_fields(change: dict[str, object], message: str) -> None:
    with pytest.raises(LetterError, match=message):
        render_letter(dataclasses.replace(INPUT, **change))  # type: ignore[arg-type]


def test_typographic_characters_are_refused_not_substituted() -> None:
    # The customer's characters are never silently rewritten to ASCII.
    with pytest.raises(LetterError, match="0x2019"):
        printable("It\u2019s fine")
    with pytest.raises(LetterError, match="0x2014"):
        printable("one \u2014 two")
    # Latin-1 characters the PDF font supports still print unchanged.
    draft = Draft((DraftSentence("I parked on Caf\u00e9 St.", (), 0),))
    assert (
        "I parked on Caf\u00e9 St." in render_letter(dataclasses.replace(INPUT, draft=draft)).text
    )


def test_unprintable_characters_are_refused() -> None:
    with pytest.raises(LetterError, match="cannot print"):
        printable("Nguyễn")
    with pytest.raises(LetterError, match="cannot print"):
        render_letter(dataclasses.replace(INPUT, sender=Sender("李雷", ("1 Main St",))))


# --- Adoption ------------------------------------------------------------------------------

AT = datetime(2026, 10, 11, 18, 30, tzinfo=UTC)


def test_adopt_records_exact_text_hash_and_time() -> None:
    text = render_letter(INPUT).text
    adoption = adopt(text, "cust_1", fixed_clock(AT))
    assert adoption.adopted_at == AT
    assert adoption.sha256 == text_sha256(text)
    assert adoption.screen_text == FINAL_SCREEN_TEXT
    assert is_adopted(adoption, text)


@pytest.mark.parametrize(
    "edit",
    [
        lambda t: t + " ",
        lambda t: t.replace("covered", "Covered"),
        lambda t: t.replace("\n\n", "\n", 1),
        lambda t: t.replace("SF-998877", "SF-998878"),
    ],
)
def test_any_edit_after_adoption_invalidates_it(edit: object) -> None:
    text = render_letter(INPUT).text
    adoption = adopt(text, "cust_1", fixed_clock(AT))
    assert callable(edit)
    assert not is_adopted(adoption, edit(text))


def test_adoption_needs_text_and_customer() -> None:
    with pytest.raises(AdoptionError, match="empty"):
        adopt("  ", "cust_1", fixed_clock(AT))
    with pytest.raises(AdoptionError, match="customer_id"):
        adopt("text", " ", fixed_clock(AT))


def test_final_screen_wording_is_fixed() -> None:
    assert FINAL_SCREEN_TEXT == (
        "Here is your typed draft based on your notes. Read it, edit any details, and "
        "approve if you adopt it as your own statement."
    )


def test_letter_uses_explicit_per_ticket_destination() -> None:
    fresh = dataclasses.replace(
        CONTEST_ADDRESS, line1="22 Main Street", city="Oakland", zip_code="94612"
    )
    letter = render_letter(dataclasses.replace(INPUT, mailing_address=fresh))
    assert "22 Main Street\nOakland, CA 94612" in letter.text
    assert "11 South Van Ness Avenue" not in letter.text


@pytest.mark.parametrize(
    "attention", ["Citation Review", "ATTN: Citation Review", "Attention: Review"]
)
def test_attention_line_is_never_doubled(attention: str) -> None:
    address = dataclasses.replace(CONTEST_ADDRESS, attention=attention)
    text = render_letter(dataclasses.replace(INPUT, mailing_address=address)).text
    assert "ATTN: ATTN" not in text
    assert "ATTN: Attention" not in text
    assert text.count(attention.split(": ")[-1]) == 1


@pytest.mark.parametrize(
    ("citation", "photos", "line"),
    [
        (True, 0, "Enclosures: Copy of citation"),
        (True, 1, "Enclosures: Copy of citation; 1 evidence photo"),
        (True, 2, "Enclosures: Copy of citation; 2 evidence photos"),
        (False, 0, None),
    ],
)
def test_enclosures_line_follows_the_signature(
    citation: bool, photos: int, line: str | None
) -> None:
    text = render_letter(
        dataclasses.replace(INPUT, encloses_citation=citation, evidence_photos=photos)
    ).text
    if line is None:
        assert "Enclosures" not in text
    else:
        assert text.endswith("\n\n" + line)
