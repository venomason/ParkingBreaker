from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from parkingbreaker.json_types import JsonValue
from parkingbreaker.research.findings import Finding, FindingStatus, apply_quote_guard
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.support import (
    address_purpose_problem,
    numbers_in,
    value_supported_by_quote,
)
from samples import source

K = FactKey

_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_TEENS = [
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def spell(n: int, sep: str = "-", hundred_and: bool = False) -> str:
    """Independent English spelling of 1..999 for the tests."""
    parts = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} hundred")
        n %= 100
        if n and hundred_and:
            parts.append("and")
    if 10 <= n < 20:
        parts.append(_TEENS[n - 10])
    elif n >= 20:
        parts.append(_TENS[n // 10] + (sep + _ONES[n % 10] if n % 10 else ""))
    elif n:
        parts.append(_ONES[n])
    return " ".join(parts)


# --- The headline case: a real quote cannot carry a different number --------------------

QUOTE_21 = "Contests must be received within 21 days of the citation issue date."


def test_45_is_not_supported_by_a_21_day_quote() -> None:
    assert value_supported_by_quote(K.DEADLINE_DAYS, 21, QUOTE_21)
    assert not value_supported_by_quote(K.DEADLINE_DAYS, 45, QUOTE_21)


def test_guard_downgrades_a_real_quote_with_the_wrong_value() -> None:
    page = source(QUOTE_21)
    claim = Finding(K.DEADLINE_DAYS, 45, FindingStatus.CONFIRMED, QUOTE_21, (page.sha256,))
    (out,) = apply_quote_guard([claim], [page])
    assert out.status is FindingStatus.UNKNOWN
    assert out.notes == "quote guard: value not in quote"
    honest = Finding(K.DEADLINE_DAYS, 21, FindingStatus.CONFIRMED, QUOTE_21, (page.sha256,))
    assert apply_quote_guard([honest], [page])[0].status is FindingStatus.CONFIRMED


@pytest.mark.parametrize(
    "quote",
    [
        "within twenty-one days of issue",
        "within twenty one days of issue",
        "within Twenty-One (21) days of issue",
        "within 21 days of issue",
        "a 21-day window from issue",
        "no later than the 21st day after issue",
    ],
)
def test_word_and_digit_forms_of_21(quote: str) -> None:
    assert value_supported_by_quote(K.DEADLINE_DAYS, 21, quote)


@pytest.mark.parametrize(
    "quote",
    [
        "within twenty days and one week",  # 20 and 1, never 21
        "within 121 days of issue",
        "within 210 days of issue",
        "within 2 1 days of issue",
        "within twenty-two days",
        "within twenty days",
    ],
)
def test_near_misses_for_21(quote: str) -> None:
    assert not value_supported_by_quote(K.DEADLINE_DAYS, 21, quote)


def test_twenty_alone_does_not_hide_inside_twenty_one() -> None:
    assert not value_supported_by_quote(K.DEADLINE_DAYS, 20, "within twenty-one days")
    assert not value_supported_by_quote(K.DEADLINE_DAYS, 1, "within twenty-one days")


def test_deadline_number_is_bound_to_its_purpose_and_period() -> None:
    quote = "Contest within 21 days of the notice. Late fees apply after 45 days."
    assert value_supported_by_quote(K.DEADLINE_DAYS, 21, quote)
    assert not value_supported_by_quote(K.DEADLINE_DAYS, 45, quote)
    assert not value_supported_by_quote(
        K.DEADLINE_DAYS,
        45,
        "Late fees begin within 45 days; contests must be filed within 21 days.",
    )


def test_business_day_period_is_not_silently_calculated_as_calendar_days() -> None:
    assert value_supported_by_quote(K.DEADLINE_DAYS, 21, "Contest within 21 calendar days.")
    assert not value_supported_by_quote(K.DEADLINE_DAYS, 21, "Contest within 21 business days.")
    assert not value_supported_by_quote(K.DEADLINE_DAYS, 21, "Contest within 21 working days.")


def test_timed_or_zoned_deadline_remains_unverified_by_date_only_rules() -> None:
    assert not value_supported_by_quote(
        K.DEADLINE_DAYS, 21, "Contest within 21 days, by 5:00 p.m. Pacific time."
    )
    assert not value_supported_by_quote(
        K.DEADLINE_ABSOLUTE,
        {"applicable": True, "date": "2026-11-15"},
        "Your contest must be received by November 15, 2026 at 11:59 p.m. PST.",
    )


def test_unrelated_service_date_and_notice_mention_do_not_change_clock_start() -> None:
    quote = (
        "Appeal within 21 days of the citation issue date. "
        "Keep this notice for your records; date of service is shown above."
    )
    assert value_supported_by_quote(K.DEADLINE_CLOCK_START, "ISSUE_DATE", quote)
    assert not value_supported_by_quote(K.DEADLINE_CLOCK_START, "NOTICE_DATE", quote)
    assert not value_supported_by_quote(K.DEADLINE_CLOCK_START, "SERVICE_DATE", quote)


@given(st.integers(1, 365), st.sampled_from(["-", " "]), st.booleans())
def test_every_deadline_in_bounds_is_read_from_words_and_digits(
    n: int, sep: str, hundred_and: bool
) -> None:
    words = spell(n, sep, hundred_and)
    assert value_supported_by_quote(K.DEADLINE_DAYS, n, f"You have {words} days to appeal.")
    assert value_supported_by_quote(K.DEADLINE_DAYS, n, f"You have {n} days to appeal.")
    assert not value_supported_by_quote(K.DEADLINE_DAYS, n + 1, f"You have {words} days.")


def test_numbers_in_reads_runs_and_digits() -> None:
    assert list(numbers_in("one hundred and five, then 3,000 and $90.50")) == [105, 3000, 90.5]
    assert list(numbers_in("three hundred sixty-five")) == [365]
    assert list(numbers_in("rock and roll")) == []


def test_bool_and_strings_are_not_numbers() -> None:
    assert not value_supported_by_quote(K.DEADLINE_DAYS, True, "within 1 day")
    assert not value_supported_by_quote(K.DEADLINE_DAYS, "21", QUOTE_21)


# --- Deadline rule -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "quote", "ok"),
    [
        ("RECEIPT", "Contests must be received by the deadline.", True),
        ("RECEIPT", "Appeals must be delivered to our office by the due date.", True),
        ("RECEIPT", "Appeals must be postmarked by the deadline.", False),
        ("RECEIPT", "Appeals postmarked and received by the deadline are accepted.", False),
        (
            "RECEIPT",
            "Contests must be received by the deadline; postmarks are not accepted.",
            True,
        ),
        ("RECEIPT", "Appeals must be submitted by the deadline.", False),
        ("POSTMARK", "Appeals must be postmarked by the deadline.", True),
        ("POSTMARK", "Postmarks dated by the deadline are accepted.", True),
        ("POSTMARK", "Postmarks are not accepted after the deadline.", False),
        ("POSTMARK", "Appeals must not be postmarked after the deadline.", False),
        ("POSTMARK", "Contests must be received by the deadline.", False),
        ("POSTMARKED", "Appeals must be postmarked by the deadline.", False),
    ],
)
def test_deadline_rule(value: str, quote: str, ok: bool) -> None:
    assert value_supported_by_quote(K.DEADLINE_RULE, value, quote) is ok


# --- Clock start ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "quote", "ok"),
    [
        ("ISSUE_DATE", "within 21 days of the citation issue date", True),
        ("ISSUE_DATE", "within 21 days after the citation was issued", True),
        ("ISSUE_DATE", "within 21 days of the citation date", True),
        ("ISSUE_DATE", "within 21 days of the notice", False),
        ("NOTICE_DATE", "within 21 days of the first courtesy notice", True),
        ("NOTICE_DATE", "within 21 days of the citation issue date", False),
        ("SOMETHING_ELSE", "within 21 days of the notice", False),
    ],
)
def test_clock_start(value: str, quote: str, ok: bool) -> None:
    assert value_supported_by_quote(K.DEADLINE_CLOCK_START, value, quote) is ok


# --- Address, code section, fine -----------------------------------------------------------

ADDRESS: dict[str, JsonValue] = {
    "recipient": "SFMTA Customer Service Center",
    "attention": "Citation Review",
    "line1": "11 South Van Ness Avenue",
    "line2": None,
    "city": "San Francisco",
    "state": "CA",
    "zip_code": "94103",
}
ADDRESS_QUOTE = (
    "SFMTA Customer Service Center ATTN: Citation Review, "
    "11 South Van Ness Avenue, San Francisco, CA 94103"
)


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        (ADDRESS, True),
        ({**ADDRESS, "zip_code": "94103-1234"}, False),
        ({**ADDRESS, "zip_code": "94102"}, False),
        ({**ADDRESS, "line1": "1 South Van Ness Avenue"}, False),  # 1 only inside 11
        ({**ADDRESS, "line1": "111 South Van Ness Avenue"}, False),
        ({"zip_code": "94103"}, False),
        ("11 South Van Ness Avenue 94103", False),
    ],
)
def test_mailing_address(value: JsonValue, ok: bool) -> None:
    assert value_supported_by_quote(K.MAILING_ADDRESS, value, ADDRESS_QUOTE) is ok


def test_address_without_a_street_number_needs_the_whole_line() -> None:
    po: dict[str, JsonValue] = {
        **ADDRESS,
        "recipient": "Citation Review Unit",
        "attention": None,
        "line1": "Citation Review Unit",
        "zip_code": "94120",
    }
    assert value_supported_by_quote(
        K.MAILING_ADDRESS, po, "Mail to Citation Review Unit, San Francisco, CA 94120."
    )
    assert not value_supported_by_quote(K.MAILING_ADDRESS, po, "Mail to San Francisco, 94120.")


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("SEC. 7.2.22", True),
        ("SEC.  7.2.22", True),  # whitespace only
        ("Sec. 7.2.22", False),  # case is not normalised
        ("SEC. 7.2.23", False),
        ("", False),
        (722, False),
    ],
)
def test_governing_code_section(value: JsonValue, ok: bool) -> None:
    quote = "SEC. 7.2.22. STREET CLEANING. It shall be unlawful to park"
    assert value_supported_by_quote(K.GOVERNING_CODE_SECTION, value, quote) is ok


@pytest.mark.parametrize(
    ("value", "ok"), [(90, True), (90.0, True), (90.5, False), (9, False), (False, False)]
)
def test_fine_amount(value: JsonValue, ok: bool) -> None:
    assert value_supported_by_quote(K.FINE_AMOUNT, value, "The fine is $90.") is ok


# --- Lists: every item verbatim; empty lists need a negation ------------------------------


@pytest.mark.parametrize(
    ("value", "quote", "ok"),
    [
        (["citation number"], "Include your citation number and evidence.", True),
        (["citation number", "evidence"], "Include your citation number and evidence.", True),
        (["citation number", "photo ID"], "Include your citation number and evidence.", False),
        (["Citation number"], "Include your citation number.", False),
        ([], "Include your citation number.", False),
        ("citation number", "Include your citation number.", False),
        ([1], "Include 1 copy.", False),
    ],
)
def test_list_keys(value: JsonValue, quote: str, ok: bool) -> None:
    for key in (K.REQUIRED_ITEMS, K.REQUIRED_DECLARATIONS, K.CODE_EXCEPTIONS):
        assert value_supported_by_quote(key, value, quote) is ok


@pytest.mark.parametrize(
    ("key", "quote"),
    [
        (K.REQUIRED_ITEMS, "No items are required."),
        (K.REQUIRED_DECLARATIONS, "A signed declaration is not required."),
        (K.REQUIRED_DECLARATIONS, "No declaration is needed."),
        (K.CODE_EXCEPTIONS, "There are no exceptions."),
    ],
)
def test_empty_list_requires_absence_of_that_specific_requirement(key: FactKey, quote: str) -> None:
    assert value_supported_by_quote(key, [], quote)
    for other in {K.REQUIRED_ITEMS, K.REQUIRED_DECLARATIONS, K.CODE_EXCEPTIONS} - {key}:
        assert not value_supported_by_quote(other, [], quote)


@pytest.mark.parametrize(
    "quote",
    [
        "Do not include original documents. Include your citation number.",
        "No declaration is needed. A sworn statement is required.",
        "A photo is not required.",
        "No exceptions apply to payments. Include your citation number.",
    ],
)
def test_unrelated_negation_cannot_prove_an_empty_list(quote: str) -> None:
    for key in (K.REQUIRED_ITEMS, K.REQUIRED_DECLARATIONS, K.CODE_EXCEPTIONS):
        assert not value_supported_by_quote(key, [], quote)


@pytest.mark.parametrize(
    "quote",
    [
        "You may contest a parking citation by mail.",
        "Contests sent by mail must be received by the deadline.",
        "We accept mailed appeals.",
        "The agency accepts contests by mail.",
    ],
)
def test_explicit_mail_contest_route_is_supported(quote: str) -> None:
    assert value_supported_by_quote(K.MAIL_IN_ACCEPTED, True, quote)


@pytest.mark.parametrize(
    ("value", "quote"),
    [
        (True, "Mail your payment to 11 South Van Ness Avenue."),
        (True, "You may not contest a citation by mail."),
        (True, "We do not accept mailed appeals."),
        (True, "We never accept mailed appeals."),
        (True, "Customers ask whether they can appeal by mail."),
        (True, "In the past we accepted mailed appeals."),
        (True, "Contests submitted online are accepted; mail payments separately."),
        (True, "Mailing address: 11 South Van Ness Avenue."),
        (False, "We accept mailed appeals."),
        (1, "We accept mailed appeals."),
        ("true", "We accept mailed appeals."),
    ],
)
def test_mail_mentions_or_negative_claims_do_not_prove_permission(
    value: JsonValue, quote: str
) -> None:
    assert not value_supported_by_quote(K.MAIL_IN_ACCEPTED, value, quote)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("line1", "11 Invented Street"),
        ("line1", "11 North Van Ness Avenue"),
        ("line2", "Suite 100"),
        ("city", "Oakland"),
        ("state", "NV"),
        ("recipient", "Invented Review Office"),
        ("attention", "Payments Department"),
    ],
)
def test_address_quote_must_support_every_mailed_component(field: str, value: str) -> None:
    assert not value_supported_by_quote(K.MAILING_ADDRESS, {**ADDRESS, field: value}, ADDRESS_QUOTE)


def test_address_whitespace_case_and_punctuation_can_differ() -> None:
    quoted = (
        "SFMTA CUSTOMER SERVICE CENTER, ATTN: CITATION REVIEW\n"
        "11 South Van Ness Avenue\nSan Francisco, CA 94103"
    )
    assert value_supported_by_quote(K.MAILING_ADDRESS, ADDRESS, quoted)


def test_address_components_cannot_be_spliced_from_separate_destinations() -> None:
    quote = (
        "SFMTA Customer Service Center ATTN: Citation Review, "
        "11 South Van Ness Avenue, San Francisco, CA 94103. "
        "Payments Department: 22 Main Street, Oakland, CA 94612."
    )
    spliced = {**ADDRESS, "city": "Oakland", "zip_code": "94612"}
    assert not value_supported_by_quote(K.MAILING_ADDRESS, spliced, quote)


@pytest.mark.parametrize("field", ["recipient", "line1", "city", "state", "zip_code"])
def test_address_support_requires_a_complete_address(field: str) -> None:
    assert not value_supported_by_quote(
        K.MAILING_ADDRESS, {k: v for k, v in ADDRESS.items() if k != field}, ADDRESS_QUOTE
    )


def test_zip_extension_is_supported_only_when_present_in_quote() -> None:
    extended = {**ADDRESS, "zip_code": "94103-1234"}
    assert not value_supported_by_quote(K.MAILING_ADDRESS, extended, ADDRESS_QUOTE)
    assert value_supported_by_quote(K.MAILING_ADDRESS, extended, ADDRESS_QUOTE + "-1234")


# --- Destination purpose: real official wording (saved 2026-10-12, docs v6 note 24) ----

LA_BOX: dict[str, JsonValue] = {
    "recipient": "Parking Violations Bureau",
    "line1": "P.O. Box 30247",
    "city": "Los Angeles",
    "state": "CA",
    "zip_code": "90030",
}
LA_WAIVER_BOX: dict[str, JsonValue] = {**LA_BOX, "line1": "P.O. Box 30420"}
LA_INITIAL_REVIEW = (
    "4. By Mail (Download and submit an Initial Review Form) Parking Violations Bureau "
    "P.O. Box 30247 Los Angeles, CA 90030"
)
LA_WAIVER = (
    "A request for a Pre-Payment Waiver can be made by telephone at (866) 561-9742 or mail "
    "a completed waiver request form to: Parking Violations Bureau P.O. Box 30420 "
    "Los Angeles, CA 90030"
)


def test_la_initial_review_box_is_a_contest_destination() -> None:
    assert value_supported_by_quote(K.MAILING_ADDRESS, LA_BOX, LA_INITIAL_REVIEW)


def test_la_prepayment_waiver_box_is_not_a_contest_destination() -> None:
    # The quote guard alone accepted this as MAILING_ADDRESS (note 24 counterexample).
    assert not value_supported_by_quote(K.MAILING_ADDRESS, LA_WAIVER_BOX, LA_WAIVER)


def test_payment_address_elsewhere_does_not_invalidate_contest_destination() -> None:
    quote = (
        "Mail your contest to Parking Violations Bureau P.O. Box 30247 Los Angeles, CA 90030. "
        "For pre-payment waiver requests, mail the form to Parking Violations Bureau "
        "P.O. Box 30420 Los Angeles, CA 90030."
    )

    assert value_supported_by_quote(K.MAILING_ADDRESS, LA_BOX, quote)
    assert not value_supported_by_quote(K.MAILING_ADDRESS, LA_WAIVER_BOX, quote)


@pytest.mark.parametrize(
    "quote",
    [
        # Address bytes alone, no purpose: unresolved, goes down the ladder.
        "Parking Violations Bureau P.O. Box 30247 Los Angeles, CA 90030",
        "Mail your payment to Parking Violations Bureau P.O. Box 30247 Los Angeles, CA 90030",
        "To contest or pay, write to Parking Violations Bureau P.O. Box 30247 "
        "Los Angeles, CA 90030",
        "Refunds and correspondence: Parking Violations Bureau P.O. Box 30247 "
        "Los Angeles, CA 90030",
    ],
)
def test_address_without_a_sole_contest_purpose_is_unsupported(quote: str) -> None:
    assert not value_supported_by_quote(K.MAILING_ADDRESS, LA_BOX, quote)


@pytest.mark.parametrize(
    ("value", "quote"),
    [
        (
            {
                "recipient": "DMV Adjudication Services",
                "attention": "Mail Adjudication",
                "line1": "PO Box 37135",
                "city": "Washington",
                "state": "DC",
                "zip_code": "20013",
            },
            "Mail your statement or the adjudication form to: DMV Adjudication Services "
            "ATTN: Mail Adjudication PO Box 37135 Washington, DC 20013",
        ),
        (
            {
                "recipient": "City of Boston",
                "line1": "P.O. Box 55800",
                "city": "Boston",
                "state": "MA",
                "zip_code": "02205",
            },
            "Write a letter to appeal your ticket. Mail your letter to the City. You can mail "
            "your letter to: City of Boston P.O. Box 55800 Boston, MA 02205",
        ),
    ],
    ids=["dc", "boston"],
)
def test_other_real_contest_destinations_are_supported(value: JsonValue, quote: str) -> None:
    assert value_supported_by_quote(K.MAILING_ADDRESS, value, quote)


@pytest.mark.parametrize(
    "quote",
    [
        "You may also submit your protest by mail.",  # SFMTA, rejected before (note 24)
        "To submit your Administrative Review by Mail, send your written request and "
        "supporting documentation to:",  # San Diego
        "You can dispute a ticket by mail.",
        "We accept protests by mail.",
    ],
)
def test_real_mail_contest_wording_is_supported(quote: str) -> None:
    assert value_supported_by_quote(K.MAIL_IN_ACCEPTED, True, quote)


@pytest.mark.parametrize(
    "quote",
    [
        "You may not submit your protest by mail.",
        "You can mail your letter to:",
        "You may also submit your payment by mail.",
        "To submit your payment by mail, send it to:",
    ],
)
def test_mail_wording_without_contest_permission_is_unsupported(quote: str) -> None:
    assert not value_supported_by_quote(K.MAIL_IN_ACCEPTED, True, quote)


def test_purpose_failure_is_named_in_the_guard_note() -> None:
    page = source(LA_WAIVER)
    claim = Finding(
        K.MAILING_ADDRESS, LA_WAIVER_BOX, FindingStatus.CONFIRMED, LA_WAIVER, (page.sha256,)
    )
    (out,) = apply_quote_guard([claim], [page])
    assert out.status is FindingStatus.UNKNOWN
    assert out.notes == "quote guard: address quote names another purpose (pre-payment)"


def test_purpose_problem_is_silent_when_the_address_itself_is_absent() -> None:
    assert address_purpose_problem(LA_BOX, LA_WAIVER) is None
    assert address_purpose_problem(LA_WAIVER_BOX, LA_WAIVER) is not None
    assert address_purpose_problem(LA_BOX, LA_INITIAL_REVIEW) is None
