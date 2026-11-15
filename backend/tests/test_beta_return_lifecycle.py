"""Selected free-beta returned mail closes without waiting for a payment that never exists.

Real SqliteTransactionStore, real CaseService and the existing fake Lob seam. Payment is a
dependency that fails on any touch: a beta case has no authorization, capture, refund or
re-mail to reconcile. Return evidence is the existing application fixture seam, not an
asserted Lob payload contract.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from mail_samples import make_request, seed_approval, seed_destination
from parkingbreaker import case_state as cs
from parkingbreaker.address_policy import (
    AddressFaultFinding,
    FailureFacts,
    Responsibility,
    ResponsibilityDecision,
    address_fingerprint,
)
from parkingbreaker.agencies import IssuerType, ResolvedAgency
from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import SweepResult
from parkingbreaker.beta_store import BETA_DISCLOSURE_SHA256
from parkingbreaker.case_service import CaseService
from parkingbreaker.case_status import CaseStatus
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import (
    DEFAULT_RETRY_POLICY,
    VERIFIED_IDEMPOTENCY_WINDOW,
    Accepted,
    LetterEvidence,
    LobResponse,
    Uncertain,
)
from parkingbreaker.payment_records import PaymentClient
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.research_records import agency_json
from parkingbreaker.returned_mail import ReturnCause, UspsReturnReason
from parkingbreaker.submission_coordinator import run_submission_step
from parkingbreaker.submission_records import outcome_json
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_beta_admission import NOW, campaign
from test_case_service import service

ORIGINAL_LETTER = "ltr_BetaOriginal"
ISSUER = "CA:issuer-1.gov"


def _offer_json() -> str:
    """The stored pre-hold offer the customer accepted to start the selected appeal."""
    return s.Offer(
        case_id="case_1",
        issuer=s.ResolvedIssuer(
            issuer_id=ISSUER,
            issuer_name="Fixture issuer 1",
            deciding_body="Parking review",
            state="CA",
            stage="initial",
            service_basis="municipal",
            evidence_ref="fixture-evidence",
        ),
        tiers=[
            s.TierOffer(
                tier=s.MailTier.CERTIFIED,
                amount_cents=2999,
                description="Certified mail",
            )
        ],
    ).model_dump_json()


def _candidate(store: SqliteTransactionStore) -> None:
    """Trusted qualification fixture; the offer is the real stored pre-hold shape."""
    agency = ResolvedAgency(
        ISSUER,
        "Fixture issuer 1",
        "CA",
        ("issuer-1.gov",),
        IssuerType.MUNICIPAL,
        "Parking review",
        "CA",
    )
    store.create_owned_workspace("owner-1", "ws_1", Workspace(), NOW)
    store.record_intake(
        "case_1",
        "ws_1",
        json.dumps({"fixture": 1}),
        _offer_json(),
        NOW,
        agency_json=agency_json(agency),
    )
    digest = hashlib.sha256(b"qualification-1").hexdigest()
    store.bind_beta_jurisdiction("beta-1", ISSUER, "jurisdiction-1", digest, NOW)
    store.qualify_beta_candidate("beta-1", "case_1", digest, NOW + timedelta(days=1), NOW)


class FrozenClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class ForbiddenPayments:
    """Any payment touch proves the closure invented a financial remedy."""

    def __getattribute__(self, name: str) -> object:
        raise AssertionError("payment dependency touched: " + name)


@dataclass
class BetaMailing:
    real: CaseService
    path: Path
    clock: FrozenClock
    token: str
    lob: FakeLobClient

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

    def sweep(self) -> SweepResult:
        return self.real.advance_due()

    def confirm_return(
        self,
        *,
        cause: ReturnCause = ReturnCause.UNKNOWN,
        reason: UspsReturnReason = UspsReturnReason.OTHER,
        finding: AddressFaultFinding = AddressFaultFinding.NOT_ESTABLISHED,
        event_id: str | None = None,
    ) -> bool:
        frozen = self.store.get(self.token, 1)
        assert frozen is not None
        facts = FailureFacts(
            "case_1",
            return_cause=cause,
            mailed_address_fingerprint=address_fingerprint(frozen.request.recipient),
            address_finding=finding,
        )
        payload = json.dumps(
            {
                "event": "letter.returned_to_sender",
                "occurred_at": self.clock().isoformat(),
                "tracking_number": None,
                "letter_id": self.lob.letters[frozen.request.submission_token],
                "attempt": 1,
                "usps_reason": reason.value,
                "failure_facts": asdict(facts),
                "mailing_cost_cents": 0,
            },
            sort_keys=True,
        )
        accepted = self.store.record_webhook(
            "lob",
            event_id or f"return-{self.token}",
            self.token,
            payload,
            letter_event=cs.ReturnConfirmed(reason.value),
            occurred_at=self.clock(),
            new_effect_id=lambda: uuid4().hex,
        )
        assert self.case.letter_state is cs.CaseState.RETURNED
        self.restart()
        return accepted


def _post_events(
    store: SqliteTransactionStore, case: CaseRecord, events: tuple[object, ...]
) -> CaseRecord:
    for event in events:
        case = store.apply_letter_event(
            case.case_token,
            cast(cs.Event, event),
            case.revision,
            occurred_at=NOW,
            new_effect_id=lambda: uuid4().hex,
        )
    return case


def beta_prepared_case(tmp_path: Path, lob: FakeLobClient) -> BetaMailing:
    """A selected NO_CHARGE case with its approved original request frozen, pre-submission.

    `lob` is installed as the service's own mail seam, so the exact same client observes
    the submission step and every later sweep.
    """
    clock = FrozenClock()
    path = tmp_path / "pb.sqlite"
    real = service(path, payments=cast(PaymentClient, ForbiddenPayments()), clock=clock)
    store = real._store
    campaign(store)
    _candidate(store)
    store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW)
    case = store.start_beta_case("case_1", BETA_DISCLOSURE_SHA256, NOW)
    assert (case.amount_cents, case.payment_state) == (0, PaymentState.NO_CHARGE)
    seed_destination(store, case, NOW)
    case = _post_events(
        store,
        case,
        (
            cs.ResearchComplete(),
            cs.DraftReady(),
            cs.CustomerApproved(),
            cs.SubmitRequested(),
        ),
    )
    assert case.letter_state is cs.CaseState.SUBMITTING
    store.prepare(case.case_token, 1, make_request(token="beta-approved-submission"), NOW)
    seed_approval(store, case.case_token)
    object.__setattr__(real._deps, "lob", lob)
    return BetaMailing(real, path, clock, case.case_token, lob)


def beta_returned_case(tmp_path: Path) -> BetaMailing:
    """A selected NO_CHARGE case whose accepted original letter was confirmed returned."""
    lob = FakeLobClient([LobResponse(200, ORIGINAL_LETTER, evidence=LetterEvidence.PRODUCTION)])
    mailing = beta_prepared_case(tmp_path, lob)
    case = mailing.case
    run_submission_step(
        mailing.store,
        lob,
        mailing.token,
        1,
        expected_case_revision=case.revision,
        expected_cursor_revision=None,
        operation_id="beta-submission-1",
        clock=mailing.clock,
    )
    reloaded = mailing.store.get_case(mailing.token)
    assert reloaded is not None
    assert reloaded.letter_state is cs.CaseState.IN_TRANSIT
    assert reloaded.payment_state is PaymentState.NO_CHARGE
    return mailing


def assert_no_money_or_second_submission(mailing: BetaMailing) -> None:
    # The active service client is the same fake that observed the submission, so its
    # create-call list covers every sweep too. Lookups are not create operations.
    assert mailing.real._deps.lob is mailing.lob
    assert len(mailing.lob.calls) == 1
    assert len({call[0] for call in mailing.lob.calls}) == 1
    assert [row for row in mailing.store.effects(mailing.token) if row.machine == "payment"] == []
    assert mailing.store.payment_cursor(mailing.token) is None
    assert mailing.store.returned_mail_refund(mailing.token) is None
    assert all(
        row.effect is not cs.Effect.SUBMIT_REMAIL for row in mailing.store.effects(mailing.token)
    )
    assert mailing.store.get(mailing.token, 2) is None
    assert mailing.case.attempt == 1


def assert_one_closure(mailing: BetaMailing) -> None:
    rows = (
        mailing.store._require_open()
        .execute("SELECT COUNT(*) FROM pb_return_remedies WHERE case_token=?", (mailing.token,))
        .fetchone()
    )
    assert rows == (1,)


@pytest.mark.parametrize(
    ("cause", "reason", "finding", "responsibility"),
    [
        (
            ReturnCause.UNKNOWN,
            UspsReturnReason.OTHER,
            AddressFaultFinding.NOT_ESTABLISHED,
            Responsibility.UNKNOWN_CAUSE_EXISTING_REMEDY,
        ),
        (
            ReturnCause.AGENCY_SIDE,
            UspsReturnReason.REFUSED,
            AddressFaultFinding.NOT_ESTABLISHED,
            Responsibility.AGENCY_SIDE_EXISTING_REMEDY,
        ),
        (
            ReturnCause.UNKNOWN,
            UspsReturnReason.NO_SUCH_NUMBER,
            AddressFaultFinding.DESTINATION_WRONG_SOLE_CAUSE,
            Responsibility.OUR_ERROR_FULL_REFUND,
        ),
    ],
    ids=["unknown", "agency_side", "system_verified_wrong"],
)
def test_beta_return_closes_once_without_payment(
    tmp_path: Path,
    cause: ReturnCause,
    reason: UspsReturnReason,
    finding: AddressFaultFinding,
    responsibility: Responsibility,
) -> None:
    mailing = beta_returned_case(tmp_path)
    try:
        assert mailing.confirm_return(cause=cause, reason=reason, finding=finding)
        # Duplicate deliveries stay deduplicated and never close twice.
        assert not mailing.confirm_return(cause=cause, reason=reason, finding=finding)

        # Baseline: this sweep leaves the case RETURNED and reports waiting for a payment
        # that can never exist, so the free-beta case is stranded forever.
        result = mailing.sweep()
        assert result.failures == ()
        assert mailing.case.letter_state is cs.CaseState.CLOSED_RETURNED
        assert mailing.case.case_status is CaseStatus.DONE
        assert_no_money_or_second_submission(mailing)

        closed = mailing.real.status("case_1")
        remedy = closed.return_remedy
        assert remedy is not None
        assert remedy.attempt == 1
        assert remedy.kind == "BETA_RETURN_CLOSED"
        assert remedy.reason_code == "BETA_RETURN_CLOSED"
        assert remedy.refund_cents == 0
        assert remedy.refund_status is None
        assert remedy.remail is False
        assert "free beta" in remedy.message
        assert "no charge" in remedy.message
        assert "payment" not in remedy.message
        assert "wrong" not in remedy.message.lower()

        recorded = mailing.store.returned_mail_resolution(mailing.token, 1)
        assert recorded is not None
        assert recorded.responsibility is responsibility
        assert recorded.remedy is None
        assert recorded.attempt == 1

        # The original mailing proof stays with the case for the customer and owner.
        proof = mailing.real.mailing_record("case_1")
        assert proof.letter_id == ORIGINAL_LETTER
        assert proof.pdf_base64
        agency_side = responsibility is Responsibility.AGENCY_SIDE_EXISTING_REMEDY
        assert (remedy.proof_packet is not None) is agency_side

        # Repeated sweeps and a store reopen resolve nothing more.
        resolved_at = recorded.resolved_at
        for _ in range(3):
            assert mailing.sweep().failures == ()
            mailing.restart()
        assert mailing.case.letter_state is cs.CaseState.CLOSED_RETURNED
        again = mailing.store.returned_mail_resolution(mailing.token, 1)
        assert again is not None
        assert again.resolved_at == resolved_at
        assert_one_closure(mailing)
        assert_no_money_or_second_submission(mailing)
    finally:
        mailing.store.close()


def test_customer_directed_exclusion_is_never_a_beta_address_fault(tmp_path: Path) -> None:
    """A recorded customer exclusion keeps its factual row but never its address-fault copy."""
    mailing = beta_returned_case(tmp_path)
    try:
        assert mailing.confirm_return(cause=ReturnCause.UNKNOWN)
        case = mailing.case
        mailing.store.resolve_returned_mail(
            mailing.token,
            case.revision,
            ResponsibilityDecision(
                Responsibility.CUSTOMER_DIRECTED_ADDRESS_EXCLUSION,
                "SUPPLIED_ADDRESS_SOLE_CAUSE",
                None,
            ),
            None,
            None,
            now=mailing.clock(),
        )
        remedy = mailing.real.status("case_1").return_remedy
        assert remedy is not None
        assert remedy.kind == "BETA_RETURN_CLOSED"
        assert "wrong" not in remedy.message.lower()
        assert "address" not in remedy.message.lower()
        assert mailing.store.returned_mail_resolution(mailing.token, 1) is not None
        assert_no_money_or_second_submission(mailing)
    finally:
        mailing.store.close()


def test_unconfirmed_submission_refuses_beta_closure(tmp_path: Path) -> None:
    """Invariant only: an unconfirmed original provider answer can never be closed over.

    This deliberately rewrites the stored cursor/operation pair to Uncertain; it is not
    provider reconciliation proof. The recovery proof lives in the lost-answer test below.
    """
    mailing = beta_returned_case(tmp_path)
    try:
        assert mailing.confirm_return(cause=ReturnCause.UNKNOWN)
        db = mailing.store._require_open()
        operation_row = db.execute(
            "SELECT rowid FROM pb_submission_operations "
            "WHERE case_token=? AND attempt=1 ORDER BY rowid DESC LIMIT 1",
            (mailing.token,),
        ).fetchone()
        assert operation_row is not None
        unresolved = outcome_json(Uncertain("synthetic unconfirmed provider answer"))
        db.execute(
            "UPDATE pb_submission_cursors SET owner=NULL, outcome_json=? "
            "WHERE case_token=? AND attempt=1",
            (unresolved, mailing.token),
        )
        db.execute(
            "UPDATE pb_submission_operations SET outcome_json=? WHERE rowid=?",
            (unresolved, operation_row[0]),
        )
        result = mailing.sweep()
        assert [failure.reason for failure in result.failures] == ["returned_mailing_not_confirmed"]
        assert mailing.case.letter_state is cs.CaseState.RETURNED
        pending = mailing.real.status("case_1").return_remedy
        assert pending is not None
        assert pending.kind == "RETURN_PENDING"
        assert "payment" not in pending.message
        assert mailing.store.returned_mail_resolution(mailing.token, 1) is None
        assert_no_money_or_second_submission(mailing)
    finally:
        mailing.store.close()


@pytest.mark.parametrize("lost_response", [False, True], ids=["late_production", "lost_response"])
def test_uncertain_original_recovers_by_lookup_before_return_closure(
    tmp_path: Path,
    lost_response: bool,
) -> None:
    """Late evidence and a lost create reply both recover the one original before closure."""
    submission_token = "beta-approved-submission"
    lob = FakeLobClient(
        [
            LobResponse(None, error="synthetic lost create response")
            if lost_response
            else LobResponse(200, ORIGINAL_LETTER, evidence=LetterEvidence.RENDERED)
        ]
    )
    mailing = beta_prepared_case(tmp_path, lob)
    try:
        case = mailing.case
        run_submission_step(
            mailing.store,
            lob,
            mailing.token,
            1,
            expected_case_revision=case.revision,
            expected_cursor_revision=None,
            operation_id="beta-submission-1",
            clock=mailing.clock,
        )
        prepared = mailing.store.get(mailing.token, 1)
        assert prepared is not None
        if lost_response:
            # Provider-side inventory survived even though no response or letter id
            # reached the application. Only a subsequent metadata lookup can find it.
            lob.letters[submission_token] = ORIGINAL_LETTER
            lob.by_key[prepared.key] = ORIGINAL_LETTER
            lob.evidence_by_token[submission_token] = LetterEvidence.RENDERED
        expected_known_id = None if lost_response else ORIGINAL_LETTER
        # Neither initial answer proves production: one key, one original, unresolved.
        assert mailing.case.letter_state is cs.CaseState.SUBMITTING
        first = mailing.store.submission_cursor(mailing.token, 1)
        assert first is not None
        assert isinstance(first.outcome, Uncertain)
        assert first.outcome.letter_id == expected_known_id
        assert [call[0] for call in lob.calls] == [prepared.key]
        assert set(lob.letters.values()) == {ORIGINAL_LETTER}
        assert_no_money_or_second_submission(mailing)

        # A restart before any matching provider evidence keeps the same unresolved
        # answer: no closure, no second submission, no payment.
        mailing.restart()
        assert mailing.case.letter_state is cs.CaseState.SUBMITTING
        still = mailing.store.submission_cursor(mailing.token, 1)
        assert still is not None
        assert isinstance(still.outcome, Uncertain)
        assert still.outcome.letter_id == expected_known_id
        assert len(lob.calls) == 1
        assert_no_money_or_second_submission(mailing)

        # The provider later confirms production evidence for that same letter identity.
        lob.evidence_by_token[submission_token] = LetterEvidence.PRODUCTION
        if lost_response:
            # The first response was lost, so its normal same-key retry stays
            # available; reconcile by metadata only at the existing idempotency cutoff,
            # where create is no longer allowed.
            mailing.clock.now = (
                first.first_sent_at
                + VERIFIED_IDEMPOTENCY_WINDOW
                - (DEFAULT_RETRY_POLICY.lookup_margin)
            )
        else:
            mailing.clock.now += timedelta(minutes=5)
        assert mailing.sweep().failures == ()
        accepted_case = mailing.store.get_case(mailing.token)
        assert accepted_case is not None
        assert accepted_case.letter_state is cs.CaseState.IN_TRANSIT
        assert accepted_case.payment_state is PaymentState.NO_CHARGE
        accepted = mailing.store.submission_cursor(mailing.token, 1)
        assert accepted is not None
        assert isinstance(accepted.outcome, Accepted)
        assert accepted.outcome.letter_id == ORIGINAL_LETTER
        assert len(lob.calls) == 1
        assert lob.cancels == []
        if lost_response:
            assert submission_token in lob.lookups

        # The confirmed return then closes exactly once through the production webhook seam.
        assert mailing.confirm_return(cause=ReturnCause.UNKNOWN)
        assert mailing.sweep().failures == ()
        closed = mailing.store.get_case(mailing.token)
        assert closed is not None
        assert closed.letter_state is cs.CaseState.CLOSED_RETURNED
        remedy = mailing.real.status("case_1").return_remedy
        assert remedy is not None
        assert remedy.kind == "BETA_RETURN_CLOSED"
        assert remedy.attempt == 1
        assert_one_closure(mailing)
        assert_no_money_or_second_submission(mailing)
        assert mailing.sweep().failures == ()
        assert_one_closure(mailing)
        assert_no_money_or_second_submission(mailing)
    finally:
        mailing.store.close()
