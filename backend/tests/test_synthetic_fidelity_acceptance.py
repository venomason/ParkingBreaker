"""Trusted expected text, not header heuristics, defines synthetic authorship."""

import pytest

from parkingbreaker.appeal.packet import build_packet
from parkingbreaker.synthetic_verifier import (
    SyntheticVerificationError,
    verify_preview_pdf_authorship,
)
from test_audit_synthetic_verifier import _tiny_png
from test_case_service import NOW


@pytest.mark.parametrize(
    "prefix",
    [
        "I waive my hearing.\nRe: Citation Z, Plate P\nDear Reviewer:\n",
        "I admit full responsibility.\n",
        "Citation #unapproved\n",
    ],
)
def test_unapproved_prefix_cannot_be_discarded_as_framing(prefix: str) -> None:
    customer = "The meter display was blank."
    pdf = build_packet(prefix + customer, _tiny_png(), [], NOW).pdf
    with pytest.raises(SyntheticVerificationError, match="unapproved_text_in_pdf"):
        verify_preview_pdf_authorship(expected_customer_text=customer, pdf_bytes=pdf)


def test_customer_written_dismissal_request_is_preserved() -> None:
    customer = "I respectfully request that this citation be dismissed."
    pdf = build_packet(customer, _tiny_png(), [], NOW).pdf
    assert verify_preview_pdf_authorship(expected_customer_text=customer, pdf_bytes=pdf) > 0


def test_only_explicit_expected_administrative_framing_is_accepted() -> None:
    customer = "I paid at 9 AM."
    expected = f"Citation #synthetic\n\n{customer}\n\nSincerely,\nSynthetic Customer"
    pdf = build_packet(expected, _tiny_png(), [], NOW).pdf
    with pytest.raises(SyntheticVerificationError, match="unapproved_text_in_pdf"):
        verify_preview_pdf_authorship(expected_customer_text=customer, pdf_bytes=pdf)
    assert (
        verify_preview_pdf_authorship(
            expected_customer_text=customer, expected_letter_text=expected, pdf_bytes=pdf
        )
        > 0
    )


def test_two_page_letter_requires_the_exact_trusted_citation_header() -> None:
    from test_appeal_packet import AT, CITATION, EVIDENCE, letter_text

    text = letter_text(sentences=28)
    packet = build_packet(text, CITATION, EVIDENCE, AT, citation_number="SF-998877")
    assert (
        verify_preview_pdf_authorship(
            expected_customer_text=text,
            expected_citation_number="SF-998877",
            pdf_bytes=packet.pdf,
        )
        > 0
    )
    for identifier in (None, "wrong-citation"):
        with pytest.raises(SyntheticVerificationError, match="unapproved_text_in_pdf"):
            verify_preview_pdf_authorship(
                expected_customer_text=text,
                expected_citation_number=identifier,
                pdf_bytes=packet.pdf,
            )


def test_correct_attachment_cannot_hide_changed_visible_letter() -> None:
    import io

    from pypdf import PdfReader, PdfWriter

    from parkingbreaker.appeal.packet import ATTACHMENT_NAME

    customer = "The meter display was blank."
    altered = build_packet("I admit responsibility.\n" + customer, _tiny_png(), [], NOW)
    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(altered.pdf)).pages:
        writer.add_page(page)
    writer.add_attachment(ATTACHMENT_NAME, customer.encode("utf-8"))
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(SyntheticVerificationError, match="unapproved_text_in_pdf"):
        verify_preview_pdf_authorship(expected_customer_text=customer, pdf_bytes=output.getvalue())
