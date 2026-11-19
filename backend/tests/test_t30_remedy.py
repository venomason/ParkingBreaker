"""T30 return contracts: real CaseService, SQLite, and the existing provider fakes.

Only I/O is faked. Intake, consent, drafting, adoption, packet freeze, dispatch,
payment, and scheduler recovery use production code. The second-return test seeds
the existing first-remedy transition explicitly to isolate the durable re-mail cap;
all automatic-remedy tests leave those transitions to the real scheduler.

Return evidence is an application fixture, NOT an asserted Lob payload contract:
the existing record_webhook seam atomically saves ReturnConfirmed and a payload
containing the existing FailureFacts value, exact attempt identity and a synthetic
actual mailing cost. Production currently has no typed return-evidence ingress or
reader; these tests specify the downstream contract, not provider qualification.
NO_SUCH_NUMBER alone is never treated as proof of sole customer fault.

Since T30 step 1 (ce596e4) the payment fake supports exact partial refunds; partial-refund
coverage checks the requested amount, one provider refund, and the truthful settlement.
Lost-response coverage uses the fake's real refund/cache/inventory semantics.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from httpx2 import ASGITransport, AsyncClient

from mail_samples import seed_approval
from parkingbreaker import case_state as cs
from parkingbreaker.address_policy import (
    AddressFaultFinding,
    AddressProvenance,
    DestinationDecision,
    EvidenceKind,
    EvidenceRef,
    FailureFacts,
    Responsibility,
    address_fingerprint,
    classify_address_failure,
    remedy_for_responsibility,
)
from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.approval_manifest import build_remail_approval_manifest
from parkingbreaker.case_service import CaseService
from parkingbreaker.intake_store import IntakeConflict
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.mail_request import envelope_sha256
from parkingbreaker.payment_fakes import FakePaymentClient, full_outcome
from parkingbreaker.payment_records import PaymentOutcome
from parkingbreaker.payment_state import PaymentEffect, PaymentState
from parkingbreaker.returned_mail import RemedyKind, ReturnCause, UspsReturnReason
from parkingbreaker.state_table import IllegalTransition
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore
from prepared_packet_support import redirected_pdf
from test_case_service import (
    ALICE,
    BOB,
    Clock,
    approve_with_preview,
    drafting_case,
    fake_resolution,
)
from test_payment_coordinator import compensation_client
from test_stripe_payment import CHARGE

assert fake_resolution
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fake_resolution")]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class LostRefundResponse(FakePaymentClient):
    """Refund succeeds in the fake ledger; its first response never reaches the app."""

    lost_responses = 0

    def refund(
        self,
        authorization_ref: str,
        charge_ref: str,
        amount_cents: int,
        key: str,
        *,
        captured_cents: int,
    ) -> PaymentOutcome:
        outcome = super().refund(
            authorization_ref, charge_ref, amount_cents, key, captured_cents=captured_cents
        )
        if self.lost_responses == 0:
            self.lost_responses += 1
            raise TimeoutError("synthetic lost refund response")
        return outcome


@dataclass
class Mailing:
    real: CaseService
    case_id: str
    token: str
    path: Path
    clock: Clock
    payments: FakePaymentClient
    lob: FakeLobClient
    original: DestinationDecision

    @property
    def store(self) -> SqliteTransactionStore:
        return self.real._store

    @property
    def case(self) -> CaseRecord:
        saved = self.store.get_case(self.token)
        assert saved is not None
        return saved

    def restart(self) -> None:
        deps = self.real._deps
        self.store.close()
        self.real = CaseService(replace(deps, store=SqliteTransactionStore(self.path)))

    def remail_approval(self) -> s.Approve:
        saved = self.store.get_draft(self.case_id)
        assert saved is not None
        assert saved.adopted_sha256 is not None
        packet = self.real.packet_preview(
            self.case_id, s.PacketPreviewRequest(adopted_sha256=saved.adopted_sha256, remail=True)
        )
        return s.Approve(
            remail=True,
            adopted_sha256=saved.adopted_sha256,
            packet_sha256=packet.packet_sha256,
            envelope_sha256=packet.envelope_sha256,
        )

    def approve_remail(self) -> None:
        self.real.approve(self.case_id, self.remail_approval())

    def sweep(self, count: int = 8) -> None:
        # Each bounded invocation can do only one payment operation. Reopen every
        # time: no in-memory remedy, consent, attempt counter, or refund key suffices.
        for _ in range(count):
            result = self.real.advance_due()
            assert result.failures == (), result.failures
            self.restart()
            cursor = self.store.payment_cursor(self.token)
            if cursor is not None and not cursor.done:
                self.clock.now = max(self.clock.now, cursor.next_at)

    def correct_destination(self) -> DestinationDecision:
        current = self.store.latest_destination_decision(self.case_id, self.original.owner)
        assert current is not None
        address = replace(self.original.address, line1="987 Verified Correction Avenue")
        decision = DestinationDecision(
            self.case_id,
            self.original.owner,
            address,
            address_fingerprint(address),
            AddressProvenance.SYSTEM_VERIFIED,
            self.original.refund_policy_version,
            self.clock(),
            verification_evidence=(
                EvidenceRef(
                    "corrected-official-quote", "b" * 64, EvidenceKind.OFFICIAL_SOURCE_QUOTE
                ),
            ),
        )
        self.store.save_destination_decision(
            self.case_id, self.original.owner, decision, current.revision
        )
        self.restart()
        return decision

    def confirm_return(
        self,
        *,
        cause: ReturnCause = ReturnCause.UNKNOWN,
        finding: AddressFaultFinding = AddressFaultFinding.NOT_ESTABLISHED,
        reason: UspsReturnReason = UspsReturnReason.OTHER,
        evidence_ids: tuple[str, ...] = (),
        mailing_cost: int = 0,
    ) -> FailureFacts:
        case = self.case
        frozen = self.store.get(self.token, case.attempt)
        assert frozen is not None
        cursor = self.store.submission_cursor(self.token, case.attempt)
        assert cursor is not None
        facts = FailureFacts(
            self.case_id,
            return_cause=cause,
            mailed_address_fingerprint=address_fingerprint(frozen.request.recipient),
            address_finding=finding,
            finding_evidence_ids=evidence_ids,
        )
        payload = json.dumps(
            {
                "event": "letter.returned_to_sender",
                "occurred_at": self.clock().isoformat(),
                "tracking_number": None,
                "letter_id": self.lob.letters[frozen.request.submission_token],
                "attempt": case.attempt,
                "usps_reason": reason.value,
                "failure_facts": asdict(facts),
                "mailing_cost_cents": mailing_cost,
            },
            sort_keys=True,
        )
        event_id = f"synthetic-return-{case.attempt}"
        assert self.store.record_webhook(
            "lob",
            event_id,
            self.token,
            payload,
            letter_event=cs.ReturnConfirmed(reason.value),
            occurred_at=self.clock(),
            new_effect_id=lambda: uuid4().hex,
        )
        # Replay is checked at the actual atomic event/deduplication seam.
        assert not self.store.record_webhook(
            "lob",
            event_id,
            self.token,
            payload,
            letter_event=cs.ReturnConfirmed(reason.value),
            occurred_at=self.clock(),
            new_effect_id=lambda: uuid4().hex,
        )
        assert self.case.letter_state is cs.CaseState.RETURNED
        self.restart()
        assert payload in self.store.tracking_payloads(self.token)
        return facts


@asynccontextmanager
async def mailed_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    directed: bool = False,
    lost_refund_response: bool = False,
) -> AsyncIterator[Mailing]:
    # Current production cannot create a new unverified mailing. Returned-mail
    # tests below load a synthetic historical provenance after the original send.
    client, real, case_id = await drafting_case(tmp_path, monkeypatch)
    mailing: Mailing | None = None
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
        assert decision.decision.provenance is AddressProvenance.SYSTEM_VERIFIED
        payment_type = LostRefundResponse if lost_refund_response else FakePaymentClient
        payments = payment_type(case.authorization_ref)
        # Keep the real authorization's exact charge/expiry identity. The fake's
        # default "charge-original" is deliberately unsuitable for this service case.
        payments.object = replace(
            payments.object,
            evidence_version=1,
            original_charge_ref=CHARGE,
            capture_before=int(case.authorization_expires_at.timestamp()),
            classification="verified_object",
        )
        payments.responses = [
            replace(
                full_outcome(
                    PaymentOutcome("succeeded", case.authorization_ref, charge_ref=CHARGE)
                ),
                evidence_version=1,
                original_charge_ref=CHARGE,
                capture_before=int(case.authorization_expires_at.timestamp()),
                classification="verified_object",
            )
        ]
        lob = FakeLobClient(
            [
                LobResponse(200, "ltr_OriginalReturn", evidence=LetterEvidence.PRODUCTION),
                LobResponse(200, "ltr_FreeRemail", evidence=LetterEvidence.PRODUCTION),
            ]
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
        mailing.sweep(3)
        assert mailing.case.letter_state is cs.CaseState.IN_TRANSIT
        assert mailing.case.payment_state is PaymentState.CAPTURED
        assert payments.captures == 1
        assert len(lob.calls) == 1
        if directed:
            from test_t30_mail_gate import save_customer_instruction

            # Explicit pre-policy snapshot seed, never an opt-in production route.
            mailing.original = save_customer_instruction(mailing.store, case_id, record.owner)
            assert mailing.original.provenance is AddressProvenance.CUSTOMER_DIRECTED_UNVERIFIED
        yield mailing
    finally:
        await client.aclose()
        (real._store if mailing is None else mailing.store).close()


def assert_one_charge(mailing: Mailing) -> None:
    assert mailing.payments.captures == 1
    assert [call[0] for call in mailing.payments.calls].count("capture") == 1
    assert not any(call[0] in {"authorize", "release"} for call in mailing.payments.calls)
    capture_effects = [
        row
        for row in mailing.store.effects(mailing.token)
        if row.machine == "payment" and row.effect is PaymentEffect.CAPTURE_PAYMENT
    ]
    assert len(capture_effects) == 1


def assert_full_refund(mailing: Mailing) -> None:
    assert mailing.case.payment_state is PaymentState.REFUNDED, (
        "Returned-mail refund never reached the existing payment path",
        mailing.case,
    )
    assert mailing.case.letter_state is cs.CaseState.CLOSED_RETURNED
    assert mailing.payments.refund_calls == 1
    refunds = [r for r in mailing.payments.refunds if r.status == "succeeded"]
    assert len(refunds) == 1
    assert refunds[0].amount_cents == mailing.case.amount_cents
    assert refunds[0].authorization_ref == mailing.case.authorization_ref
    assert refunds[0].charge_ref == CHARGE
    events = [
        json.loads(row.event_json)
        for row in mailing.store.transition_history(mailing.token)
        if row.machine == "payment" and row.event_name == "RefundIssued"
    ]
    assert events == [
        {
            "refund_ref": refunds[0].refund_ref,
            "amount_cents": mailing.case.amount_cents,
            "captured_cents": mailing.case.amount_cents,
        }
    ]
    cursor = mailing.store.payment_cursor(mailing.token)
    assert cursor is not None
    assert cursor.kind == "refund"
    assert cursor.done
    # Supervisor amendment 2026-10-21: effect renamed / partial refunds supported
    # by T30 step 1 (ce596e4).
    refund_effects = [
        row
        for row in mailing.store.effects(mailing.token)
        if row.machine == "payment" and row.effect is PaymentEffect.REFUND_RETURNED_MAIL
    ]
    assert len(refund_effects) == 1
    assert refund_effects[0].completed_at is not None
    assert_one_charge(mailing)


@pytest.mark.parametrize(
    ("correction_available", "deadline_expired"), [(False, True), (True, False), (True, True)]
)
async def test_system_verified_wrong_destination_always_full_refund(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    correction_available: bool,
    deadline_expired: bool,
) -> None:
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        original = mailing.store.get(mailing.token, 1)
        facts = mailing.confirm_return(
            finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE,
            reason=UspsReturnReason.NO_SUCH_NUMBER,
            evidence_ids=("return-destination-proof",),
        )
        if correction_available:
            mailing.correct_destination()
        if deadline_expired:
            mailing.clock.now += timedelta(days=120)
        responsibility = classify_address_failure(mailing.original, facts)
        assert responsibility.unconditional_full_refund
        remedy = remedy_for_responsibility(
            responsibility,
            deadline_feasible=correction_available,
            other_verified_address_exists=correction_available,
            price_cents=mailing.case.amount_cents,
            mailing_cost_cents=800,
            remails_used=0,
        )
        assert remedy is not None
        assert remedy.kind is RemedyKind.FULL_REFUND
        mailing.sweep()
        assert_full_refund(mailing)
        assert len(mailing.lob.calls) == 1  # Full refund even when a re-mail is feasible.
        assert mailing.store.get(mailing.token, 1) == original
        before = mailing.store.transition_history(mailing.token)
        mailing.sweep(3)
        assert_full_refund(mailing)
        assert mailing.store.transition_history(mailing.token) == before


async def test_frozen_customer_instruction_keeps_exclusion_after_verified_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with mailed_case(tmp_path, monkeypatch, directed=True) as mailing:
        frozen = mailing.store.get(mailing.token, 1)
        facts = mailing.confirm_return(
            finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE,
            reason=UspsReturnReason.NO_SUCH_NUMBER,
            evidence_ids=("sole-cause-return-proof",),
        )
        correction = mailing.correct_destination()
        assert correction.provenance is AddressProvenance.SYSTEM_VERIFIED
        assert correction.fingerprint != mailing.original.fingerprint
        assert classify_address_failure(mailing.original, facts).exclusion_applies
        # Using today's verified correction misclassifies the original mailing.
        assert not classify_address_failure(correction, facts).exclusion_applies
        mailing.clock.now += timedelta(days=120)
        mailing.sweep()
        assert mailing.case.letter_state is cs.CaseState.CLOSED_RETURNED, (
            "The scheduler did not close the proven customer-address-only return"
        )
        assert mailing.case.payment_state is PaymentState.CAPTURED
        assert mailing.payments.refund_calls == 0
        assert len(mailing.lob.calls) == 1
        assert mailing.store.get(mailing.token, 1) == frozen
        assert_one_charge(mailing)


@pytest.mark.parametrize("directed", [False, True])
async def test_unknown_cause_remails_only_after_fresh_exact_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directed: bool
) -> None:
    async with mailed_case(tmp_path, monkeypatch, directed=directed) as mailing:
        first = mailing.store.get(mailing.token, 1)
        facts = mailing.confirm_return(reason=UspsReturnReason.NO_SUCH_NUMBER)
        assert not classify_address_failure(mailing.original, facts).exclusion_applies
        corrected = mailing.correct_destination()
        approval = mailing.remail_approval()
        first_approval = mailing.store.get_approval_manifest(mailing.token, 1)
        assert first_approval is not None
        mailing.sweep(2)
        assert mailing.case.letter_state is cs.CaseState.RETURNED
        assert mailing.store.get(mailing.token, 2) is None
        assert len(mailing.lob.calls) == 1
        assert first is not None
        with pytest.raises(ServiceConflict, match="packet_preview_mismatch"):
            mailing.real.approve(
                mailing.case_id,
                approval.model_copy(update={"envelope_sha256": envelope_sha256(first.request)}),
            )
        mailing.clock.now += timedelta(minutes=1)
        async with AsyncClient(
            transport=ASGITransport(app=create_app(mailing.real)), base_url="http://real.test"
        ) as client:
            path = f"/cases/{mailing.case_id}/approval"
            foreign = await client.post(path, headers=BOB, json=approval.model_dump(mode="json"))
            assert foreign.status_code == 404
            accepted = await client.post(path, headers=ALICE, json=approval.model_dump(mode="json"))
            assert accepted.status_code == 200, accepted.text
        manifest = mailing.store.get_approval_manifest(mailing.token, 2)
        assert manifest is not None
        assert datetime.fromisoformat(manifest.approved_at_utc) == mailing.clock()
        assert manifest.approved_at_utc > first_approval.approved_at_utc
        assert manifest.packet_pdf_sha256 == approval.packet_sha256
        assert manifest.envelope_sha256 == approval.envelope_sha256
        mailing.restart()
        mailing.real.approve(mailing.case_id, approval)
        mailing.sweep()
        remailing = mailing.case
        assert remailing.attempt == 2
        assert remailing.letter_state is cs.CaseState.IN_TRANSIT
        second = mailing.store.get(mailing.token, 2)
        assert second is not None
        assert second.request.recipient == corrected.address
        assert second.request.recipient != mailing.original.address
        assert mailing.store.get(mailing.token, 1) == first
        assert len(mailing.lob.calls) == 2
        assert len({key for key, _ in mailing.lob.calls}) == 2
        assert mailing.payments.refund_calls == 0
        assert_one_charge(mailing)
        mailing.sweep(3)
        assert len(mailing.lob.calls) == 2
        assert_one_charge(mailing)


@pytest.mark.parametrize("legacy", ["inherited_text", "inherited_timestamp"])
async def test_second_create_rejects_legacy_inherited_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: str
) -> None:
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        first = mailing.store.get(mailing.token, 1)
        manifest = mailing.store.get_approval_manifest(mailing.token, 1)
        saved = mailing.store.get_draft(mailing.case_id)
        assert first is not None
        assert manifest is not None
        assert saved is not None
        mailing.confirm_return()
        correction = mailing.correct_destination()
        mailing.clock.now += timedelta(minutes=1)
        # Reproduce a frozen attempt from before explicit remail approval was enforced.
        request = replace(
            first.request,
            submission_token=uuid4().hex,
            recipient=correction.address,
            pdf=redirected_pdf(mailing.real, mailing.case_id, correction.address),
        )
        mailing.store.prepare(mailing.token, 2, request, mailing.clock())
        mailing.store.record_approval_manifest(
            build_remail_approval_manifest(
                manifest,
                request,
                approved_at=mailing.clock() if legacy == "inherited_text" else first.prepared_at,
                approved_text=saved.letter_text
                if legacy == "inherited_text"
                else "Synthetic changed letter",
                affirmations=manifest.affirmations,
            )
        )
        mailing.store.apply_letter_event(
            mailing.token,
            cs.RemailRequested(),
            mailing.case.revision,
            occurred_at=mailing.clock(),
            new_effect_id=lambda: uuid4().hex,
        )
        mailing.restart()
        mailing.sweep(12)
        assert len(mailing.lob.calls) == 1
        assert_full_refund(mailing)
        assert_one_charge(mailing)


@pytest.mark.parametrize("directed", [False, True])
async def test_unknown_cause_without_correction_refunds_and_never_blames_customer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directed: bool
) -> None:
    async with mailed_case(tmp_path, monkeypatch, directed=directed) as mailing:
        # Zero actual cost is a synthetic ledger fact, not a Lob price assertion.
        facts = mailing.confirm_return(mailing_cost=0)
        assert not classify_address_failure(mailing.original, facts).exclusion_applies
        mailing.clock.now += timedelta(days=120)
        mailing.sweep()
        assert_full_refund(mailing)
        assert len(mailing.lob.calls) == 1


async def test_unknown_cause_partial_refund_intent_uses_actual_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with mailed_case(tmp_path, monkeypatch, directed=True) as mailing:
        mailing.confirm_return(mailing_cost=800)
        mailing.clock.now += timedelta(days=120)
        mailing.sweep()
        cursor = mailing.store.payment_cursor(mailing.token)
        assert cursor is not None
        assert cursor.kind == "refund", (
            "There is no normal returned-mail refund intent; stopped-case compensation is separate"
        )
        assert cursor.amount_cents == mailing.case.amount_cents - 800
        # Supervisor amendment 2026-10-21: effect renamed / partial refunds supported
        # by T30 step 1 (ce596e4).
        assert mailing.case.payment_state is PaymentState.REFUNDED
        assert mailing.payments.refund_calls == 1
        refunds = [r for r in mailing.payments.refunds if r.status == "succeeded"]
        assert len(refunds) == 1
        assert refunds[0].amount_cents == mailing.case.amount_cents - 800
        events = [
            json.loads(row.event_json)
            for row in mailing.store.transition_history(mailing.token)
            if row.machine == "payment" and row.event_name == "RefundIssued"
        ]
        assert events == [
            {
                "refund_ref": refunds[0].refund_ref,
                "amount_cents": mailing.case.amount_cents - 800,
                "captured_cents": mailing.case.amount_cents,
            }
        ]
        assert_one_charge(mailing)


@pytest.mark.parametrize("corrected", [False, True])
async def test_agency_side_return_preserves_proof_packet_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corrected: bool
) -> None:
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        facts = mailing.confirm_return(
            cause=ReturnCause.AGENCY_SIDE, reason=UspsReturnReason.REFUSED
        )
        responsibility = classify_address_failure(mailing.original, facts)
        assert responsibility.responsibility is Responsibility.AGENCY_SIDE_EXISTING_REMEDY
        if corrected:
            mailing.correct_destination()
            mailing.approve_remail()
        mailing.sweep()
        expected = cs.CaseState.IN_TRANSIT if corrected else cs.CaseState.CLOSED_RETURNED
        assert mailing.case.letter_state is expected, "Agency-side proof remedy was never resolved"
        assert mailing.case.attempt == (2 if corrected else 1)
        assert mailing.payments.refund_calls == 0
        assert_one_charge(mailing)
        text = " ".join(customer_messages(mailing.real.status(mailing.case_id).model_dump()))
        assert "proof" in text.lower(), "The customer cannot obtain the proof-packet remedy"


async def test_lost_return_refund_response_reconciles_original_charge_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with mailed_case(tmp_path, monkeypatch, lost_refund_response=True) as mailing:
        mailing.confirm_return(
            finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE,
            evidence_ids=("wrong-verified-destination-proof",),
        )
        mailing.clock.now += timedelta(days=120)
        mailing.sweep(12)
        payments = mailing.payments
        assert isinstance(payments, LostRefundResponse)
        assert payments.lost_responses == 1, "No returned-mail refund reached the payment adapter"
        assert_full_refund(mailing)
        first_refund = next(i for i, call in enumerate(payments.calls) if call[0] == "refund")
        assert any(call[0] == "retrieve_refunds" for call in payments.calls[first_refund + 1 :])
        keys = [call[3] for call in payments.calls if call[0] == "refund"]
        assert keys
        assert len(set(keys)) == 1
        before = mailing.store.transition_history(mailing.token)
        mailing.sweep(3)
        assert_full_refund(mailing)
        assert mailing.store.transition_history(mailing.token) == before


async def test_second_return_closes_without_third_letter_or_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with mailed_case(tmp_path, monkeypatch) as mailing:
        mailing.confirm_return()
        correction = mailing.correct_destination()
        # This fixture deliberately starts at the existing first-remedy seam so
        # absence of automatic attempt 1 orchestration cannot mask the durable cap.
        mailing.store.apply_letter_event(
            mailing.token,
            cs.RemailRequested(),
            mailing.case.revision,
            occurred_at=mailing.clock(),
            new_effect_id=lambda: uuid4().hex,
        )
        first = mailing.store.get(mailing.token, 1)
        assert first is not None
        mailing.store.prepare(
            mailing.token,
            2,
            replace(
                first.request,
                recipient=correction.address,
                submission_token=uuid4().hex,
                pdf=redirected_pdf(mailing.real, mailing.case_id, correction.address),
            ),
            mailing.clock(),
        )
        seed_approval(mailing.store, mailing.token, 2)
        mailing.sweep(3)
        assert mailing.case.attempt == 2
        assert mailing.case.letter_state is cs.CaseState.IN_TRANSIT
        assert_one_charge(mailing)
        mailing.confirm_return(mailing_cost=0)
        with pytest.raises(IllegalTransition):
            mailing.store.apply_letter_event(
                mailing.token,
                cs.RemailRequested(),
                mailing.case.revision,
                occurred_at=mailing.clock(),
                new_effect_id=lambda: uuid4().hex,
            )
        mailing.sweep()
        final_case = mailing.store.get_case(mailing.token)
        assert final_case is not None
        assert final_case.letter_state is cs.CaseState.CLOSED_RETURNED, (
            "Second return stays unresolved instead of applying the exhausted-remail remedy"
        )
        assert_full_refund(mailing)
        assert len(mailing.lob.calls) == 2
        assert_one_charge(mailing)


@pytest.mark.parametrize("directed", [False, True])
async def test_no_corrected_verified_destination_never_resends_returned_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directed: bool
) -> None:
    async with mailed_case(tmp_path, monkeypatch, directed=directed) as mailing:
        mailing.confirm_return()
        mailing.sweep()
        assert len(mailing.lob.calls) == 1
        assert mailing.store.get(mailing.token, 2) is None
        assert_one_charge(mailing)


def customer_messages(value: object) -> Iterator[str]:
    """Read human copy, not raw machine states such as REFUNDED or CLOSED_RETURNED."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"message", "description", "summary", "title"} and isinstance(item, str):
                yield item
            else:
                yield from customer_messages(item)
    elif isinstance(value, list):
        for item in value:
            yield from customer_messages(item)


def reason_codes(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"reason", "reason_key", "reason_code"} and isinstance(item, str):
                yield item
            else:
                yield from reason_codes(item)
    elif isinstance(value, list):
        for item in value:
            yield from reason_codes(item)


@pytest.mark.parametrize("excluded", [False, True])
async def test_customer_visible_return_copy_describes_actual_remedy_without_false_no_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, excluded: bool
) -> None:
    async with mailed_case(tmp_path, monkeypatch, directed=excluded) as mailing:
        mailing.confirm_return(
            finding=AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE,
            evidence_ids=("destination-fault-proof",),
        )
        mailing.clock.now += timedelta(days=120)
        mailing.sweep()
        public = mailing.real.status(mailing.case_id).model_dump()
        text = " ".join(customer_messages(public)).lower()
        assert text, "Status has no customer-visible returned-mail remedy explanation"
        assert "return" in text
        assert "refund" in text
        if excluded:
            assert "address" in text
            assert any(word in text for word in ("no refund", "not refund"))
        else:
            assert "full" in text or "$29.99" in text
        assert "not charged" not in text
        assert "no charge" not in text
        stop = public.get("stop")
        assert not isinstance(stop, dict) or stop.get("no_charge") is not True
        for code in reason_codes(public):
            assert code
            assert all(char.isupper() or char.isdigit() or char in "_:." for char in code)
            assert mailing.original.address.line1 not in code
            assert mailing.original.owner not in code
            assert "alex@example.test" not in code
            assert "SYN-0001" not in code


async def test_post_freeze_customer_edit_cannot_rebind_consent_or_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with mailed_case(tmp_path, monkeypatch, directed=True) as mailing:
        before = mailing.store.destination_decision_history(mailing.case_id, mailing.original.owner)
        with pytest.raises(IntakeConflict, match="destination_locked_after_draft"):
            mailing.store.save_customer_address(
                mailing.case_id,
                mailing.original.owner,
                replace(mailing.original.address, line1="999 Later Customer Street"),
                mailing.clock(),
                1,
            )
        mailing.restart()
        assert (
            mailing.store.destination_decision_history(mailing.case_id, mailing.original.owner)
            == before
        )
        assert_one_charge(mailing)


async def test_lost_response_fixture_keeps_actual_refund_and_same_key_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive boundary control; does not stand in for automatic return recovery."""
    async with mailed_case(tmp_path, monkeypatch, lost_refund_response=True) as mailing:
        payments = mailing.payments
        with pytest.raises(TimeoutError, match="synthetic lost refund response"):
            payments.refund(
                mailing.case.authorization_ref,
                CHARGE,
                mailing.case.amount_cents,
                "key",
                captured_cents=mailing.case.amount_cents,
            )
        evidence = payments.retrieve_refunds(mailing.case.authorization_ref, CHARGE)
        assert evidence.full_refund
        replay = payments.refund(
            mailing.case.authorization_ref,
            CHARGE,
            mailing.case.amount_cents,
            "key",
            captured_cents=mailing.case.amount_cents,
        )
        assert replay.full_refund
        assert payments.refund_calls == 1
        assert_one_charge(mailing)


async def test_existing_stopped_case_compensation_is_a_working_distinct_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same service can refund an unexpected capture on an actual no-mail stop."""
    client, real, case_id = await drafting_case(tmp_path, monkeypatch)
    try:
        clock = Clock()
        clock.now += timedelta(days=1)
        payments = compensation_client()
        record = real._intake(case_id)
        assert record.case_token is not None
        case = real._store.get_case(record.case_token)
        assert case is not None
        # Reuse the established captured-object fake, with this authorization's
        # original identity. The expired review window legitimately requests release.
        payments.object = replace(
            payments.object,
            authorization_ref=case.authorization_ref,
            charge_ref=CHARGE,
            original_charge_ref=CHARGE,
        )
        object.__setattr__(real._deps, "clock", clock)
        object.__setattr__(real._deps, "payments", payments)
        lob = FakeLobClient()
        object.__setattr__(real._deps, "lob", lob)
        for _ in range(8):
            assert real.advance_due().failures == ()
            deps = real._deps
            real._store.close()
            real = CaseService(replace(deps, store=SqliteTransactionStore(tmp_path / "pb.sqlite")))
            cursor = real._store.payment_cursor(record.case_token)
            if cursor is not None and not cursor.done:
                clock.now = max(clock.now, cursor.next_at)
        assert real.status(case_id).state is cs.CaseState.STOPPED_NO_CHARGE
        assert real.status(case_id).payment_state is PaymentState.REFUNDED
        assert payments.refund_calls == 1
        assert payments.captures == payments.releases == 0
        assert not lob.calls
    finally:
        await client.aclose()
        real._store.close()
