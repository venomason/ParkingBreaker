"""T30 remedy review fixes: regression tests for the five confirmed defects.

Each test was written first and confirmed failing against the reviewed code, then the
source fix landed. Helpers come from test_t30_remedy (the `mailed_case` fixture, its
Mailing helper and the money assertions) and test_case_service; only the evidence
writer that must carry a modified FailureFacts value is local to this module.

Return evidence remains an application fixture, NOT a Lob payload contract: the
production ingress seam exercised here derives only what a signed return webhook
actually carries (the USPS return reason) plus a documented, flagged cost estimate.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject

from parkingbreaker import case_state as cs
from parkingbreaker.address_policy import (
    AddressFaultFinding,
    FailureFacts,
    address_fingerprint,
    classify_address_failure,
)
from parkingbreaker.lob_submission import LobResponse
from parkingbreaker.mail_config import EXPECTED_MAILING_COST, MailClass
from parkingbreaker.mail_request import request_fingerprint
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.research.pdf_text import extract_pdf_text
from parkingbreaker.returned_mail import ReturnCause, UspsReturnReason
from test_case_service import INTAKE, fake_resolution
from test_t30_remedy import (
    Mailing,
    assert_full_refund,
    assert_one_charge,
    customer_messages,
    mailed_case,
    reason_codes,
)

assert fake_resolution
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fake_resolution")]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def letter_state(mailing: Mailing) -> cs.CaseState:
    """A fresh read of the case state; the property reloads from the store."""
    return mailing.case.letter_state


def confirm_return_with(
    mailing: Mailing,
    facts: FailureFacts,
    *,
    reason: UspsReturnReason = UspsReturnReason.NO_SUCH_NUMBER,
    mailing_cost: int = 0,
) -> None:
    """The fixture's return evidence write, with the caller's exact FailureFacts value."""
    case = mailing.case
    frozen = mailing.store.get(mailing.token, case.attempt)
    assert frozen is not None
    payload = json.dumps(
        {
            "event": "letter.returned_to_sender",
            "occurred_at": mailing.clock().isoformat(),
            "tracking_number": None,
            "letter_id": mailing.lob.letters[frozen.request.submission_token],
            "attempt": case.attempt,
            "usps_reason": reason.value,
            "failure_facts": asdict(facts),
            "mailing_cost_cents": mailing_cost,
        },
        sort_keys=True,
    )
    event_id = f"synthetic-return-{case.attempt}"
    assert mailing.store.record_webhook(
        "lob",
        event_id,
        mailing.token,
        payload,
        letter_event=cs.ReturnConfirmed(reason.value),
        occurred_at=mailing.clock(),
        new_effect_id=lambda: uuid4().hex,
    )
    assert mailing.case.letter_state is cs.CaseState.RETURNED
    mailing.restart()


# D1: an additional our-fault never defeats the unconditional full refund of the
# captured amount for a SYSTEM_VERIFIED destination that proved wrong.


@pytest.mark.parametrize(
    "flag",
    ["omitted_required_contents", "service_failure", "duplicate_charge", "return_cause"],
)
async def test_another_our_fault_never_defeats_the_unconditional_full_refund(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        base = FailureFacts(
            mailing.case_id,
            mailed_address_fingerprint=address_fingerprint(mailing.original.address),
            address_finding=AddressFaultFinding.DESTINATION_WRONG_WITH_OTHER_CAUSES,
            finding_evidence_ids=("wrong-destination-proof",),
        )
        if flag == "omitted_required_contents":
            facts = replace(base, omitted_required_contents=True)
        elif flag == "service_failure":
            facts = replace(base, service_failure=True)
        elif flag == "duplicate_charge":
            facts = replace(base, duplicate_charge=True)
        else:
            facts = replace(base, return_cause=ReturnCause.OUR_ERROR)
        confirm_return_with(mailing, facts, mailing_cost=800)
        mailing.correct_destination()
        mailing.sweep()
        # The reviewed defect: two letters, zero refunds, payment still CAPTURED.
        assert len(mailing.lob.calls) == 1, "a re-mail replaced the unconditional refund"
        assert mailing.payments.refund_calls == 1, "zero refunds for a proven wrong destination"
        assert_full_refund(mailing)
        assert_one_charge(mailing)
        assert classify_address_failure(mailing.original, facts).unconditional_full_refund


# D2: the corrected-destination re-mail re-renders the packet; the letter body must not
# still print the returned address.


async def test_remail_re_renders_the_packet_with_the_corrected_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        mailing.confirm_return()
        corrected = mailing.correct_destination()
        mailing.approve_remail()
        mailing.sweep()
        assert mailing.case.attempt == 2
        first = mailing.store.get(mailing.token, 1)
        second = mailing.store.get(mailing.token, 2)
        assert first is not None
        assert second is not None
        assert second.request.recipient == corrected.address
        old_line = mailing.original.address.line1
        new_line = corrected.address.line1
        text1 = extract_pdf_text(first.request.pdf)
        text2 = extract_pdf_text(second.request.pdf)
        assert text1 is not None
        assert text2 is not None
        assert old_line in text1
        assert new_line not in text1
        assert new_line in text2, "the re-mail letter body does not print the corrected address"
        assert old_line not in text2, "the re-mail letter body still prints the returned address"
        # The customer's adopted text, identifiers and attachments are identical otherwise.
        assert text2 == text1.replace(old_line, new_line)
        assert INTAKE["customer_position"]["statement"] in text2
        assert INTAKE["ticket"]["citation_number"] in text2
        # The frozen request and hash for attempt 2 reflect the new PDF.
        assert second.request.pdf != first.request.pdf
        assert (
            hashlib.sha256(second.request.pdf).hexdigest()
            != hashlib.sha256(first.request.pdf).hexdigest()
        )
        assert request_fingerprint(second.request) != request_fingerprint(first.request)
        assert second.request.submission_token != first.request.submission_token
        assert_one_charge(mailing)


def test_re_rendered_packet_keeps_the_captured_signature_ink() -> None:
    """A signed packet keeps the customer's ink through the corrected re-render.

    The ink exists only inside the frozen PDF (the signed `mailed_case` fixture does not
    exist), so this pins the recovery the re-mail relies on. The printed address changes;
    the signature artwork survives.
    """
    from item2_helpers import customer_ink
    from parkingbreaker.appeal.letter import redirect_letter_address
    from parkingbreaker.appeal.packet import build_packet, frozen_signature_artwork
    from samples import CONTEST_ADDRESS
    from test_appeal_packet import AT, CITATION, letter_text

    ink = customer_ink()
    text = letter_text()
    first = build_packet(text, CITATION, [], AT, customer_signature=ink)
    recovered = frozen_signature_artwork(first.pdf)
    assert recovered is not None
    redirected = redirect_letter_address(
        text, CONTEST_ADDRESS, replace(CONTEST_ADDRESS, line1="987 Verified Correction Avenue")
    )
    second = build_packet(redirected, CITATION, [], AT, customer_signature=recovered)
    printed = extract_pdf_text(second.pdf)
    assert printed is not None
    assert "987 Verified Correction Avenue" in printed
    assert CONTEST_ADDRESS.line1 not in printed
    assert frozen_signature_artwork(second.pdf) is not None


# D3: production's signed return webhook must persist the derived return cause and a
# bounded, flagged cost estimate, so a real returned letter resolves instead of staying
# RETURNED/CAPTURED forever.


def signed_return(
    mailing: Mailing, letter_id: str, details_event: str, *, event_id: str
) -> tuple[bytes, dict[str, str]]:
    """A signed Lob return webhook in the documented tracking-event shape."""
    now = mailing.clock()
    payload = json.dumps(
        {
            "id": event_id,
            "event_type": {"id": "letter.returned_to_sender"},
            "date_created": now.isoformat(),
            "body": {
                "id": letter_id,
                "tracking_events": [
                    {
                        "type": "certified",
                        "name": "Returned to Sender",
                        "details": {
                            "event": details_event,
                            "description": "Package is to be returned to sender.",
                            "action_required": False,
                        },
                    }
                ],
            },
        },
        sort_keys=True,
    ).encode()
    stamp = str(int(now.timestamp()))
    signature = hmac.new(b"secret", stamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    return payload, {"lob-signature": signature, "lob-signature-timestamp": stamp}


@pytest.mark.parametrize(
    ("details_event", "expected_reason", "expected_cause"),
    [
        ("no_such_number", "NO_SUCH_NUMBER", "UNKNOWN"),
        ("package_unclaimed", "UNCLAIMED", "AGENCY_SIDE"),
    ],
)
async def test_signed_return_webhook_persists_the_classified_cause_and_bounded_cost_estimate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    details_event: str,
    expected_reason: str,
    expected_cause: str,
) -> None:
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        object.__setattr__(mailing.real._deps, "lob_webhook_secret", "secret")
        frozen = mailing.store.get(mailing.token, 1)
        assert frozen is not None
        letter_id = mailing.lob.letters[frozen.request.submission_token]
        body, headers = signed_return(mailing, letter_id, details_event, event_id="evt_return")
        assert mailing.real.webhook("lob", body, headers)
        assert not mailing.real.webhook("lob", body, headers)
        assert letter_state(mailing) is cs.CaseState.RETURNED
        mailing.restart()
        stored = json.loads(mailing.store.tracking_payloads(mailing.token)[-1])
        # The reason and its classification are persisted with the webhook record.
        assert stored["usps_reason"] == expected_reason
        assert stored["return_cause"] == expected_cause
        # The documented expected cost is recorded as a flagged ESTIMATE, never as an
        # authoritative actual cost, and never negative or above the price.
        assert stored["mailing_cost_status"] == "ESTIMATE"
        assert stored["mailing_cost_unverified"] is True
        estimate = stored["mailing_cost_cents"]
        assert estimate == EXPECTED_MAILING_COST[MailClass.CERTIFIED]
        assert 0 <= estimate <= mailing.case.amount_cents
        if expected_cause == "UNKNOWN":
            # A cost-dependent refund waits, bounded, for an authoritative actual cost.
            mailing.clock.now += timedelta(days=13)
            mailing.sweep()
            assert letter_state(mailing) is cs.CaseState.RETURNED
            assert mailing.payments.refund_calls == 0
            mailing.clock.now += timedelta(days=2)
            mailing.sweep()
            assert letter_state(mailing) is cs.CaseState.CLOSED_RETURNED
            assert mailing.case.payment_state is PaymentState.REFUNDED
            assert mailing.payments.refund_calls == 1
            refunds = [r for r in mailing.payments.refunds if r.status == "succeeded"]
            assert refunds[0].amount_cents == mailing.case.amount_cents - estimate
            assert_one_charge(mailing)
        else:
            mailing.clock.now += timedelta(days=120)
            mailing.sweep()
            # The agency-side classification produced its proof-packet remedy, no refund.
            assert letter_state(mailing) is cs.CaseState.CLOSED_RETURNED
            assert mailing.payments.refund_calls == 0
            text = " ".join(customer_messages(mailing.real.status(mailing.case_id).model_dump()))
            assert "proof" in text.lower()
            assert_one_charge(mailing)


# D4: responsibility is decided before cost validation; a full refund that needs no
# cost is never blocked by an invalid recorded cost.


async def test_invalid_cost_never_blocks_an_unconditional_full_refund(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        facts = FailureFacts(
            mailing.case_id,
            mailed_address_fingerprint=address_fingerprint(mailing.original.address),
            address_finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE,
            finding_evidence_ids=("wrong-destination-proof",),
        )
        confirm_return_with(mailing, facts, mailing_cost=-1)
        mailing.sweep()
        # The reviewed defect: advance_due reports return_cost_invalid, zero refunds.
        assert_full_refund(mailing)
        assert mailing.payments.refund_calls == 1
        assert_one_charge(mailing)


# D5: the status must reflect the final outcome of attempt 2 when the arranged re-mail
# is rejected, not fall back to "has been arranged".


async def test_status_reports_the_final_outcome_of_a_rejected_remail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        mailing.confirm_return()
        mailing.correct_destination()
        mailing.approve_remail()
        mailing.lob.script = [LobResponse(422, error="synthetic invalid mail request")]
        mailing.sweep(12)
        assert mailing.case.attempt == 2
        assert letter_state(mailing) is cs.CaseState.CLOSED_RETURNED
        public = mailing.real.status(mailing.case_id).model_dump()
        text = " ".join(customer_messages(public)).lower()
        # The reviewed defect: the status still claims the re-mail "has been arranged".
        assert "has been arranged" not in text
        assert "could not be sent" in text or "was not sent" in text
        remedy = public["return_remedy"]
        assert isinstance(remedy, dict)
        assert remedy["remail"] is False
        for code in reason_codes(public):
            assert code
            assert all(char.isupper() or char.isdigit() or char in "_:." for char in code)
        # The reviewed defect: the case closed with the payment captured and no refund.
        assert_full_refund(mailing)
        assert_one_charge(mailing)


# Round 2 F1/F7: structural destination, with quoted old address preserved exactly.


def edit_before_adoption(monkeypatch: pytest.MonkeyPatch, *, mismatched_header: bool) -> None:
    from parkingbreaker.api import schemas as s
    from parkingbreaker.appeal.letter import address_block
    from parkingbreaker.case_service import CaseService

    original_step = CaseService.step

    def step(real: CaseService, case_id: str, body: s.WorkerStep) -> s.CaseStatus:
        result = original_step(real, case_id, body)
        if body.step_id == "draft":
            draft = real.draft(case_id)
            record = real._intake(case_id)
            decision = real._store.latest_destination_decision(case_id, record.owner)
            assert decision is not None
            old = address_block(decision.decision.address)
            text = draft.text
            if mismatched_header:
                text = text.replace(
                    old, old.replace(decision.decision.address.line1, "999 Different Header"), 1
                )
            # Full address quoted as a separate paragraph in the customer's statement.
            statement = "\n\nI copied this address from the notice:\n" + old + "\nEnd of my quote."
            text = text.replace("\n\nSincerely,", statement + "\n\nSincerely,")
            real.edit(case_id, s.EditDraft(text=text, expected_sha256=draft.text_sha256))
        return result

    monkeypatch.setattr(CaseService, "step", step)


@pytest.mark.parametrize("mismatched_header", [False, True])
async def test_f1_structural_redirect_preserves_statement_or_refunds_without_remail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatched_header: bool
) -> None:
    from parkingbreaker.appeal.letter import address_block
    from parkingbreaker.appeal.packet import ATTACHMENT_NAME

    edit_before_adoption(monkeypatch, mismatched_header=mismatched_header)
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        first = mailing.store.get(mailing.token, 1)
        assert first is not None
        adopted = PdfReader(io.BytesIO(first.request.pdf)).attachments[ATTACHMENT_NAME][0]
        assert address_block(mailing.original.address).encode() in adopted.split(b"\n\n", 3)[3]
        mailing.confirm_return()
        corrected = mailing.correct_destination()
        if not mismatched_header:
            mailing.approve_remail()
        mailing.sweep()
        if mismatched_header:
            assert len(mailing.lob.calls) == 1, "a mismatched header was re-mailed"
            assert mailing.store.get(mailing.token, 2) is None
            assert_full_refund(mailing)
            public = mailing.real.status(mailing.case_id)
            assert public.return_remedy is not None
            assert not public.return_remedy.remail
            assert "full refund is confirmed" in public.return_remedy.message
        else:
            second = mailing.store.get(mailing.token, 2)
            assert second is not None
            actual = PdfReader(io.BytesIO(second.request.pdf)).attachments[ATTACHMENT_NAME][0]
            # Exactly section 3 is changed; the entire statement and every other byte match.
            sections = adopted.decode().split("\n\n", 3)
            assert sections[2] == address_block(mailing.original.address)
            expected = "\n\n".join([*sections[:2], address_block(corrected.address), sections[3]])
            assert actual == expected.encode()
            assert actual.split(b"\n\n", 3)[3] == adopted.split(b"\n\n", 3)[3]
            assert mailing.payments.refund_calls == 0
        assert_one_charge(mailing)
        mailing.sweep()
        assert len(mailing.lob.calls) == (1 if mismatched_header else 2)


# F2/F7: nonuniform pixels catch unrelated artwork, as well as lost ink and colour modes.


def signature_image(mode: str, fmt: str) -> bytes:
    ink = Image.new("RGBA", (140, 40), (255, 255, 255, 0 if mode == "RGBA" else 255))
    ImageDraw.Draw(ink).line(
        [(5, 35), (30, 5), (55, 30), (100, 10)], fill=(20, 40, 80, 255), width=3
    )
    ink = ink.convert(mode)
    out = io.BytesIO()
    ink.save(out, fmt)
    return out.getvalue()


@pytest.mark.parametrize(
    ("mode", "fmt"),
    [
        ("P", "PNG"),
        ("1", "PNG"),
        ("L", "PNG"),
        ("RGB", "PNG"),
        ("RGBA", "PNG"),
        ("RGB", "JPEG"),
        ("CMYK", "JPEG"),
    ],
)
def test_f2_signature_pixels_survive_every_valid_packet_image(mode: str, fmt: str) -> None:
    from parkingbreaker.appeal.packet import build_packet, frozen_signature_artwork
    from test_appeal_packet import AT, CITATION, letter_text

    ink = signature_image(mode, fmt)
    first = build_packet(letter_text(), CITATION, [], AT, customer_signature=ink)
    recovered = frozen_signature_artwork(first.pdf)
    assert recovered is not None, f"lost {mode} {fmt} signature"
    # JPEG is decoded before comparison; PNG indexed/bilevel/grayscale become RGBA.
    expected = Image.open(io.BytesIO(ink)).convert("RGBA")
    actual = Image.open(io.BytesIO(recovered)).convert("RGBA")
    assert actual.size == expected.size
    assert actual.tobytes() == expected.tobytes(), f"changed {mode} {fmt} signature pixels"
    second = build_packet(letter_text(), CITATION, [], AT, customer_signature=recovered)
    final_ink = frozen_signature_artwork(second.pdf)
    assert final_ink is not None
    assert Image.open(io.BytesIO(final_ink)).convert("RGBA").tobytes() == expected.tobytes()


def sign_initial_packet(monkeypatch: pytest.MonkeyPatch, ink: bytes) -> None:
    from parkingbreaker.case_service import CaseService

    assemble = CaseService._assemble_packet

    def signed(real: CaseService, record: Any, saved: Any, signature_ref: str | None) -> Any:
        ref = real._deps.photos.put(record.owner, ink)
        return assemble(real, record, saved, ref)

    monkeypatch.setattr(CaseService, "_assemble_packet", signed)


async def test_f2_corrupt_signature_fails_closed_and_refunds_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from parkingbreaker.appeal.packet import frozen_signature_artwork

    sign_initial_packet(monkeypatch, signature_image("RGB", "PNG"))
    async with mailed_case(tmp_path, monkeypatch) as mailing:

        def corrupt_then_recover(pdf: bytes) -> bytes | None:
            writer = PdfWriter(clone_from=io.BytesIO(pdf))
            widget = writer.pages[0].get("/Annots", [])[0].get_object()
            assert isinstance(widget, DictionaryObject)
            # Retain the signed widget, but simulate lost/corrupted appearance data.
            del widget[NameObject("/AP")]
            out = io.BytesIO()
            writer.write(out)
            return frozen_signature_artwork(out.getvalue())

        monkeypatch.setattr(
            "parkingbreaker.case_service.frozen_signature_artwork", corrupt_then_recover
        )
        mailing.confirm_return()
        mailing.correct_destination()
        mailing.sweep(12)
        assert len(mailing.lob.calls) == 1, "unsigned re-mail was submitted"
        assert mailing.store.get(mailing.token, 2) is None
        assert_full_refund(mailing)
        assert_one_charge(mailing)
        remedy = mailing.real.status(mailing.case_id).return_remedy
        assert remedy is not None
        assert "full refund is confirmed" in remedy.message


# F3/F7: provider timestamps and later notifications cannot extend our durable deadline.


def post_return(
    mailing: Mailing, *, event_id: str, future_days: int = 0, kind: object = "certified"
) -> None:
    frozen = mailing.store.get(mailing.token, mailing.case.attempt)
    assert frozen is not None
    letter_id = mailing.lob.letters[frozen.request.submission_token]
    body, _headers = signed_return(mailing, letter_id, "no_such_number", event_id=event_id)
    data = json.loads(body)
    data["date_created"] = (mailing.clock() + timedelta(days=future_days)).isoformat()
    if kind == "missing":
        del data["body"]["tracking_events"][0]["type"]
    else:
        data["body"]["tracking_events"][0]["type"] = kind
    if kind != "certified":
        data["body"]["tracking_events"][0]["details"]["event"] = "package_unclaimed"
    payload = json.dumps(data).encode()
    stamp = str(int(mailing.clock().timestamp()))
    signature = hmac.new(b"secret", stamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    object.__setattr__(mailing.real._deps, "lob_webhook_secret", "secret")
    assert mailing.real.webhook(
        "lob", payload, {"lob-signature": signature, "lob-signature-timestamp": stamp}
    )
    mailing.restart()


@pytest.mark.parametrize("future_days", [0, 365])
async def test_f3_first_ingestion_bounds_cost_wait_despite_repeated_or_future_notifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, future_days: int
) -> None:
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        first_confirmation = mailing.clock()
        post_return(mailing, event_id="first", future_days=future_days)
        mailing.clock.now = first_confirmation + timedelta(days=13)
        post_return(mailing, event_id="day13", future_days=future_days)
        mailing.sweep()
        assert mailing.payments.refund_calls == 0
        mailing.clock.now = first_confirmation + timedelta(days=14)
        mailing.sweep()
        assert letter_state(mailing) is cs.CaseState.CLOSED_RETURNED
        assert mailing.case.payment_state is PaymentState.REFUNDED
        assert mailing.payments.refund_calls == 1
        mailing.clock.now = first_confirmation + timedelta(days=26)
        post_return(mailing, event_id="day26", future_days=future_days)
        mailing.sweep()
        assert mailing.payments.refund_calls == 1
        assert len(mailing.lob.calls) == 1
        assert_one_charge(mailing)


# F4: each customer-visible branch distinguishes estimated from actual deductions.


@pytest.mark.parametrize("branch", ["pending", "completed", "zero"])
async def test_f4_estimated_deduction_copy_is_truthful_in_every_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        post_return(mailing, event_id="estimate")
        mailing.clock.now += timedelta(days=14)
        if branch == "zero":
            monkeypatch.setattr(
                "parkingbreaker.case_service.estimated_mailing_cost", lambda _class, price: price
            )
        mailing.real._resolve_return(mailing.case_id)
        if branch == "completed":
            mailing.sweep()
        public = mailing.real.status(mailing.case_id)
        assert public.return_remedy is not None
        assert "estimated mailing cost" in public.return_remedy.message.lower()
        assert "actual mailing cost" not in public.return_remedy.message.lower()
        if branch == "pending":
            assert public.return_remedy.refund_status == "pending"
        if branch == "completed":
            assert public.return_remedy.refund_status == "completed"
        if branch == "zero":
            assert public.return_remedy.refund_cents == 0
        assert_one_charge(mailing)


# F5/F7: local gate failures are ours; a second return is never an arranged re-mail.


async def test_f5_local_gate_refusal_does_not_blame_the_mail_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from parkingbreaker.address_policy import EvidenceKind, EvidenceRef

    async with mailed_case(tmp_path, monkeypatch) as mailing:
        mailing.confirm_return()
        mailing.correct_destination()
        mailing.approve_remail()
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
        remedy = mailing.real.status(mailing.case_id).return_remedy
        assert remedy is not None
        assert remedy.remail is False
        assert "mail provider rejected" not in remedy.message
        assert "address verification" in remedy.message
        # The reviewed defect: our own refusal closed the case with no refund.
        assert_full_refund(mailing)
        assert_one_charge(mailing)


@pytest.mark.parametrize("cost_resolved", [False, True])
async def test_f5_second_return_reports_its_own_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cost_resolved: bool
) -> None:
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        mailing.confirm_return()
        mailing.correct_destination()
        mailing.approve_remail()
        mailing.sweep()
        assert mailing.case.attempt == 2
        post_return(mailing, event_id="second-return")
        if cost_resolved:
            mailing.clock.now += timedelta(days=14)
        mailing.sweep()
        remedy = mailing.real.status(mailing.case_id).return_remedy
        assert remedy is not None
        assert remedy.attempt == 2
        assert not remedy.remail
        assert "has been arranged" not in remedy.message
        assert "re-mail was returned" in remedy.message
        if not cost_resolved:
            assert "awaiting mailing cost evidence" in remedy.message
            assert mailing.payments.refund_calls == 0
        else:
            assert remedy.refund_status == "completed"
            assert mailing.payments.refund_calls == 1
        assert len(mailing.lob.calls) == 2
        assert_one_charge(mailing)


# F6: signed malformed event shapes do not imply agency fault or remove a refund.


@pytest.mark.parametrize("kind", ["bogus", None, "missing"])
async def test_f6_unsupported_tracking_types_never_classify_a_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: object
) -> None:
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        post_return(mailing, event_id="bad-type", kind=kind)
        data = json.loads(mailing.store.tracking_payloads(mailing.token)[-1])
        assert data["usps_reason"] is None
        assert data["return_cause"] == "UNKNOWN"
        mailing.clock.now += timedelta(days=14)
        mailing.sweep()
        assert mailing.payments.refund_calls == 1
        assert_one_charge(mailing)


@pytest.mark.parametrize("reverse_decode", [False, True])
def test_f2_image_mask_preserves_rendered_ink(reverse_decode: bool) -> None:
    from PIL import ImageOps
    from pypdf.generic import ArrayObject, BooleanObject, NumberObject

    from parkingbreaker.appeal.packet import build_packet, frozen_signature_artwork
    from test_appeal_packet import AT, CITATION, letter_text

    ink = signature_image("1", "PNG")
    packet = build_packet(letter_text(), CITATION, [], AT, customer_signature=ink)
    writer = PdfWriter(clone_from=io.BytesIO(packet.pdf))
    widget = writer.pages[0].get("/Annots", [])[0].get_object()
    assert isinstance(widget, DictionaryObject)
    appearance = widget.get("/AP", {})["/N"]
    objects = appearance["/Resources"]["/XObject"]
    image = next(iter(objects.values())).get_object()
    image[NameObject("/ImageMask")] = BooleanObject(True)
    del image[NameObject("/ColorSpace")]
    image[NameObject("/Decode")] = ArrayObject(
        [NumberObject(1 if reverse_decode else 0), NumberObject(0 if reverse_decode else 1)]
    )
    out = io.BytesIO()
    writer.write(out)
    recovered = frozen_signature_artwork(out.getvalue())
    assert recovered is not None
    actual = Image.open(io.BytesIO(recovered)).convert("RGBA")
    expected = Image.new("RGBA", (140, 40), (0, 0, 0, 0))
    alpha = Image.open(io.BytesIO(ink)).convert("L")
    expected.putalpha(alpha if reverse_decode else ImageOps.invert(alpha))
    assert actual.tobytes() == expected.tobytes()


async def test_f4_later_actual_evidence_cannot_relabel_an_estimated_deduction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        post_return(mailing, event_id="estimate-original")
        mailing.clock.now += timedelta(days=14)
        mailing.sweep()
        original = mailing.real.status(mailing.case_id).return_remedy
        assert original is not None
        data = json.loads(mailing.store.tracking_payloads(mailing.token)[-1])
        data["mailing_cost_status"] = "ACTUAL"
        data["mailing_cost_unverified"] = False
        data["occurred_at"] = mailing.clock().isoformat()
        assert mailing.store.record_webhook(
            "lob",
            "later-actual-cost",
            mailing.token,
            json.dumps(data),
            occurred_at=mailing.clock(),
            new_effect_id=lambda: uuid4().hex,
        )
        mailing.restart()
        current = mailing.real.status(mailing.case_id).return_remedy
        assert current is not None
        assert "estimated mailing cost" in current.message
        assert current == original
        assert mailing.payments.refund_calls == 1


def test_f7_fidelity_regression_rejects_unrelated_artwork(monkeypatch: pytest.MonkeyPatch) -> None:
    """The round-2 red-rectangle mutation must be caught by the new pixel assertions."""
    unrelated = io.BytesIO()
    Image.new("RGB", (140, 40), "red").save(unrelated, "PNG")
    monkeypatch.setattr(
        "parkingbreaker.appeal.packet.frozen_signature_artwork", lambda _pdf: unrelated.getvalue()
    )
    with pytest.raises(AssertionError, match="signature pixels"):
        test_f2_signature_pixels_survive_every_valid_packet_image("RGB", "PNG")


@pytest.mark.parametrize(
    ("mode", "fmt"),
    [
        ("P", "PNG"),
        ("1", "PNG"),
        ("L", "PNG"),
        ("RGB", "PNG"),
        ("RGBA", "PNG"),
        ("RGB", "JPEG"),
        ("CMYK", "JPEG"),
    ],
)
async def test_f2_signed_remail_preserves_original_pixels_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, fmt: str
) -> None:
    from parkingbreaker.appeal.packet import frozen_signature_artwork

    ink = signature_image(mode, fmt)
    sign_initial_packet(monkeypatch, ink)
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        mailing.confirm_return()
        mailing.correct_destination()
        mailing.approve_remail()
        mailing.sweep()
        second = mailing.store.get(mailing.token, 2)
        assert second is not None
        recovered = frozen_signature_artwork(second.request.pdf)
        assert recovered is not None
        expected = Image.open(io.BytesIO(ink)).convert("RGBA")
        actual = Image.open(io.BytesIO(recovered)).convert("RGBA")
        assert actual.size == expected.size
        assert actual.tobytes() == expected.tobytes()
        assert len(mailing.lob.calls) == 2
        assert mailing.payments.refund_calls == 0
        assert_one_charge(mailing)


@pytest.mark.parametrize("branch", ["pending", "completed", "zero"])
async def test_f4_authoritative_actual_cost_copy_stays_actual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        mailing.confirm_return(mailing_cost=mailing.case.amount_cents if branch == "zero" else 800)
        mailing.real._resolve_return(mailing.case_id)
        if branch == "completed":
            mailing.sweep()
        remedy = mailing.real.status(mailing.case_id).return_remedy
        assert remedy is not None
        assert "actual mailing cost" in remedy.message
        assert "estimated" not in remedy.message
        assert_one_charge(mailing)


async def test_f4_v1_migration_preserves_money_bindings_and_unknown_cost_basis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from parkingbreaker.transaction_store import SqliteTransactionStore

    async with mailed_case(tmp_path, monkeypatch) as mailing:
        mailing.confirm_return(mailing_cost=800)
        mailing.sweep()
        before = mailing.store.returned_mail_resolution(mailing.token, 1)
        frozen_before = mailing.store.get(mailing.token, 1)
        payment_before = mailing.store.payment_cursor(mailing.token)
        assert before is not None
        mailing.store.close()
        # Reproduce the deployed v1 schema exactly, with all its original columns/data.
        with sqlite3.connect(mailing.path) as database:
            database.execute("ALTER TABLE pb_return_remedies RENAME TO v2_remedies")
            database.execute("""CREATE TABLE pb_return_remedies (
        case_token TEXT NOT NULL,
        attempt INTEGER NOT NULL CHECK (attempt IN (1, 2)),
        responsibility TEXT NOT NULL,
        reason TEXT NOT NULL,
        kind TEXT,
        refund_cents INTEGER NOT NULL CHECK (refund_cents >= 0),
        remails INTEGER NOT NULL CHECK (remails IN (0, 1)),
        resolved_at TEXT NOT NULL,
        PRIMARY KEY (case_token, attempt)
    )""")
            database.execute(
                "INSERT INTO pb_return_remedies SELECT case_token, attempt, responsibility, "
                "reason, kind, refund_cents, remails, resolved_at FROM v2_remedies"
            )
            database.execute("DROP TABLE v2_remedies")
            database.execute(
                "UPDATE pb_store_versions SET version=1 WHERE component='return_remedies'"
            )
        # Production open performs the migration atomically. No refund/provider replay.
        mailing.real = type(mailing.real)(
            replace(mailing.real._deps, store=SqliteTransactionStore(mailing.path))
        )
        after = mailing.store.returned_mail_resolution(mailing.token, 1)
        assert after is not None
        assert after == replace(before, cost_is_estimate=None)
        assert mailing.store.get(mailing.token, 1) == frozen_before
        assert mailing.store.payment_cursor(mailing.token) == payment_before
        assert mailing.store.frozen_destination(mailing.token, 1) == mailing.original
        remedy = mailing.real.status(mailing.case_id).return_remedy
        assert remedy is not None
        assert "recorded mailing cost" in remedy.message
        mailing.sweep()
        assert mailing.payments.refund_calls == 1
        assert_one_charge(mailing)
