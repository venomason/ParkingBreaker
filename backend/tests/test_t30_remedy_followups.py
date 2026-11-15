"""T30 returned-mail remedy follow-ups: M1/L1/L2/L3 regressions and L4 mutant kills.

Each test was written first and confirmed failing against the reviewed code. The
scenarios come from build-kit/evidence/task-T30-2026-10-20/final-review-remedy-opus.md:
a free re-mail that cannot be completed must resolve to one full refund of the
captured amount (M1), the agency-side fallback keeps the proof packet and the
original classification (L1), CMYK signature ink re-prints identically (L2),
signature recovery reads only our own letter-page widget (L3), and the L4 tests
kill the in-memory mutants the review showed surviving.

Return evidence is an application fixture, NOT a Lob payload contract (same seam
as test_t30_remedy).
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from parkingbreaker import case_service
from parkingbreaker import case_state as cs
from parkingbreaker.address_policy import (
    EvidenceKind,
    EvidenceRef,
    Responsibility,
    address_fingerprint,
    remedy_for_responsibility,
)
from parkingbreaker.api import schemas as s
from parkingbreaker.appeal.packet import PacketError, build_packet, frozen_signature_artwork
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse, NotSent, Rejected
from parkingbreaker.payment_fakes import FakePaymentClient, full_outcome
from parkingbreaker.payment_records import PaymentOutcome
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.returned_mail import ReturnCause, UspsReturnReason
from test_appeal_packet import AT, CITATION, letter_text
from test_case_service import Clock, approve_with_preview, drafting_case, fake_resolution
from test_stripe_payment import CHARGE
from test_t30_remedy import (
    Mailing,
    assert_full_refund,
    assert_one_charge,
    mailed_case,
)
from test_t30_remedy_review import (
    edit_before_adoption,
    post_return,
    signature_image,
)

assert fake_resolution
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fake_resolution")]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def letter_state(mailing: Mailing) -> cs.CaseState:
    """A fresh read of the case state; the property reloads from the store."""
    return mailing.case.letter_state


class PendingCapturePayment(FakePaymentClient):
    """The fake captures once but withholds confirmation until reconciliation is released."""

    confirmed = False

    def capture(
        self, authorization_ref: str, amount_cents: int, key: str, *, final_capture: bool
    ) -> PaymentOutcome:
        super().capture(authorization_ref, amount_cents, key, final_capture=final_capture)
        return PaymentOutcome("transient", authorization_ref)

    def retrieve(self, authorization_ref: str) -> PaymentOutcome:
        outcome = super().retrieve(authorization_ref)
        if outcome.status == "succeeded" and not self.confirmed:
            return replace(
                outcome,
                status="requires_capture",
                charge_ref=None,
                amount_received=0,
                amount_capturable=outcome.amount_cents,
            )
        return outcome


@asynccontextmanager
async def pending_capture_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Mailing]:
    """A real mailed case whose original capture has not yet been confirmed."""
    client, real, case_id = await drafting_case(tmp_path, monkeypatch)
    mailing = None
    try:
        clock = Clock()
        object.__setattr__(real._deps, "clock", clock)
        real.step(case_id, s.WorkerStep(step_id="draft"))
        draft = real.draft(case_id)
        real.adopt_text(case_id, s.AdoptText(text=draft.text, text_sha256=draft.text_sha256))
        real.approve(case_id, approve_with_preview(real, case_id, draft.text_sha256))
        record = real._intake(case_id)
        assert record.case_token is not None
        case = real._store.get_case(record.case_token)
        assert case is not None
        decision = real._store.latest_destination_decision(case_id, record.owner)
        assert decision is not None
        payments = PendingCapturePayment(case.authorization_ref)
        payments.object = replace(
            payments.object,
            evidence_version=1,
            original_charge_ref=CHARGE,
            capture_before=int(case.authorization_expires_at.timestamp()),
            classification="verified_object",
        )
        payments.responses = [
            full_outcome(replace(payments.object, status="succeeded", charge_ref=CHARGE))
        ]
        lob = FakeLobClient(
            [LobResponse(200, "ltr_OriginalReturn", evidence=LetterEvidence.PRODUCTION)]
        )
        object.__setattr__(real._deps, "payments", payments)
        object.__setattr__(real._deps, "lob", lob)
        mailing = Mailing(
            real,
            case_id,
            case.case_token,
            tmp_path / "pb.sqlite",
            clock,
            payments,
            lob,
            decision.decision,
        )
        real._dispatch(mailing.token)
        assert mailing.case.letter_state is cs.CaseState.IN_TRANSIT
        cursor = mailing.store.payment_cursor(mailing.token)
        assert cursor is not None
        mailing.clock.now = max(mailing.clock(), cursor.next_at)
        real._dispatch(mailing.token)
        assert mailing.case.payment_state is PaymentState.CAPTURE_RETRYING
        assert mailing.payments.captures == 1
        mailing.restart()
        yield mailing
    finally:
        await client.aclose()
        (real._store if mailing is None else mailing.store).close()


def settle_original_capture(mailing: Mailing) -> None:
    """Provider reconciliation confirms the same original charge after a lost result."""
    assert isinstance(mailing.payments, PendingCapturePayment)
    mailing.payments.confirmed = True
    cursor = mailing.store.payment_cursor(mailing.token)
    assert cursor is not None
    assert not cursor.done
    mailing.clock.now = max(mailing.clock(), cursor.next_at)
    mailing.real._dispatch(mailing.token)
    cursor = mailing.store.payment_cursor(mailing.token)
    assert cursor is not None
    assert cursor.kind == "capture"
    assert cursor.done
    assert mailing.case.payment_state is PaymentState.CAPTURED
    assert mailing.store.returned_mail_refund(mailing.token) is None
    mailing.restart()


@pytest.mark.parametrize("capture_first", [False, True])
@pytest.mark.parametrize("failure", ["provider", "gate", "not_sent"])
async def test_failed_remail_refund_is_orchestrated_after_durable_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capture_first: bool, failure: str
) -> None:
    async with pending_capture_case(tmp_path, monkeypatch) as mailing:
        if capture_first:
            settle_original_capture(mailing)
        mailing.confirm_return()
        mailing.correct_destination()
        if not capture_first:
            # H2 (architecture-v8 20.19, 2026-10-22): a free re-mail is sent only when the
            # original payment was CAPTURED. While the capture is unconfirmed no second
            # letter is sent, nothing is refunded, and the status says no charge was made.
            mailing.lob.script = [
                LobResponse(500 if failure == "not_sent" else 422, error="synthetic refusal")
            ]
            for _ in range(3):
                mailing.real._resolve_return(mailing.case_id)
                mailing.real._dispatch(mailing.token)
                assert mailing.real.advance_due().failures == ()
                mailing.restart()
                assert len(mailing.lob.calls) == 1
                assert len(mailing.lob.script) == 1
                assert mailing.store.submission_cursor(mailing.token, 2) is None
                assert mailing.case.letter_state is cs.CaseState.RETURNED
                assert mailing.store.returned_mail_refund(mailing.token) is None
                assert mailing.payments.refund_calls == 0
            waiting = mailing.real.status(mailing.case_id).return_remedy
            assert waiting is not None
            assert waiting.kind == "RETURN_PENDING"
            assert waiting.remail is False
            assert waiting.refund_status is None
            assert waiting.refund_cents == 0
            assert "no charge has been made yet" in waiting.message.lower()
            assert "refund" not in waiting.message.lower()
            settle_original_capture(mailing)
        mailing.real._resolve_return(mailing.case_id)
        if failure == "gate":
            latest = mailing.store.latest_destination_decision(
                mailing.case_id, mailing.original.owner
            )
            assert latest is not None
            address = replace(latest.decision.address, line1="1000 Newer Verified Correction")
            mailing.store.save_destination_decision(
                mailing.case_id,
                mailing.original.owner,
                replace(latest.decision, address=address, fingerprint=address_fingerprint(address)),
                latest.revision,
            )
        mailing.lob.script = [
            LobResponse(500 if failure == "not_sent" else 422, error="synthetic refusal")
        ]
        real_request = type(mailing.store).request_returned_mail_refund

        def outside_transaction(self: Any, *args: Any, **kwargs: Any) -> Any:
            assert not self._require_open().in_transaction, "refund requested inside transaction"
            return real_request(self, *args, **kwargs)

        monkeypatch.setattr(
            type(mailing.store), "request_returned_mail_refund", outside_transaction
        )
        mailing.real._dispatch(mailing.token)
        mailing.restart()
        if failure == "not_sent":
            assert mailing.case.letter_state is cs.CaseState.SUBMITTING
            assert mailing.store.returned_mail_refund(mailing.token) is None
            mailing.clock.now += timedelta(days=1)
            mailing.real._dispatch(mailing.token)
            mailing.restart()
        cursor = mailing.store.submission_cursor(mailing.token, 2)
        assert cursor is not None
        assert isinstance(cursor.outcome, Rejected | NotSent)
        assert mailing.case.letter_state is cs.CaseState.CLOSED_RETURNED
        # Both completion transactions only record their own results. Refund work
        # reaches the existing scheduler after the confirmed capture is durable.
        assert mailing.store.returned_mail_refund(mailing.token) is None
        mailing.sweep(12)
        assert mailing.case.payment_state is PaymentState.REFUNDED
        assert mailing.payments.refunds[0].amount_cents == mailing.case.amount_cents
        assert mailing.payments.refunds[0].charge_ref == CHARGE
        assert mailing.payments.refunds[0].authorization_ref == mailing.case.authorization_ref
        assert mailing.payments.captures == 1
        assert len({call[3] for call in mailing.payments.calls if call[0] == "capture"}) == 1
        assert len(mailing.payments.refunds) == 1
        intent = mailing.store.returned_mail_refund(mailing.token)
        assert intent is not None
        assert intent.amount_cents == mailing.case.amount_cents
        mailing.sweep(3)
        assert mailing.store.returned_mail_refund(mailing.token) == intent
        assert mailing.payments.refund_calls == 1


@pytest.mark.parametrize(
    "state",
    [
        PaymentState.AUTHORIZED,
        PaymentState.CAPTURE_PENDING,
        PaymentState.CAPTURE_RETRYING,
        PaymentState.RELEASED,
        PaymentState.WRITTEN_OFF,
    ],
)
async def test_failed_unpaid_remail_status_never_promises_a_refund(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: PaymentState
) -> None:
    async with pending_capture_case(tmp_path, monkeypatch) as mailing:
        mailing.confirm_return()
        mailing.correct_destination()
        mailing.real._resolve_return(mailing.case_id)
        mailing.lob.script = [LobResponse(422, error="synthetic refusal")]
        mailing.real._dispatch(mailing.token)
        mailing.restart()
        status = mailing.real._return_remedy_status(
            replace(mailing.case, payment_state=state), mailing.case_id
        )
        assert status is not None
        capturable = {
            PaymentState.AUTHORIZED,
            PaymentState.CAPTURE_PENDING,
            PaymentState.CAPTURE_RETRYING,
        }
        assert (
            "no charge has been made yet" if state in capturable else "no charge was made"
        ) in status.message.lower()
        assert "refund" not in status.message.lower()
        assert status.kind == "RETURN_PENDING"
        assert status.refund_status is None
        assert status.refund_cents == 0
        assert mailing.payments.refund_calls == 0


async def test_agency_fallback_derives_full_refund_from_service_failure_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edit_before_adoption(monkeypatch, mismatched_header=True)
    calls = []
    original = remedy_for_responsibility

    def observed(result: Any, **kwargs: Any) -> Any:
        calls.append((result, kwargs))
        return original(result, **kwargs)

    monkeypatch.setattr(case_service, "remedy_for_responsibility", observed)
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        mailing.confirm_return(cause=ReturnCause.AGENCY_SIDE, reason=UspsReturnReason.REFUSED)
        mailing.correct_destination()
        mailing.sweep(12)
        failures = [(result, kw) for result, kw in calls if result.reason == "SERVICE_FAILURE"]
        assert failures, "fallback bypassed the existing pure responsibility/remedy policy"
        result, kwargs = failures[0]
        assert result.responsibility is Responsibility.OUR_ERROR_FULL_REFUND
        assert kwargs["deadline_feasible"] is False
        stored = mailing.store.returned_mail_resolution(mailing.token, 1)
        assert stored is not None
        assert stored.responsibility is Responsibility.AGENCY_SIDE_EXISTING_REMEDY
        assert stored.reason == "SERVICE_FAILURE"
        assert_full_refund(mailing)
        status = mailing.real.status(mailing.case_id).return_remedy
        assert status is not None
        assert status.proof_packet is not None


# --- M1: a free re-mail that cannot be completed resolves to one full refund --------


@pytest.mark.parametrize("lost_response", [False, True])
async def test_failed_remail_provider_rejection_refunds_the_captured_amount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lost_response: bool
) -> None:
    """Lob rejects the free re-mail: the case must not close captured with no refund."""
    async with mailed_case(tmp_path, monkeypatch, lost_refund_response=lost_response) as mailing:
        mailing.confirm_return()
        mailing.correct_destination()
        mailing.lob.script = [LobResponse(422, error="synthetic invalid mail request")]
        mailing.sweep(12)
        assert mailing.case.attempt == 2
        assert letter_state(mailing) is cs.CaseState.CLOSED_RETURNED
        # The reviewed defect: CLOSED_RETURNED with the payment still captured.
        assert_full_refund(mailing)
        assert_one_charge(mailing)
        remedy = mailing.real.status(mailing.case_id).return_remedy
        assert remedy is not None
        assert remedy.kind == "FULL_REFUND"
        assert remedy.refund_cents == mailing.case.amount_cents
        assert remedy.refund_status == "completed"
        assert remedy.remail is False
        assert "has been arranged" not in remedy.message
        assert "could not be sent" in remedy.message or "was not sent" in remedy.message
        assert "full refund is confirmed" in remedy.message


async def test_failed_remail_gate_refusal_is_our_service_failure_with_full_refund(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Our own address-gate refusal is SERVICE_FAILURE: full refund, never the provider."""
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        mailing.confirm_return()
        mailing.correct_destination()
        mailing.real._resolve_return(mailing.case_id)
        latest = mailing.store.latest_destination_decision(mailing.case_id, mailing.original.owner)
        assert latest is not None
        address = replace(latest.decision.address, line1="1000 Newer Verified Correction")
        newer = replace(
            latest.decision,
            address=address,
            fingerprint=address_fingerprint(address),
            verification_evidence=(
                EvidenceRef("newer-quote", "c" * 64, EvidenceKind.OFFICIAL_SOURCE_QUOTE),
            ),
        )
        mailing.store.save_destination_decision(
            mailing.case_id, mailing.original.owner, newer, latest.revision
        )
        mailing.sweep(12)
        assert len(mailing.lob.calls) == 1
        assert letter_state(mailing) is cs.CaseState.CLOSED_RETURNED
        assert_full_refund(mailing)
        assert_one_charge(mailing)
        remedy = mailing.real.status(mailing.case_id).return_remedy
        assert remedy is not None
        assert remedy.reason_code == "SERVICE_FAILURE"
        assert remedy.refund_cents == mailing.case.amount_cents
        assert remedy.refund_status == "completed"
        assert "mail provider rejected" not in remedy.message
        assert "address verification" in remedy.message
        assert "full refund is confirmed" in remedy.message


async def test_uncertain_remail_waits_until_resolved_then_refunds_when_never_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An uncertain free re-mail keeps waiting; confirmed non-send refunds in full."""
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        mailing.confirm_return()
        mailing.correct_destination()
        mailing.real._resolve_return(mailing.case_id)
        assert letter_state(mailing) is cs.CaseState.SUBMITTING
        mailing.lob.script = [LobResponse(500, error="synthetic 5xx")]
        mailing.real._dispatch(mailing.token)
        mailing.restart()
        assert letter_state(mailing) is cs.CaseState.SUBMITTING
        assert mailing.case.attempt == 2
        # It never closes early and never claims money while it is unresolved.
        assert mailing.payments.refund_calls == 0
        remedy = mailing.real.status(mailing.case_id).return_remedy
        assert remedy is not None
        assert "has been arranged" not in remedy.message
        assert "confirming whether the free re-mail was sent" in remedy.message
        assert remedy.remail is True
        # The lookup resolves the uncertain create as an authoritative non-send.
        mailing.clock.now += timedelta(days=1)
        mailing.sweep(12)
        assert letter_state(mailing) is cs.CaseState.CLOSED_RETURNED
        assert_full_refund(mailing)
        assert_one_charge(mailing)
        remedy = mailing.real.status(mailing.case_id).return_remedy
        assert remedy is not None
        assert "was not sent" in remedy.message
        assert "full refund is confirmed" in remedy.message
        assert remedy.refund_status == "completed"


# --- L1: the agency-side fallback keeps the proof packet and both recorded facts ----


async def test_agency_side_fallback_keeps_proof_packet_and_original_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreproducible agency-side re-mail: full refund AND the proof packet, both facts kept."""
    edit_before_adoption(monkeypatch, mismatched_header=True)
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        mailing.confirm_return(cause=ReturnCause.AGENCY_SIDE, reason=UspsReturnReason.REFUSED)
        mailing.correct_destination()
        mailing.sweep(12)
        assert_full_refund(mailing)
        assert_one_charge(mailing)
        resolved = mailing.store.returned_mail_resolution(mailing.token, 1)
        assert resolved is not None
        # The reviewed defect: the record replaced the agency-side classification
        # with our error, so the status dropped the customer's proof packet.
        assert resolved.responsibility is Responsibility.AGENCY_SIDE_EXISTING_REMEDY
        assert resolved.reason == "SERVICE_FAILURE"
        remedy = mailing.real.status(mailing.case_id).return_remedy
        assert remedy is not None
        assert "full refund is confirmed" in remedy.message
        assert remedy.proof_packet is not None
        first = mailing.store.get(mailing.token, 1)
        assert first is not None
        assert hashlib.sha256(first.request.pdf).hexdigest() == remedy.proof_packet.pdf_sha256


# --- L2: CMYK signature ink prints identically on the re-mail ----------------------


def raster_page1(pdf: bytes, directory: Path) -> Image.Image:
    directory.mkdir(parents=True)
    (directory / "page.pdf").write_bytes(pdf)
    subprocess.run(
        [
            "pdftoppm",
            "-r",
            "150",
            "-f",
            "1",
            "-l",
            "1",
            "-png",
            str(directory / "page.pdf"),
            str(directory / "out"),
        ],
        check=True,
        capture_output=True,
    )
    return Image.open(next(directory.glob("out*.png"))).convert("RGB")


def signature_region(pdf: bytes, raster: Image.Image) -> Image.Image:
    """Crop the printed signature block; the widget Rect is the ink's exact bounds."""
    page = PdfReader(io.BytesIO(pdf)).pages[0]
    rect = None
    for reference in page.get("/Annots", []):
        widget = reference.get_object()
        if str(widget.get("/T", "")) == "Signature":
            rect = [float(value) for value in widget["/Rect"]]
            break
    assert rect is not None
    page_height = float(page.mediabox.height)
    scale = raster.height / page_height
    left, bottom, right, top = rect
    return raster.crop(
        (
            round(left * scale),
            round((page_height - top) * scale),
            round(right * scale),
            round((page_height - bottom) * scale),
        )
    )


def test_cmyk_signature_remail_prints_identical_pixels() -> None:
    """A CMYK JPEG signature re-embeds its ink data: the re-mail prints the same pixels."""
    ink = signature_image("CMYK", "JPEG")
    first = build_packet(letter_text(), CITATION, [], AT, customer_signature=ink)
    recovered = frozen_signature_artwork(first.pdf)
    assert recovered is not None
    second = build_packet(letter_text(), CITATION, [], AT, customer_signature=recovered)
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        original = signature_region(first.pdf, raster_page1(first.pdf, directory / "a"))
        remail = signature_region(second.pdf, raster_page1(second.pdf, directory / "b"))
    assert original.size == remail.size
    assert original.getbbox() is not None, "the signature region is blank"
    assert original.tobytes() == remail.tobytes(), "the re-mail prints different signature pixels"


# --- L3: signature recovery reads only our own letter-page widget -----------------


def with_signature_field(packet: bytes, *, appearance: bool, field_type: str = "/Tx") -> bytes:
    """Append an enclosed form page whose text field is named 'Signature'."""
    writer = PdfWriter(clone_from=io.BytesIO(packet))
    page = writer.add_blank_page(612, 792)
    widget = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/FT"): NameObject(field_type),
            NameObject("/T"): TextStringObject("Signature"),
            NameObject("/Rect"): ArrayObject([NumberObject(v) for v in (72, 72, 300, 100)]),
        }
    )
    if appearance:
        stream = DecodedStreamObject()
        stream.set_data(b"BT /Helv 12 Tf 2 8 Td (Jane Doe) Tj ET")
        stream.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Form"),
                NameObject("/BBox"): ArrayObject([NumberObject(v) for v in (0, 0, 228, 28)]),
            }
        )
        widget[NameObject("/AP")] = DictionaryObject({NameObject("/N"): writer._add_object(stream)})
    page[NameObject("/Annots")] = ArrayObject([writer._add_object(widget)])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.mark.parametrize("appearance", [False, True])
@pytest.mark.parametrize("field_type", ["/Tx", "/Sig"])
@pytest.mark.parametrize("sentences", [3, 32])
def test_enclosed_form_signature_field_is_not_our_signature(
    appearance: bool, field_type: str, sentences: int
) -> None:
    """A genuinely unsigned letter stays unsigned whatever an enclosed form calls its fields."""
    unsigned = build_packet(letter_text(sentences), CITATION, [], AT)
    assert frozen_signature_artwork(unsigned.pdf) is None
    enclosed = with_signature_field(unsigned.pdf, appearance=appearance, field_type=field_type)
    assert frozen_signature_artwork(enclosed) is None


def test_signature_on_second_letter_page_ignores_enclosed_agency_sig() -> None:
    signed = build_packet(
        letter_text(32), CITATION, [], AT, customer_signature=signature_image("RGB", "PNG")
    )
    pages = PdfReader(io.BytesIO(signed.pdf)).pages
    assert pages[1].get("/Annots")
    expected = frozen_signature_artwork(signed.pdf)
    assert expected is not None
    enclosed = with_signature_field(signed.pdf, appearance=True, field_type="/Sig")
    assert frozen_signature_artwork(enclosed) == expected


def test_unreadable_artwork_in_our_widget_still_fails_closed() -> None:
    ink = signature_image("RGB", "PNG")
    packet = build_packet(letter_text(), CITATION, [], AT, customer_signature=ink)
    writer = PdfWriter(clone_from=io.BytesIO(packet.pdf))
    widget = writer.pages[0].get("/Annots", [])[0].get_object()
    assert isinstance(widget, DictionaryObject)
    assert str(widget.get("/T", "")) == "Signature"
    del widget[NameObject("/AP")]
    out = io.BytesIO()
    writer.write(out)
    with pytest.raises(PacketError, match="not recoverable"):
        frozen_signature_artwork(out.getvalue())


# --- L4: kill the mutants the review showed surviving -----------------------------


async def test_attempt2_cost_window_starts_at_the_second_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 14-day cost window anchors at attempt 2's own return, not attempt 1's."""
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        first_return = mailing.clock()
        mailing.confirm_return()
        mailing.correct_destination()
        mailing.sweep()
        assert mailing.case.attempt == 2
        mailing.clock.now = first_return + timedelta(days=10)
        post_return(mailing, event_id="second-return")
        # Five days after the second return: still waiting for cost evidence.
        mailing.clock.now = first_return + timedelta(days=15)
        mailing.sweep()
        assert mailing.payments.refund_calls == 0
        assert letter_state(mailing) is cs.CaseState.RETURNED
        remedy = mailing.real.status(mailing.case_id).return_remedy
        assert remedy is not None
        assert remedy.kind == "RETURN_PENDING"
        assert "awaiting mailing cost evidence" in remedy.message
        # Fourteen days after the second return the estimate resolves it.
        mailing.clock.now = first_return + timedelta(days=24, hours=1)
        mailing.sweep()
        assert mailing.payments.refund_calls == 1
        assert letter_state(mailing) is cs.CaseState.CLOSED_RETURNED
        remedy = mailing.real.status(mailing.case_id).return_remedy
        assert remedy is not None
        assert remedy.refund_status == "completed"
        assert remedy.refund_cents > 0
        assert remedy.refund_cents < mailing.case.amount_cents


@pytest.mark.parametrize("outcome", ["uncertain", "in_transit", "delivered", "not_sent"])
async def test_attempt2_status_copy_reports_each_actual_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """Copy names the real second-attempt outcome; it never claims an arrangement."""
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        mailing.confirm_return()
        mailing.correct_destination()
        if outcome == "uncertain":
            mailing.real._resolve_return(mailing.case_id)
            mailing.lob.script = [LobResponse(500, error="synthetic 5xx")]
            mailing.real._dispatch(mailing.token)
            mailing.restart()
            expected = "confirming whether the free re-mail was sent"
            active = True
        elif outcome == "in_transit":
            mailing.sweep()
            expected = "is in transit to a different verified address"
            active = True
        elif outcome == "delivered":
            mailing.sweep()
            assert letter_state(mailing) is cs.CaseState.IN_TRANSIT
            mailing.store.apply_letter_event(
                mailing.token,
                cs.DeliveryProofStored("proof_2"),
                mailing.case.revision,
                occurred_at=mailing.clock(),
            )
            mailing.restart()
            expected = "Delivery proof for the free re-mail has been recorded"
            active = False
        else:
            mailing.real._resolve_return(mailing.case_id)
            mailing.lob.script = [LobResponse(500, error="synthetic 5xx")]
            mailing.real._dispatch(mailing.token)
            mailing.restart()
            mailing.clock.now += timedelta(days=1)
            mailing.sweep(12)
            assert letter_state(mailing) is cs.CaseState.CLOSED_RETURNED
            expected = "The free re-mail was not sent"
            active = False
        remedy = mailing.real.status(mailing.case_id).return_remedy
        assert remedy is not None
        assert expected in remedy.message
        assert "has been arranged" not in remedy.message
        assert remedy.remail is active
        assert_one_charge(mailing)


@pytest.mark.parametrize("foreign", ["attempt", "letter", "both"])
async def test_return_evidence_reads_only_the_returned_attempt_letter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign: str
) -> None:
    """Cost and cause come from the returned attempt's own letter, never a foreign row."""
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        mailing.confirm_return(mailing_cost=0)
        frozen = mailing.store.get(mailing.token, 1)
        assert frozen is not None
        wrong = json.dumps(
            {
                "event": "letter.returned_to_sender",
                "occurred_at": mailing.clock().isoformat(),
                "tracking_number": None,
                "letter_id": (
                    mailing.lob.letters[frozen.request.submission_token]
                    if foreign == "attempt"
                    else "ltr_ForeignLetter"
                ),
                "attempt": 1 if foreign == "letter" else 2,
                "usps_reason": UspsReturnReason.OTHER.value,
                "mailing_cost_cents": mailing.case.amount_cents,
            },
            sort_keys=True,
        )
        assert mailing.store.record_webhook(
            "lob",
            "foreign-return-row",
            mailing.token,
            wrong,
            occurred_at=mailing.clock(),
            new_effect_id=lambda: uuid4().hex,
        )
        mailing.restart()
        mailing.clock.now += timedelta(days=14)
        mailing.sweep()
        assert_full_refund(mailing)
        assert_one_charge(mailing)
        # A foreign row's full-price "cost" must not erase the refundable balance.
        assert mailing.payments.refunds[0].amount_cents == mailing.case.amount_cents


async def test_fallback_waits_for_the_original_capture_before_refunding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service-failure refund waits for capture reconciliation; it never crashes."""
    edit_before_adoption(monkeypatch, mismatched_header=True)
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        mailing.confirm_return()
        mailing.correct_destination()
        # mailed_case settles the capture before yielding. Present it as still
        # unconfirmed at the remedy seam: the reachable window the guard protects,
        # where the store's own refund check would refuse the intent.
        real_get = type(mailing.store).get_case

        def unconfirmed(self: Any, case_id: str) -> Any:
            case = real_get(self, case_id)
            return (
                None if case is None else replace(case, payment_state=PaymentState.CAPTURE_PENDING)
            )

        monkeypatch.setattr(type(mailing.store), "get_case", unconfirmed)
        mailing.real._resolve_return(mailing.case_id)
        assert mailing.store.returned_mail_resolution(mailing.token, 1) is None
        assert letter_state(mailing) is cs.CaseState.RETURNED
        assert mailing.payments.refund_calls == 0
        monkeypatch.setattr(type(mailing.store), "get_case", real_get)
        mailing.sweep(12)
        assert_full_refund(mailing)
        assert_one_charge(mailing)
