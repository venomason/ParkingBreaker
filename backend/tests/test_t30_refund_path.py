"""Returned-mail money mechanics only; responsibility/orchestration belongs to step 2."""

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.effect_dispatch import dispatch_once
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.payment_coordinator import run_payment_step
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.payment_records import (
    PaymentCursor,
    PaymentOutcome,
    RefundEvidence,
    mutation_key,
)
from parkingbreaker.prices import CERTIFIED_CENTS
from parkingbreaker.returned_mail import RemedyKind
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_stripe_payment import CHARGE, REF, Transport, client, fixture, refund_fixture
from test_transaction_lifecycle import NOW, letter


def step(
    store: SqliteTransactionStore,
    payments: FakePaymentClient,
    case_id: str,
    at: datetime | None = None,
) -> PaymentCursor:
    case = store.get_case(case_id)
    assert case is not None
    cursor = store.payment_cursor(case_id)
    return run_payment_step(
        store,
        payments,
        case_id,
        expected_case_revision=case.revision,
        expected_cursor_revision=None if cursor is None else cursor.revision,
        operation_id=f"op-{0 if cursor is None else cursor.revision}",
        key="original-payment" if cursor is None else cursor.key,
        clock=lambda: at or (NOW if cursor is None else cursor.next_at),
    )


def returned(store: SqliteTransactionStore, payments: FakePaymentClient) -> CaseRecord:
    store.save_workspace("workspace", Workspace(), NOW)
    case = store.start_case("workspace", REF, NOW, NOW + timedelta(days=5))
    for event in (
        cs.ResearchComplete(),
        cs.DraftReady(),
        cs.CustomerApproved(),
        cs.SubmitRequested(),
        cs.SubmitConfirmed("lob-1"),
    ):
        case = letter(store, case, event)
    payments.object = client(Transport(fixture())).retrieve(REF)
    payments.responses = [client(Transport(fixture("succeeded"))).retrieve(REF)]
    for effect in store.pending_effects(case.case_token):
        if effect.effect is cs.Effect.SUBMIT_LETTER:
            store.claim_effect(effect.effect_id, NOW)
            store.complete_effect(effect.effect_id, "submitted", NOW)
    step(store, payments, case.case_token)
    assert step(store, payments, case.case_token).done
    captured = store.get_case(case.case_token)
    assert captured is not None
    return letter(store, captured, cs.ReturnConfirmed("address"))


@pytest.mark.parametrize(
    ("kind", "amount"),
    [
        (RemedyKind.FULL_REFUND, 2999),
        (RemedyKind.PARTIAL_REFUND, 2617),
        (RemedyKind.PARTIAL_REFUND, 2999),
        (RemedyKind.PARTIAL_REFUND, 1),
    ],
)
def test_refund_exactly_once_and_restart(tmp_path: Path, kind: RemedyKind, amount: int) -> None:
    path = tmp_path / "cases.sqlite"
    payments = FakePaymentClient(REF)
    with SqliteTransactionStore(path) as store:
        case = returned(store, payments)
        before_calls = list(payments.calls)
        requested = store.request_returned_mail_refund(case.case_token, kind, amount, now=NOW)
        assert payments.calls == before_calls
        assert requested.payment_state is ps.PaymentState.CAPTURED
        assert (
            store.request_returned_mail_refund(case.case_token, kind, amount, now=NOW) == requested
        )
    with SqliteTransactionStore(path) as store:
        cursor = step(store, payments, case.case_token)
        assert cursor.kind == "refund"
        assert cursor.call_kind == "retrieve"
        assert cursor.amount_cents == amount
        assert not cursor.done
        cursor = step(store, payments, case.case_token)
        assert cursor.call_kind == "refund"
        assert not cursor.done
        assert cursor.mutation_key == mutation_key("returned-mail", case.case_token, kind, "1")
        cursor = step(store, payments, case.case_token)
        assert cursor.done
        saved = store.get_case(case.case_token)
        assert saved is not None
        assert saved.payment_state is ps.PaymentState.REFUNDED
        issued = [
            r for r in store.transition_history(case.case_token) if r.event_name == "RefundIssued"
        ]
        assert len(issued) == 1
        assert f'"amount_cents":{amount}' in issued[0].event_json
        assert '"captured_cents":2999' in issued[0].event_json
        assert store.request_returned_mail_refund(case.case_token, kind, amount, now=NOW) == saved
        assert step(store, payments, case.case_token) == cursor
        assert payments.refund_calls == 1
        assert payments.captures == 1
        assert [r.amount_cents for r in payments.refunds] == [amount]
    with SqliteTransactionStore(path) as store:
        assert store.payment_cursor(case.case_token) == cursor


@pytest.mark.parametrize("amount", [0, -1, 3000, True])
def test_invalid_amount_has_no_writes(tmp_path: Path, amount: int) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = returned(store, FakePaymentClient(REF))
        before = store.transition_history(case.case_token), store.effects(case.case_token)
        with pytest.raises(ValueError, match="invalid returned-mail refund intent"):
            store.request_returned_mail_refund(
                case.case_token, RemedyKind.PARTIAL_REFUND, amount, now=NOW
            )
        assert (store.transition_history(case.case_token), store.effects(case.case_token)) == before


def test_full_must_equal_capture_and_conflicting_second_request_refused(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = returned(store, FakePaymentClient(REF))
        with pytest.raises(ValueError, match="invalid returned-mail refund intent"):
            store.request_returned_mail_refund(
                case.case_token, RemedyKind.FULL_REFUND, 2600, now=NOW
            )
        store.request_returned_mail_refund(
            case.case_token, RemedyKind.PARTIAL_REFUND, 2600, now=NOW
        )
        before = store.transition_history(case.case_token), store.effects(case.case_token)
        for kind, amount in [(RemedyKind.FULL_REFUND, 2999), (RemedyKind.PARTIAL_REFUND, 2601)]:
            with pytest.raises(ValueError, match="refund intent changed"):
                store.request_returned_mail_refund(case.case_token, kind, amount, now=NOW)
        assert (store.transition_history(case.case_token), store.effects(case.case_token)) == before


@pytest.mark.parametrize("crash", [False, True])
@pytest.mark.parametrize("amount", [2600, 2999])
def test_lost_response_retrieves_without_second_refund(
    tmp_path: Path, crash: bool, amount: int
) -> None:
    class Crashed(BaseException):
        pass

    class LostResponse(FakePaymentClient):
        def refund(
            self, ref: str, charge: str, amount: int, key: str, *, captured_cents: int
        ) -> PaymentOutcome:
            # Read through another connection: intent really committed before the call.
            with SqliteTransactionStore(tmp_path / "cases.sqlite") as observer:
                durable = observer.payment_cursor(case.case_token)
                assert durable is not None
                assert durable.owner is not None
                assert durable.call_kind == "refund"
                assert durable.amount_cents == amount
            super().refund(ref, charge, amount, key, captured_cents=captured_cents)
            if crash:
                raise Crashed()
            raise TimeoutError("lost response")

    payments = LostResponse(REF)
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = returned(store, payments)
        store.request_returned_mail_refund(
            case.case_token, RemedyKind.PARTIAL_REFUND, amount, now=NOW
        )
        step(store, payments, case.case_token)
        if crash:
            with pytest.raises(Crashed):
                step(store, payments, case.case_token)
        else:
            assert not step(store, payments, case.case_token).done
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        assert step(store, payments, case.case_token).done
        assert payments.calls[-1][0] == "retrieve_refunds"
        assert payments.refund_calls == 1
        assert payments.captures == 1


def test_ambiguous_replay_is_bounded_and_keeps_intent_pending(tmp_path: Path) -> None:
    payments = FakePaymentClient(REF)
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = returned(store, payments)
        store.request_returned_mail_refund(
            case.case_token, RemedyKind.PARTIAL_REFUND, 2600, now=NOW
        )
        payments.refund_responses = [PaymentOutcome("uncertain", REF)]
        step(store, payments, case.case_token)
        first = step(store, payments, case.case_token)
        step(store, payments, case.case_token)
        replay = step(store, payments, case.case_token)
        assert replay.mutation_key == first.mutation_key
        at = NOW + timedelta(hours=23)
        cursor = step(store, payments, case.case_token, at)
        assert cursor.call_kind == "retrieve"
        assert not cursor.done
        assert cursor.next_at > at
        assert payments.refund_calls == 0
        assert len([c for c in payments.calls if c[0] == "refund"]) == 2


def test_fake_partial_idempotency_and_parameter_mismatch() -> None:
    payments = FakePaymentClient(REF)
    payments.object = client(Transport(fixture("succeeded"))).retrieve(REF)
    result = payments.refund(REF, CHARGE, 2600, "key", captured_cents=CERTIFIED_CENTS)
    assert result.refunds is not None
    assert result.refunds[0].amount_cents == 2600
    assert payments.refund(REF, CHARGE, 2600, "key", captured_cents=CERTIFIED_CENTS) == result
    with pytest.raises(ValueError, match="idempotency parameters"):
        payments.refund(REF, CHARGE, 2500, "key", captured_cents=CERTIFIED_CENTS)
    assert payments.refund_calls == 1


def test_stripe_partial_request_and_full_object_evidence() -> None:
    transport = Transport(refund_fixture(amount=2600))
    result = client(transport).refund(
        REF, CHARGE, 2600, "partial-key", captured_cents=CERTIFIED_CENTS
    )
    assert transport.calls[0][:4] == (
        "post",
        "/v1/refunds",
        {"charge": [CHARGE], "amount": ["2600"]},
        "partial-key",
    )
    assert result.full_object
    assert result.refunds is not None
    assert result.refunds[0].amount_cents == 2600


def test_dispatch_consumes_requested_refund_only(tmp_path: Path) -> None:
    payments = FakePaymentClient(REF)
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = returned(store, payments)
        store.request_returned_mail_refund(case.case_token, RemedyKind.FULL_REFUND, 2999, now=NOW)
        ids = iter(f"dispatch-{n}" for n in range(30))

        def clock() -> datetime:
            cursor = store.payment_cursor(case.case_token)
            assert cursor is not None
            return cursor.next_at

        for _ in range(3):
            dispatch_once(
                store,
                FakeLobClient(),
                payments,
                clock,
                lambda: next(ids),
                case_id=case.case_token,
            )
        cursor = store.payment_cursor(case.case_token)
        assert cursor is not None
        assert cursor.done
        assert payments.refund_calls == 1


def test_existing_stopped_compensation_stays_full(tmp_path: Path) -> None:
    from test_payment_coordinator import (
        compensation_client,
        compensation_step,
        stopped_compensation,
    )

    payments = compensation_client()
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = stopped_compensation(store)
        compensation_step(store, payments, case, reconcile_capture=True)
        for _ in range(3):
            cursor = compensation_step(store, payments, case)
        assert cursor.done
        assert payments.refund_calls == 1
        assert payments.refunds[0].amount_cents == 2999


def test_before_call_crash_replays_original_key_only_after_lookup(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    payments = FakePaymentClient(REF)
    with SqliteTransactionStore(path) as store:
        case = returned(store, payments)
        requested = store.request_returned_mail_refund(
            case.case_token, RemedyKind.PARTIAL_REFUND, 2600, now=NOW
        )
        lookup = step(store, payments, case.case_token)
        with store.submission_ownership():
            interrupted = store.claim_payment(
                case.case_token,
                requested.revision,
                lookup.revision,
                now=NOW,
                operation_id="interrupted-before-call",
                key=lookup.key,
                retry_effect_id="unused",
            )
        assert interrupted.call_kind == "refund"
        assert payments.refund_calls == 0
    with SqliteTransactionStore(path) as store:
        recovered = step(store, payments, case.case_token)
        assert recovered.call_kind == "retrieve"
        replay = step(store, payments, case.case_token)
        assert replay.call_kind == "refund"
        assert replay.mutation_key == interrupted.mutation_key
        assert step(store, payments, case.case_token).done
        assert payments.refund_calls == 1


@pytest.mark.parametrize("existing_amount", [1000, 2600, 2999])
def test_existing_refunds_never_trigger_an_additional_refund(
    tmp_path: Path, existing_amount: int
) -> None:
    payments = FakePaymentClient(REF)
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = returned(store, payments)
        payments.refunds = (
            RefundEvidence("re_Existing", REF, CHARGE, existing_amount, "usd", "succeeded"),
        )
        store.request_returned_mail_refund(
            case.case_token, RemedyKind.PARTIAL_REFUND, 2600, now=NOW
        )
        cursor = step(store, payments, case.case_token)
        assert cursor.done is (existing_amount == 2600)
        step(store, payments, case.case_token)
        assert payments.refund_calls == 0


@pytest.mark.parametrize("status", ["pending", "failed", "canceled"])
def test_identified_unsettled_refund_stays_pending_without_new_key(
    tmp_path: Path, status: str
) -> None:
    payments = FakePaymentClient(REF)
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = returned(store, payments)
        store.request_returned_mail_refund(
            case.case_token, RemedyKind.PARTIAL_REFUND, 2600, now=NOW
        )
        refund = RefundEvidence("re_Unsettled", REF, CHARGE, 2600, "usd", status)
        payments.refund_responses = [replace(payments.object, refunds=(refund,))]
        step(store, payments, case.case_token)
        created = step(store, payments, case.case_token)
        payments.refunds = (refund,)
        for _ in range(3):
            cursor = step(store, payments, case.case_token)
            assert cursor.call_kind == "retrieve"
            assert not cursor.done
            assert cursor.mutation_key == created.mutation_key
        assert len([c for c in payments.calls if c[0] == "refund"]) == 1


def test_only_returned_original_capture_can_request_refund(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        store.save_workspace("workspace", Workspace(), NOW)
        case = store.start_case("workspace", REF, NOW, NOW + timedelta(days=5))
        with pytest.raises(ValueError, match="requires confirmed original capture"):
            store.request_returned_mail_refund(
                case.case_token, RemedyKind.FULL_REFUND, 2999, now=NOW
            )


def test_direct_refund_confirmation_cannot_bypass_reconciliation(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = returned(store, FakePaymentClient(REF))
        case = store.request_returned_mail_refund(
            case.case_token, RemedyKind.PARTIAL_REFUND, 2600, now=NOW
        )
        with pytest.raises(ValueError, match="requires reconciled exact intent"):
            store.apply_payment_event(
                case.case_token,
                ps.RefundIssued("re_Invented", 2600, 2999),
                case.revision,
                occurred_at=NOW,
            )


def test_request_refuses_clock_rollback_without_writes(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = returned(store, FakePaymentClient(REF))
        before = store.transition_history(case.case_token), store.effects(case.case_token)
        with pytest.raises(ValueError, match="precedes case/payment evidence"):
            store.request_returned_mail_refund(
                case.case_token, RemedyKind.FULL_REFUND, 2999, now=NOW - timedelta(seconds=1)
            )
        assert (store.transition_history(case.case_token), store.effects(case.case_token)) == before


def test_second_attempt_key_and_single_case_intent(tmp_path: Path) -> None:
    payments = FakePaymentClient(REF)
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = returned(store, payments)
        for event in (
            cs.RemailRequested(),
            cs.SubmitConfirmed("lob-2"),
            cs.ReturnConfirmed("address"),
        ):
            case = letter(store, case, event)
        store.request_returned_mail_refund(case.case_token, RemedyKind.FULL_REFUND, 2999, now=NOW)
        step(store, payments, case.case_token)
        cursor = step(store, payments, case.case_token)
        assert cursor.mutation_key == mutation_key(
            "returned-mail", case.case_token, RemedyKind.FULL_REFUND, "2"
        )
        assert step(store, payments, case.case_token).done
        with pytest.raises(ValueError, match="refund intent changed"):
            store.request_returned_mail_refund(
                case.case_token, RemedyKind.PARTIAL_REFUND, 2600, now=cursor.next_at
            )
        assert payments.captures == 1
        assert payments.refund_calls == 1
