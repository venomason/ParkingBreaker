"""Bounded durable payment dispatch; one logical original-object operation per step (v6 note 20)."""

from collections.abc import Callable
from contextlib import nullcontext
from datetime import datetime

from parkingbreaker import prices
from parkingbreaker.payment_records import (
    PaymentClient,
    PaymentCursor,
    PaymentOutcome,
    outcome_json,
)
from parkingbreaker.payment_state import EXPIRY_MARGIN, PaymentEffect, PaymentState
from parkingbreaker.transaction_store import RevisionConflict, SqliteTransactionStore


def run_payment_step(
    store: SqliteTransactionStore,
    client: PaymentClient,
    case_id: str,
    *,
    expected_case_revision: int,
    expected_cursor_revision: int | None,
    operation_id: str,
    key: str,
    clock: Callable[[], datetime],
    new_effect_id: Callable[[], str] | None = None,
    already_owned: bool = False,
    retry_effect_id: str = "",
    writeoff_effect_ids: tuple[str, ...] = (),
    compensation_effect_ids: tuple[str, ...] = (),
) -> PaymentCursor:
    """Intents commit before calls. Refund retries reuse their key only inside its replay window.

    No replacement PaymentIntent/charge, SDK, messages, letter calls or implicit clock.
    A hung client owns the existing process lock until termination.
    """
    if already_owned and not store._submission_locked:
        raise RevisionConflict("exclusive ownership required")
    with nullcontext() if already_owned else store.submission_ownership():
        previous = store.payment_cursor(case_id)
        case = store.get_case(case_id)
        if case is not None and case.payment_state is PaymentState.NO_CHARGE:
            raise ValueError("no-charge case cannot dispatch payment")
        if (
            case is None
            or case.revision != expected_case_revision
            or (None if previous is None else previous.revision) != expected_cursor_revision
        ):
            raise RevisionConflict("stale payment/case revision")
        if (
            previous is not None
            and previous.done
            and next(
                store._iter_pending_effects(
                    case_id, kinds=(PaymentEffect.REFUND_RETURNED_MAIL,), limit=1
                ),
                None,
            )
            is None
        ):
            if key != previous.key:
                raise ValueError("immutable payment key changed")
            return previous
        claimed = store.claim_payment(
            case_id,
            expected_case_revision,
            expected_cursor_revision,
            now=clock(),
            operation_id=operation_id,
            key=key,
            retry_effect_id=retry_effect_id,
            authorization_evidence_required=client.authorization_evidence_required,
        )
        call_at = clock()
        if call_at.tzinfo is None or call_at < claimed.next_at:
            raise ValueError("invalid caller call time")
        try:
            if claimed.call_kind == "capture" and (
                call_at >= case.authorization_expires_at - EXPIRY_MARGIN
                or (
                    claimed.contract_version == 3
                    and (
                        previous is None
                        or previous.outcome.authorization_inspection is None
                        or previous.outcome.authorization_inspection.expires_at
                        != case.authorization_expires_at
                    )
                )
            ):
                outcome = PaymentOutcome("uncertain", claimed.authorization_ref, case.amount_cents)
            elif claimed.call_kind == "retrieve":
                if claimed.kind == "refund":
                    assert claimed.authorization_charge_ref is not None
                    outcome = client.retrieve_refunds(
                        claimed.authorization_ref, claimed.authorization_charge_ref
                    )
                else:
                    outcome = client.retrieve(claimed.authorization_ref)
                if (
                    claimed.contract_version == 3
                    and claimed.kind == "capture"
                    and outcome.status == "requires_capture"
                    and (
                        outcome.authorization_inspection is None
                        or outcome.authorization_inspection.expires_at
                        != case.authorization_expires_at
                    )
                ):
                    outcome = PaymentOutcome(
                        "uncertain",
                        claimed.authorization_ref,
                        case.amount_cents,
                        evidence_version=1,
                        classification="expiry_mismatch",
                        original_charge_ref=outcome.original_charge_ref,
                        capture_before=outcome.capture_before,
                    )
            elif claimed.call_kind == "capture":
                assert claimed.mutation_key is not None
                outcome = client.capture(
                    claimed.authorization_ref,
                    claimed.amount_cents,
                    claimed.mutation_key,
                    final_capture=claimed.final_capture is True,
                )
            elif claimed.call_kind == "refund":
                assert claimed.mutation_key is not None
                assert claimed.authorization_charge_ref is not None
                replay = previous is not None and claimed.mutation_key == previous.mutation_key
                if replay and not store.refund_replay_valid(previous, claimed, now=clock()):
                    outcome = PaymentOutcome(
                        "uncertain", claimed.authorization_ref, case.amount_cents
                    )
                else:
                    outcome = client.refund(
                        claimed.authorization_ref,
                        claimed.authorization_charge_ref,
                        claimed.amount_cents,
                        claimed.mutation_key,
                        captured_cents=case.amount_cents,
                    )
            else:
                assert claimed.mutation_key is not None
                outcome = client.release(claimed.authorization_ref, claimed.mutation_key)
        except Exception:
            outcome = PaymentOutcome("uncertain", claimed.authorization_ref, case.amount_cents)
        # Malformed evidence keeps the existing rejection behavior. A valid provider
        # object for a different allowed price is still unverified for this case.
        outcome_json(outcome)
        if (
            case.amount_cents not in prices.ALLOWED_PRICE_CENTS
            or outcome.amount_cents != case.amount_cents
        ):
            outcome = PaymentOutcome("uncertain", claimed.authorization_ref, case.amount_cents)
        if (
            claimed.contract_version == 3
            and claimed.authorization_charge_ref is not None
            and outcome.original_charge_ref is not None
            and outcome.original_charge_ref != claimed.authorization_charge_ref
        ):
            outcome = PaymentOutcome(
                "uncertain",
                claimed.authorization_ref,
                case.amount_cents,
                evidence_version=1,
                classification="charge_mismatch",
            )
        return store.finish_payment(
            claimed,
            outcome,
            now=clock(),
            writeoff_effect_ids=writeoff_effect_ids,
            compensation_effect_ids=compensation_effect_ids,
            new_effect_id=new_effect_id,
        )
