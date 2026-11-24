"""Bounded scheduler dispatch through the existing durable coordinators."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from functools import partial

from parkingbreaker.case_state import (
    AuthorizationExpired,
    CaseState,
    Effect,
    Stopped,
    StopReason,
    SubmitRequested,
    WindowExpired,
)
from parkingbreaker.lob_submission import (
    TIMING_REFUSAL_REASON,
    CancelableLobClient,
    CancelOutcome,
    LetterRequest,
    LobResponse,
    MetadataLookup,
    pending_evidence,
)
from parkingbreaker.mail_attempt_store import PreparedMailAttempt
from parkingbreaker.payment_coordinator import run_payment_step
from parkingbreaker.payment_records import PaymentClient
from parkingbreaker.payment_state import EXPIRY_MARGIN, PaymentEffect, PaymentState
from parkingbreaker.session_window import WindowStatus, applies_to, window_status
from parkingbreaker.submission_coordinator import CreateGuard, run_submission_step
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import (
    RevisionConflict,
    SqliteTransactionStore,
    SubmissionPaused,
)

_AUTHORIZATION_UNUSABLE = StopReason.AUTHORIZATION_EXPIRED.value
_UNPAID_REMAIL = "ORIGINAL_PAYMENT_NOT_CAPTURED"


class _FirstMailHold(Enum):
    READY = auto()
    PENDING = auto()
    EXPIRED = auto()


def _first_mail_hold(
    case: CaseRecord, payments: PaymentClient, clock: Callable[[], datetime]
) -> _FirstMailHold:
    """Read only the original paid authorization; uncertainty never releases it."""
    if case.payment_state is PaymentState.NO_CHARGE:
        return _FirstMailHold.READY
    if clock() >= case.authorization_expires_at - EXPIRY_MARGIN:
        return _FirstMailHold.EXPIRED
    if not payments.authorization_evidence_required:
        # Offline adapters still use the time check, but have no Stripe object evidence.
        return _FirstMailHold.READY
    if case.payment_state is not PaymentState.AUTHORIZED:
        return _FirstMailHold.PENDING
    try:
        current = payments.retrieve(case.authorization_ref)
        if (
            current.authorization_ref != case.authorization_ref
            or current.amount_cents != case.amount_cents
            or current.classification != "verified_object"
            or current.evidence_version != 1
            or not current.full_object
        ):
            return _FirstMailHold.PENDING
        if current.status == "canceled":
            return _FirstMailHold.EXPIRED
        inspection = current.authorization_inspection
        if (
            inspection is None
            or inspection.authorization_ref != case.authorization_ref
            or inspection.expires_at != case.authorization_expires_at
        ):
            return _FirstMailHold.PENDING
        if clock() >= inspection.expires_at - EXPIRY_MARGIN:
            return _FirstMailHold.EXPIRED
        return _FirstMailHold.READY
    except Exception:
        return _FirstMailHold.PENDING


def free_remail_allowed(payment_state: PaymentState) -> bool:
    """The one free re-mail compensates paid work (H2, §20.19, 2026-10-22).

    Only a confirmed original capture pays for the free re-mail. Pending collection
    cannot authorize another postage purchase.
    """
    return payment_state is PaymentState.CAPTURED


@dataclass(frozen=True)
class _AuthorizationGuard:
    """Recheck time at the existing coordinator's actual first-attempt create seam."""

    client: CancelableLobClient
    # None only for a NO_CHARGE case: its stored window is a customer-work deadline, not a
    # Stripe authorization, so no financial expiry may be applied to it here.
    expires_at: datetime | None
    clock: Callable[[], datetime]
    potentially_sent: bool
    deadline_feasible: Callable[[datetime], bool] | None = None

    def create_letter(self, request: LetterRequest, idempotency_key: str) -> LobResponse:
        now = self.clock()
        if self.expires_at is not None and now >= self.expires_at - EXPIRY_MARGIN:
            # A committed earlier intent may have reached Lob. Preserve uncertainty
            # until the coordinator's metadata lookup; never claim that retry was refused.
            return LobResponse(
                None if self.potentially_sent else 422, error=_AUTHORIZATION_UNUSABLE
            )
        if self.deadline_feasible is not None and not self.deadline_feasible(now):
            return LobResponse(
                None if self.potentially_sent else 422,
                error=TIMING_REFUSAL_REASON,
                local_refusal=not self.potentially_sent,
            )
        return self.client.create_letter(request, idempotency_key)

    def find_by_metadata(self, submission_token: str) -> str | MetadataLookup | None:
        return self.client.find_by_metadata(submission_token)

    def lookup_letter(self, letter_id: str, submission_token: str) -> MetadataLookup:
        return self.client.lookup_letter(letter_id, submission_token)

    def cancel_letter(self, letter_id: str) -> CancelOutcome:
        return self.client.cancel_letter(letter_id)


@dataclass(frozen=True)
class _RemailGuard:
    """Attempt 2 spends no postage the original charge will not cover (H2).

    The attempt-1 authorization exemption must not let an unpaid re-mail through: a
    create is refused locally unless the original payment is captured.
    """

    client: CancelableLobClient
    payment_state: PaymentState

    def create_letter(self, request: LetterRequest, idempotency_key: str) -> LobResponse:
        if not free_remail_allowed(self.payment_state):
            return LobResponse(422, error=_UNPAID_REMAIL, local_refusal=True)
        return self.client.create_letter(request, idempotency_key)

    def find_by_metadata(self, submission_token: str) -> str | MetadataLookup | None:
        return self.client.find_by_metadata(submission_token)

    def lookup_letter(self, letter_id: str, submission_token: str) -> MetadataLookup:
        return self.client.lookup_letter(letter_id, submission_token)

    def cancel_letter(self, letter_id: str) -> CancelOutcome:
        return self.client.cancel_letter(letter_id)


def dispatch_once(
    store: SqliteTransactionStore,
    lob: CancelableLobClient,
    payments: PaymentClient,
    clock: Callable[[], datetime],
    new_id: Callable[[], str],
    *,
    case_id: str | None = None,
    deadline_feasible: Callable[[str, datetime], bool] | None = None,
    validate_prepared: Callable[[PreparedMailAttempt], None] | None = None,
    original_create_guard: CreateGuard | None = None,
) -> int:
    """One due submission and payment call per case, with crash-safe process ownership.

    Claimed outbox rows are recoverable only while holding the same database inode
    lock as both coordinators. Never steal a live worker's lock or create new keys.
    The next scheduler invocation continues any unfinished provider reconciliation.
    """
    processed = 0
    original_guards = () if original_create_guard is None else (original_create_guard,)
    try:
        with store.submission_ownership():
            for token in store.case_ids() if case_id is None else (case_id,):
                case = store.get_case(token)
                if case is None:
                    continue
                now = clock()
                # T74: hold activation takes this same submission mutex, so a hold read
                # here cannot become effective halfway through this case's provider work.
                hold = store.case_hold(token)
                held = hold is not None and hold.held
                if (
                    applies_to(case.letter_state)
                    and window_status(case.authorized_at, now) is WindowStatus.EXPIRED
                ):
                    case = store.apply_letter_event(
                        token,
                        WindowExpired(),
                        case.revision,
                        occurred_at=now,
                        new_effect_id=new_id,
                    )
                first_mail_hold = None
                if case.letter_state is CaseState.FROZEN:
                    expired = (
                        case.payment_state is not PaymentState.NO_CHARGE
                        and now >= case.authorization_expires_at - EXPIRY_MARGIN
                    )
                    event: SubmitRequested | AuthorizationExpired | Stopped | None
                    if expired:
                        event = AuthorizationExpired()
                    elif deadline_feasible is not None and not deadline_feasible(token, now):
                        event = Stopped(StopReason.NOT_FEASIBLE)
                    elif held:
                        # A pause is a scheduling result, never a rejection or a stop:
                        # the authorization-expiry and deadline refusals above still ran.
                        event = None
                    else:
                        first_mail_hold = _first_mail_hold(case, payments, clock)
                        now = clock()
                        if first_mail_hold is _FirstMailHold.EXPIRED:
                            event = AuthorizationExpired()
                        elif first_mail_hold is _FirstMailHold.READY:
                            event = SubmitRequested()
                        else:
                            # Keep the approved case, without even a new submit row.
                            event = None
                    if event is not None:
                        case = store.apply_letter_event(
                            token,
                            event,
                            case.revision,
                            occurred_at=now,
                            new_effect_id=new_id,
                        )
                legacy = store.submission_cursor(token, case.attempt)
                if (
                    case.letter_state is CaseState.IN_TRANSIT
                    and legacy is not None
                    and pending_evidence(legacy.outcome)
                    and now >= legacy.next_at
                ):
                    run_submission_step(
                        store,
                        lob,
                        token,
                        case.attempt,
                        expected_case_revision=case.revision,
                        expected_cursor_revision=legacy.revision,
                        operation_id=new_id(),
                        clock=clock,
                        new_effect_id=new_id,
                        already_owned=True,
                        validate_prepared=validate_prepared,
                        create_guards=original_guards,
                    )
                    processed += 1
                    case = store.get_case(token)
                    assert case is not None
                for row in store.pending_effects(
                    token, kinds=(Effect.SUBMIT_LETTER, Effect.SUBMIT_REMAIL)
                ):
                    if row.effect not in {Effect.SUBMIT_LETTER, Effect.SUBMIT_REMAIL}:
                        continue
                    attempt = 1 if row.effect is Effect.SUBMIT_LETTER else 2
                    cursor = store.submission_cursor(token, attempt)
                    if cursor is not None and not pending_evidence(cursor.outcome):
                        if row.claimed_at is None:
                            store.claim_effect(row.effect_id, now)
                        store.complete_effect(row.effect_id, "resolved", now)
                        processed += 1
                        continue
                    if cursor is None and held:
                        # T74: a held attempt gets no new durable create intent, even when
                        # a submit effect was already queued. Leave it unclaimed and paused;
                        # this skips only the create, never the lookup/payment work below.
                        continue
                    if cursor is not None and now < cursor.next_at:
                        continue
                    if (
                        attempt == 2
                        and not free_remail_allowed(case.payment_state)
                        and case.payment_state
                        not in {
                            PaymentState.RELEASED,
                            PaymentState.WRITTEN_OFF,
                            PaymentState.REFUNDED,
                        }
                    ):
                        # Reconcile the original capture below before creating a new
                        # re-mail intent. A restart preserves this wait (H2).
                        continue
                    if attempt == 1 and cursor is None:
                        # A restart may leave a queued first send without a Lob cursor.
                        # Reuse only this sweep's read. A possible previous send still
                        # takes the original reconciliation path, never this preflight.
                        if first_mail_hold is None:
                            first_mail_hold = _first_mail_hold(case, payments, clock)
                        if first_mail_hold is _FirstMailHold.PENDING:
                            continue
                        now = clock()
                        if first_mail_hold is _FirstMailHold.EXPIRED:
                            # Do not manufacture a possible-send cursor for a local
                            # stop. Atomically close the old outbox row with the stop.
                            case = store.stop_unsent_expired_authorization(
                                token, case.revision, row.effect_id, now=now, new_effect_id=new_id
                            )
                            processed += 1
                            continue
                    if row.claimed_at is None:
                        store.claim_effect(row.effect_id, now)
                    try:
                        create_guard: CreateGuard = (
                            partial(
                                _AuthorizationGuard,
                                expires_at=(
                                    None
                                    if case.payment_state is PaymentState.NO_CHARGE
                                    else case.authorization_expires_at
                                ),
                                clock=clock,
                                potentially_sent=cursor is not None,
                                deadline_feasible=(
                                    None
                                    if deadline_feasible is None
                                    else partial(deadline_feasible, token)
                                ),
                            )
                            if attempt == 1
                            else partial(_RemailGuard, payment_state=case.payment_state)
                        )
                        cursor = run_submission_step(
                            store,
                            lob,
                            token,
                            attempt,
                            expected_case_revision=case.revision,
                            expected_cursor_revision=None if cursor is None else cursor.revision,
                            operation_id=new_id(),
                            clock=clock,
                            new_effect_id=new_id,
                            already_owned=True,
                            validate_prepared=validate_prepared,
                            create_guards=(*original_guards, create_guard),
                        )
                    except SubmissionPaused:
                        # A held create intent is scheduling inactivity, not a rejection,
                        # timeout, stop or completed effect. The pre-gate above leaves the
                        # create effect unclaimed; lookup/payment work still runs.
                        continue
                    processed += 1
                    if not pending_evidence(cursor.outcome):
                        store.complete_effect(row.effect_id, "resolved", clock())
                case = store.get_case(token)
                assert case is not None
                payment = store.payment_cursor(token)
                money_due = (
                    next(
                        store._iter_pending_effects(
                            token,
                            kinds=(
                                PaymentEffect.CAPTURE_PAYMENT,
                                PaymentEffect.RELEASE_HOLD,
                                PaymentEffect.REFUND_FULL,
                                PaymentEffect.REFUND_RETURNED_MAIL,
                            ),
                            limit=1,
                        ),
                        None,
                    )
                    is not None
                ) or (payment is not None and not payment.done)
                submission = store.submission_cursor(token, 1)
                # TODO(T05): When evidence is overdue, schedule a lookup of the original attempt
                # through its coordinator. Persist backoff and cap attempts and spend. Escalate
                # before the hold or filing cutoff. Park disputed actions while read-only checks
                # continue, so a late receipt can still resolve what happened to the original.
                if submission is not None and pending_evidence(submission.outcome):
                    # Legacy accepted cursors must obtain evidence before any capture.
                    money_due = False
                if money_due and (payment is None or clock() >= payment.next_at):
                    run_payment_step(
                        store,
                        payments,
                        token,
                        expected_case_revision=case.revision,
                        expected_cursor_revision=None if payment is None else payment.revision,
                        operation_id=new_id(),
                        key=f"pb-payment-{token}" if payment is None else payment.key,
                        clock=clock,
                        retry_effect_id=new_id(),
                        new_effect_id=new_id,
                        already_owned=True,
                    )
                    processed += 1
                for row in store.pending_effects(
                    token, kinds=(PaymentEffect.LOG_WRITE_OFF, PaymentEffect.LOG_COMPENSATION)
                ):
                    if row.effect in {PaymentEffect.LOG_WRITE_OFF, PaymentEffect.LOG_COMPENSATION}:
                        if row.claimed_at is None:
                            store.claim_effect(row.effect_id, clock())
                        store.complete_effect(row.effect_id, "recorded_in_outbox", clock())
                        processed += 1
    except RevisionConflict:
        # Another live coordinator owns the database; scheduler retries later.
        return processed
    return processed
