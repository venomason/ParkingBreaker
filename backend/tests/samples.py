"""Shared test data: one instance of every event type, and research helpers."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.agencies import IssuerType, MailingAddress, ResolvedAgency
from parkingbreaker.research.sources import AuthorityClass, SourceRecord
from parkingbreaker.service_policy import StateBasis

LETTER_EVENTS: tuple[cs.Event, ...] = (
    cs.PaymentAuthorized(),
    cs.CustomerNeeded("which plate is on the notice?"),
    cs.CustomerAnswered(),
    cs.ResearchComplete(),
    cs.DraftReady(),
    cs.NotesEdited(),
    cs.CustomerApproved(),
    cs.SubmitRequested(),
    cs.SubmitConfirmed("ltr_1"),
    cs.DeliveryProofStored("proof_1"),
    cs.ReturnConfirmed("refused"),
    cs.RemailRequested(),
    cs.ReturnClosed(),
    cs.Stopped(cs.StopReason.UNRESOLVABLE_FACT, "sources conflict"),
    cs.WindowExpired(),
    cs.AuthorizationExpired(),
    cs.DestinationRefused(),
    cs.LetterNotSubmittable(),
    cs.SubmitRejected("invalid address"),
    cs.ConfirmedNotSent("no letter with idempotency key"),
)

PAYMENT_EVENTS: tuple[ps.PaymentEvent, ...] = (
    ps.CaptureRequested(),
    ps.CaptureSucceeded("ch_1"),
    ps.CaptureFailed("card_declined"),
    ps.RetryCapture(),
    ps.CardDeclined("card_declined"),
    ps.AuthorizationLapsed(),
    ps.ReconciliationExhausted(),
    ps.ReleaseRequested(),
    ps.RefundIssued("re_1", 1000, 2999),
    ps.ReturnedMailRefundRequested("FULL_REFUND", 2999, 1),
    ps.UnexpectedCaptureConfirmed("pi_1", "ch_1"),
    ps.ReauthorizationNeeded(),
)

# Payment events that come from the provider or the retry schedule, not letter effects.
PAYMENT_OUTCOMES: tuple[ps.PaymentEvent, ...] = (
    ps.CaptureSucceeded("ch_1"),
    ps.CaptureFailed("processing_error"),
    ps.RetryCapture(),
    ps.CardDeclined("card_declined"),
    ps.AuthorizationLapsed(),
    ps.ReconciliationExhausted(),
    ps.RefundIssued("re_1", 2999, 2999),
)


# --- Research helpers ----------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"
RETRIEVED_AT = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def fixture(name: str) -> str:
    """A synthetic HTML fixture from tests/fixtures (see each file's header comment)."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def source(
    text: str,
    authority: AuthorityClass = AuthorityClass.OFFICIAL_PROCEDURE,
    url: str = "https://www.sfmta.com/page",
) -> SourceRecord:
    """A SourceRecord for already-extracted text; sha256 is of the text itself."""
    return SourceRecord(
        url=url,
        final_url=url,
        retrieved_at=RETRIEVED_AT,
        http_status=200,
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        content_type="text/html",
        text=text,
        authority_class=authority,
    )


SAN_FRANCISCO = ResolvedAgency(
    "us-ca-san_francisco",
    "San Francisco Municipal Transportation Agency (SFMTA)",
    "CA",
    ("sfmta.com",),
    issuer_type=IssuerType.MUNICIPAL,
    deciding_body="San Francisco Municipal Transportation Agency (SFMTA)",
    basis_key="CA",
)
TEST_BASIS = StateBasis(
    key="CA",
    rules_checked=("Synthetic test rule",),
    links=("https://example.com/test",),
    date_checked=date(2026, 10, 1),
    how_met="Test only; no legal conclusion.",
    checked_by="Test fixture",
)

CONTEST_ADDRESS = MailingAddress(
    "San Francisco Municipal Transportation Agency",
    "Citation Review",
    "11 South Van Ness Avenue",
    None,
    "San Francisco",
    "CA",
    "94103",
)
