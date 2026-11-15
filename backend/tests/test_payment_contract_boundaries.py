"""Literal payment boundaries, independent of shared scenario constructors/constants."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from parkingbreaker import payment_state as ps


def test_original_full_capture_can_be_recorded_for_compensation() -> None:
    event = ps.UnexpectedCaptureConfirmed("pi_Original1", "ch_Original1", 2999, "usd")
    result = ps.payment_transition(ps.PaymentState.RELEASED, event)
    assert result.target is ps.PaymentState.CAPTURED
    assert result.effects == (ps.PaymentEffect.REFUND_FULL, ps.PaymentEffect.LOG_COMPENSATION)


@pytest.mark.parametrize(
    ("authorization", "charge", "amount", "currency"),
    [
        ("ch_Original1", "ch_Original1", 2999, "usd"),
        ("pi_", "ch_Original1", 2999, "usd"),
        ("pi_Original!", "ch_Original1", 2999, "usd"),
        ("pi_Original1", "pi_Original1", 2999, "usd"),
        ("pi_Original1", "ch_", 2999, "usd"),
        ("pi_Original1", "ch_Original!", 2999, "usd"),
        ("pi_Original1", "ch_Original1", 2899, "usd"),
        ("pi_Original1", "ch_Original1", 3000, "usd"),
        ("pi_Original1", "ch_Original1", 2999.0, "usd"),
        ("pi_Original1", "ch_Original1", True, "usd"),
        ("pi_Original1", "ch_Original1", 2999, "USD"),
    ],
)
def test_compensation_requires_original_full_usd_capture(
    authorization: str, charge: str, amount: object, currency: str
) -> None:
    with pytest.raises(ValueError, match="requires original full USD service capture"):
        ps.UnexpectedCaptureConfirmed(authorization, charge, cast(int, amount), currency)


@pytest.mark.parametrize(("amount", "captured"), [(1, 1), (1, 2999), (2100, 2999), (2999, 2999)])
def test_refund_records_exact_full_or_partial_amount(amount: int, captured: int) -> None:
    event = ps.RefundIssued("re_Original1", amount, captured)
    assert event.amount_cents == amount
    assert event.captured_cents == captured
    assert event.is_full is (amount == captured)
    assert ps.payment_transition(ps.PaymentState.CAPTURED, event).target is ps.PaymentState.REFUNDED


@pytest.mark.parametrize(("amount", "captured"), [(0, 2999), (-1, 2999), (3000, 2999), (1, 0)])
def test_refund_never_records_zero_negative_or_more_than_capture(
    amount: int, captured: int
) -> None:
    with pytest.raises(
        ValueError, match=r"captured amount must be positive|refund must be positive"
    ):
        ps.RefundIssued("re_Original1", amount, captured)


@pytest.mark.parametrize(
    ("kind", "amount"), [("FULL_REFUND", 2999), ("PARTIAL_REFUND", 1), ("PARTIAL_REFUND", 2999)]
)
@pytest.mark.parametrize("attempt", [1, 2])
def test_return_refund_keeps_capture_until_exact_settlement(
    kind: str, amount: int, attempt: int
) -> None:
    event = ps.ReturnedMailRefundRequested(kind, amount, attempt)
    result = ps.payment_transition(ps.PaymentState.CAPTURED, event)
    assert (event.kind, event.amount_cents, event.attempt) == (kind, amount, attempt)
    assert result.target is ps.PaymentState.CAPTURED
    assert result.effects == (ps.PaymentEffect.REFUND_RETURNED_MAIL,)


@pytest.mark.parametrize(
    ("kind", "amount", "attempt"),
    [
        ("REFUND", 2999, 1),
        ("FULL_REFUND", 2899, 1),
        ("PARTIAL_REFUND", 0, 1),
        ("PARTIAL_REFUND", 3000, 1),
        ("PARTIAL_REFUND", True, 1),
        ("PARTIAL_REFUND", 1.0, 1),
        ("PARTIAL_REFUND", 1, 0),
        ("PARTIAL_REFUND", 1, 3),
        ("PARTIAL_REFUND", 1, True),
        ("PARTIAL_REFUND", 1, 1.0),
    ],
)
def test_return_refund_intent_rejects_invalid_amount_kind_or_attempt(
    kind: str, amount: object, attempt: object
) -> None:
    with pytest.raises(ValueError, match="invalid returned-mail refund intent"):
        ps.ReturnedMailRefundRequested(kind, cast(int, amount), cast(int, attempt))


def test_retry_lands_at_literal_thirty_minute_expiry_boundary() -> None:
    expiry = datetime(2026, 10, 23, 12, tzinfo=UTC)
    last_safe = expiry - timedelta(minutes=30)
    assert ps.next_capture_retry(1, last_safe - timedelta(seconds=30), expiry) == last_safe
    assert ps.next_capture_retry(1, last_safe, expiry) is ps.RetryDecision.WRITE_OFF


def test_uncertain_capture_closes_at_literal_thirty_days_without_second_charge(
    tmp_path: Path,
) -> None:
    # Imports/constructors stay in the test body: invalid event mutations must fail
    # assertions here, rather than abort collection via shared scenario fixtures.
    from parkingbreaker.payment_coordinator import run_payment_step
    from parkingbreaker.payment_fakes import FakePaymentClient
    from parkingbreaker.payment_records import PaymentOutcome
    from parkingbreaker.transaction_store import SqliteTransactionStore
    from test_transaction_lifecycle import NOW, mailed

    class StillUncertain(FakePaymentClient):
        def retrieve(self, authorization_ref: str) -> PaymentOutcome:
            self.calls.append(("retrieve", authorization_ref, 2999, ""))
            return PaymentOutcome("uncertain", authorization_ref)

    path = tmp_path / "original.sqlite"
    client = StillUncertain()
    with SqliteTransactionStore(path) as store:
        case = mailed(store)
        cursor = run_payment_step(
            store,
            client,
            case.case_token,
            expected_case_revision=case.revision,
            expected_cursor_revision=None,
            operation_id="first-uncertain",
            key="original-capture-key",
            clock=lambda: NOW,
            new_effect_id=lambda: uuid4().hex,
        )
    at = case.authorization_expires_at + timedelta(days=30)
    with SqliteTransactionStore(path) as store:
        cursor = run_payment_step(
            store,
            client,
            case.case_token,
            expected_case_revision=case.revision,
            expected_cursor_revision=cursor.revision,
            operation_id="literal-30-days",
            key=cursor.key,
            clock=lambda: at,
            new_effect_id=lambda: uuid4().hex,
        )
        settled = store.get_case(case.case_token)
        effects = store.effects(case.case_token)
        assert settled is not None
        assert settled.payment_state is ps.PaymentState.WRITTEN_OFF
        assert cursor.done
        assert cursor.outcome is not None
        assert cursor.outcome.status == "uncertain"
        assert cursor.key == "original-capture-key"
        assert [e.effect for e in effects if e.revision == settled.revision] == [
            ps.PaymentEffect.LOG_WRITE_OFF
        ]
        assert len(client.calls) == 2
        assert client.captures == client.releases == 0
    with SqliteTransactionStore(path) as store:
        replay = run_payment_step(
            store,
            client,
            case.case_token,
            expected_case_revision=settled.revision,
            expected_cursor_revision=cursor.revision,
            operation_id="restart-replay",
            key=cursor.key,
            clock=lambda: at,
            new_effect_id=lambda: uuid4().hex,
        )
        assert replay == cursor
        assert store.get_case(case.case_token) == settled
        assert store.effects(case.case_token) == effects
        assert len(client.calls) == 2
