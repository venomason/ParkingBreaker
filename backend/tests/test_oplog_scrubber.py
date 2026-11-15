from __future__ import annotations

import pytest

from parkingbreaker.oplog.scrubber import ScrubResult, entry_or_tagline, scrub

TAG = "outcome=mailed stop=none city=sf mode=standard week=2026-W41"
SENSITIVE = ("Jordan Alvarez", "7ABC123", "SF-998877", "1450 Valencia Street")


def test_plain_factual_entry_is_clean() -> None:
    text = (
        f"{TAG}. Research fetched 4 pages and rejected 1 quote. The customer was asked 2 "
        "questions about signage. USPS took 3 days; Lob lag was 1 day. Cost 1250 cents. "
        "One redraft. Margin of 6 days in week 2026-W41."
    )
    assert scrub(text, SENSITIVE) == ScrubResult(clean=True, reasons=())


@pytest.mark.parametrize(
    "text",
    [
        "Customer jordan   ALVAREZ asked a question.",
        "Customer Jordan\nAlvarez asked a question.",
        "Plate 7abc 123 was readable.",
        "Ticket sf 998877 was contested.",
        "Car was at 1450 valencia street.",
    ],
)
def test_case_sensitive_strings_any_case_or_spacing(text: str) -> None:
    result = scrub(text, SENSITIVE)
    assert not result.clean
    assert "case_sensitive_string" in result.reasons


def test_sensitive_string_needs_word_boundaries() -> None:
    assert scrub("Annual pattern noted.", ("Ann",)).clean
    assert not scrub("Ann wrote back.", ("Ann",)).clean


def test_empty_sensitive_strings_are_ignored() -> None:
    assert scrub("Mailed in CA as usual.", ("", "  ")).clean


@pytest.mark.parametrize("identifier", ["A", "CA", "1", "12"])
def test_explicit_short_sensitive_values_are_scrubbed(identifier: str) -> None:
    assert not scrub(f"Customer identifier {identifier} was processed.", (identifier,)).clean


def test_short_mixed_alphanumeric_sensitive_identifier_is_scrubbed() -> None:
    result = scrub("Customer reference A1 was processed.", ("A1",))
    assert not result.clean
    assert "case_sensitive_string" in result.reasons


def test_reasons_never_contain_the_matched_text() -> None:
    result = scrub("Jordan Alvarez", SENSITIVE)
    assert all("jordan" not in r.lower() for r in result.reasons)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Reach them at jo.doe+x@example.co.uk soon.", "email"),
        ("Call 415-555-1234 later.", "phone"),
        ("Call (415) 555-1234 later.", "phone"),
        ("Call +1 415.555.1234 later.", "phone"),
        ("Call 555-1234 later.", "phone"),
        ("Citation 12345678 noted.", "digit_run"),
        ("VIN tail 123456.", "digit_run"),
        ("Parked at 221 Baker St. overnight.", "street_address"),
        ("Parked at 1600 Pennsylvania Avenue overnight.", "street_address"),
        ("Parked at 12 West 3rd Ave overnight.", "street_address"),
        ("Issued 2026-10-11.", "exact_date"),
        ("Issued 2026/10/11.", "exact_date"),
        ("Issued 10/11/2026.", "exact_date"),
        ("Issued 10/11/26.", "exact_date"),
        ("Issued October 11, 2026.", "exact_date"),
        ("Issued Oct. 11 2026.", "exact_date"),
        ("Issued 11 October 2026.", "exact_date"),
        ("Issued October 11 in the morning.", "exact_date"),
        ("License plate 8XYZ999 was blurred.", "plate"),
        ("The plate number is AB12CD.", "plate"),
        ("AB12CD was on the plate.", "plate"),
    ],
)
def test_generic_patterns(text: str, reason: str) -> None:
    result = scrub(text, ())
    assert not result.clean
    assert reason in result.reasons


def test_standalone_plate_like_token_is_scrubbed() -> None:
    result = scrub("AB12CD was processed.", ())
    assert not result.clean
    assert "plate" in result.reasons


@pytest.mark.parametrize(
    "text",
    [
        "Week 2026-W41 saw 3 returns.",
        "The plate was unreadable in week 2026-W41.",
        "Plate photo requested 2 times.",
        "Research fetched 12 pages; 3 quotes rejected; 2 conflicts of type deadline.",
        "Cost 98000 cents? No: cost was 9800 cents over 5 days.",
        "Took 14 days and 3 redrafts on the way to mailing.",
        "2 questions asked along the way.",
        "Margin -2 days; lag 10 days; 4 of 5 pages ok.",
        "It may 3 times have bounced.",
        "Ratio 3/4 of pages.",
        "Year 2026 volume rose.",
    ],
)
def test_false_positives_are_avoided(text: str) -> None:
    assert scrub(text, SENSITIVE).clean, scrub(text, SENSITIVE).reasons


def test_multiple_reasons_are_reported_in_order() -> None:
    result = scrub("Mail jo@x.com on 2026-10-11 about 123456789.", ())
    assert result.reasons == ("email", "digit_run", "exact_date")


def test_entry_or_tagline() -> None:
    clean = f"{TAG}. USPS took 3 days."
    assert entry_or_tagline(clean, TAG, SENSITIVE) == clean
    dirty = f"{TAG}. Jordan Alvarez called."
    assert entry_or_tagline(dirty, TAG, SENSITIVE) == TAG
