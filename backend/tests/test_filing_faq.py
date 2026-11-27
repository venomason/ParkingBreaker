"""Quote attribution on the archived West Covina Police Department FAQ."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from parkingbreaker.research.findings import Finding, FindingStatus, apply_quote_guard
from parkingbreaker.research.procedure import procedure_sections, quote_procedure_stage
from parkingbreaker.research.questions import FactKey
from parkingbreaker.research.sources import AuthorityClass, SourceRecord, html_to_text

SHA = "4ed8fdadd2f54ad12cb6602bae405ff7e16ac4656a40ffe14064c67654f9f06e"
URL = "https://www.westcovina.gov/m/faq?cat=19"
STAGE = "first-level review"
BEGIN = "If you feel you have been issued a parking citation in error,"
END = "why you feel the citation was issued in error."
LATER = "Requests received outside the 15-day period will not be honored."


def capture() -> SourceRecord:
    raw = (
        Path(__file__).resolve().parents[2] / "build-kit/evidence/bench-pages" / SHA
    ).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SHA
    return SourceRecord(
        URL,
        URL,
        datetime(2026, 11, 27, tzinfo=UTC),
        200,
        SHA,
        "text/html",
        html_to_text(raw.decode()),
        AuthorityClass.OFFICIAL_PROCEDURE,
    )


def test_official_first_stage_destination_and_excluded_clocks() -> None:
    page = capture()
    text = page.text
    quote = text[text.index(BEGIN) : text.index(END) + len(END)]
    address = Finding(
        FactKey.MAILING_ADDRESS,
        {
            "recipient": "City of West Covina",
            "attention": "Adjudication Services",
            "line1": "P.O. Box 2081",
            "line2": None,
            "city": "Tustin",
            "state": "CA",
            "zip_code": "92781-2081",
        },
        FindingStatus.CONFIRMED,
        quote,
        (SHA,),
        stage=STAGE,
        jurisdiction="CA:westcovina.gov",
    )
    later = Finding(
        FactKey.DEADLINE_DAYS,
        15,
        FindingStatus.CONFIRMED,
        text[
            text.index("There will be information in the letter") : text.index(LATER) + len(LATER)
        ],
        (SHA,),
        stage=STAGE,
    )
    clock = Finding(
        FactKey.DEADLINE_CLOCK_START,
        "ISSUE_DATE",
        FindingStatus.CONFIRMED,
        "Reviews must be requested in writing within 21 days of the violation,",
        (SHA,),
        stage=STAGE,
    )
    result = apply_quote_guard([address, later, clock], [page])
    assert [row.status for row in result] == [
        FindingStatus.CONFIRMED,
        FindingStatus.UNKNOWN,
        FindingStatus.UNKNOWN,
    ]
    assert quote_procedure_stage(text, LATER) == "administrative hearing"
    assert (
        quote_procedure_stage(text, "You can also mail your parking citation payments to:") is None
    )
    assert quote_procedure_stage(text, "P.O. Box 2081, Tustin, CA 92781-2081") is None
    assert "".join(section.text for section in procedure_sections(text)) == text


def test_ambiguous_answer_never_supplies_an_initial_stage() -> None:
    text = capture().text
    assert (
        quote_procedure_stage(
            text.replace(
                "How can I dispute a parking citation?", "How do I contest my parking ticket?"
            ),
            END,
        )
        == STAGE
    )
    for amended in (
        text.replace(BEGIN, "Following an administrative hearing,"),
        text.replace(
            "How can I dispute a parking citation?", "How can I dispute a hearing decision?"
        ),
        text.replace(END, END + " Request a second-level hearing."),
    ):
        assert quote_procedure_stage(amended, END) != STAGE
