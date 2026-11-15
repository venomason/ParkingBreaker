"""Case-bound money proofs through the existing offline payment boundaries."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker import prices
from parkingbreaker.config_fakes import FakeStripeTransport
from parkingbreaker.payment_coordinator import run_payment_step
from parkingbreaker.payment_fakes import FakePaymentClient, full_outcome
from parkingbreaker.payment_records import (
    PaymentClient,
    PaymentCursor,
    PaymentOutcome,
    outcome_json,
)
from parkingbreaker.recovery_policy import payment_compatible
from parkingbreaker.returned_mail import RemedyKind
from parkingbreaker.stripe_payment import STRIPE_API_VERSION, StripePaymentClient
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace


def _client(sdk: bool) -> tuple[PaymentClient, FakeStripeTransport | None]:
    if not sdk:
        return FakePaymentClient("pi_Fake1"), None
    transport = FakeStripeTransport()
    return StripePaymentClient(
        secret_key="offline-placeholder", api_version=STRIPE_API_VERSION, transport=transport
    ), transport


def _authorize(client: PaymentClient, amount: int) -> PaymentOutcome:
    held = client.authorize(amount, "pm_card_visa", "hold", {})
    if isinstance(client, FakePaymentClient):
        # Supply the fake's optional versioned original-charge evidence for refunds.
        held = replace(
            held,
            evidence_version=1,
            original_charge_ref="ch_CaseAmount",
            capture_before=int((datetime.now(UTC) + timedelta(days=5)).timestamp()),
            classification="verified_object",
        )
        client.object = held
        client.responses = [
            full_outcome(replace(held, status="succeeded", charge_ref="ch_CaseAmount"))
        ]
    assert held.authorization_inspection is not None
    return held


def _start(store: SqliteTransactionStore, held: PaymentOutcome) -> CaseRecord:
    now = datetime.now(UTC)
    store.save_workspace("workspace", Workspace(), now)
    assert held.authorization_inspection is not None
    assert held.amount_cents is not None
    return store.start_case(
        "workspace",
        held.authorization_ref,
        now,
        held.authorization_inspection.expires_at,
        held.amount_cents,
    )


def _letter(store: SqliteTransactionStore, case: CaseRecord, event: cs.Event) -> CaseRecord:
    return store.apply_letter_event(
        case.case_token,
        event,
        case.revision,
        occurred_at=case.authorized_at,
        new_effect_id=lambda: uuid4().hex,
    )


def _step(store: SqliteTransactionStore, client: PaymentClient, token: str) -> PaymentCursor:
    case = store.get_case(token)
    assert case is not None
    cursor = store.payment_cursor(token)
    return run_payment_step(
        store,
        client,
        token,
        expected_case_revision=case.revision,
        expected_cursor_revision=None if cursor is None else cursor.revision,
        operation_id=uuid4().hex,
        key="case-money",
        new_effect_id=lambda: uuid4().hex,
        clock=lambda: case.authorized_at if cursor is None else cursor.next_at,
    )


@pytest.mark.parametrize("sdk", [False, True], ids=["payment-fake", "stripe-transport"])
@pytest.mark.parametrize("action", ["full_refund", "partial_refund", "release", "compensation"])
def test_case_amount_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: bool, action: str
) -> None:
    monkeypatch.setattr(prices, "ALLOWED_PRICE_CENTS", frozenset({2900, 2499}))
    client, transport = _client(sdk)
    held = _authorize(client, 2499)
    # A separate authorization must not overwrite this PI's amount.
    other = client.authorize(2900, "pm_card_visa", "other-hold", {})
    assert other.authorization_ref != held.authorization_ref
    assert client.retrieve(held.authorization_ref).amount_cents == 2499
    with pytest.raises(ValueError, match="capture"):
        client.capture(held.authorization_ref, 2499, "non-final", final_capture=False)
    if action == "compensation":
        assert (
            client.capture(
                held.authorization_ref, 2499, "external", final_capture=True
            ).amount_cents
            == 2499
        )
    elif action == "release" and isinstance(client, FakePaymentClient):
        client.responses = []
    path = tmp_path / "money.sqlite"
    with SqliteTransactionStore(path) as store:
        case = _start(store, held)
        if action in {"release", "compensation"}:
            case = _letter(store, case, cs.WindowExpired())
        else:
            for event in (
                cs.ResearchComplete(),
                cs.DraftReady(),
                cs.CustomerApproved(),
                cs.SubmitRequested(),
                cs.SubmitConfirmed("lob-case"),
            ):
                case = _letter(store, case, event)
        cursor = _step(store, client, case.case_token)
        if action != "compensation":
            cursor = _step(store, client, case.case_token)
        if action in {"full_refund", "partial_refund"}:
            assert cursor.done
            assert cursor.outcome.amount_received == 2499
            captured = store.get_case(case.case_token)
            assert captured is not None
            assert captured.payment_state is ps.PaymentState.CAPTURED
            case = captured
            case = _letter(store, case, cs.ReturnConfirmed("address"))
            # An allowed total for a different case is never a full refund for this case.
            with pytest.raises(ValueError, match="case amount"):
                store.request_returned_mail_refund(
                    case.case_token, RemedyKind.FULL_REFUND, 2900, now=case.authorized_at
                )
            refund = 1000 if action == "partial_refund" else 2499
            store.request_returned_mail_refund(
                case.case_token,
                RemedyKind.PARTIAL_REFUND if action == "partial_refund" else RemedyKind.FULL_REFUND,
                refund,
                now=case.authorized_at,
            )
        if action != "release":
            for _ in range(3):  # original capture lookup, one refund mutation, complete lookup
                cursor = _step(store, client, case.case_token)
            assert cursor.done
            assert cursor.kind == "refund"
            assert cursor.outcome.refund_settled(1000 if action == "partial_refund" else 2499)
            assert cursor.outcome.full_refund is (action != "partial_refund")
            refunds = [
                r
                for r in store.transition_history(case.case_token)
                if r.event_name == "RefundIssued"
            ]
            assert len(refunds) == 1
            assert '"captured_cents":2499' in refunds[0].event_json
        else:
            assert cursor.done
            assert cursor.outcome.status == "canceled"
        assert cursor.outcome.amount_cents == 2499
        untouched = client.retrieve(other.authorization_ref)
        assert untouched.amount_cents == 2900
        assert untouched.status == "requires_capture"
        if isinstance(client, FakePaymentClient):
            assert all(c[2] == 2499 for c in client.calls if c[0] in {"capture", "release"})
            assert [c[2] for c in client.calls if c[0] == "refund"] == (
                [] if action == "release" else [1000 if action == "partial_refund" else 2499]
            )
        else:
            assert transport is not None
            assert transport.objects[held.authorization_ref]["amount"] == 2499
            assert [r["amount"] for r in transport.refunds.values()] == (
                [] if action == "release" else [1000 if action == "partial_refund" else 2499]
            )
    with SqliteTransactionStore(path) as store:
        assert store.payment_cursor(case.case_token) == cursor
        saved = store.get_case(case.case_token)
        assert saved is not None
        assert saved.amount_cents == 2499
        assert saved.payment_state is (
            ps.PaymentState.RELEASED if action == "release" else ps.PaymentState.REFUNDED
        )


@pytest.mark.parametrize("release", [False, True], ids=["capture", "release"])
@pytest.mark.parametrize("status", ["requires_capture", "succeeded", "canceled"])
@pytest.mark.parametrize(("case_amount", "observed"), [(2499, 2900), (2900, 2499)])
def test_allowed_provider_amount_must_equal_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release: bool,
    status: str,
    case_amount: int,
    observed: int,
) -> None:
    monkeypatch.setattr(prices, "ALLOWED_PRICE_CENTS", frozenset({2900, 2499}))
    client, transport = _client(True)
    held = _authorize(client, case_amount)
    assert transport is not None
    obj = transport.objects[held.authorization_ref]
    obj.update(
        amount=observed,
        status=status,
        amount_received=observed if status == "succeeded" else 0,
        amount_capturable=observed if status == "requires_capture" else 0,
    )
    charge = obj["latest_charge"]
    assert isinstance(charge, dict)
    charge.update(
        amount=observed, amount_captured=obj["amount_received"], captured=status == "succeeded"
    )
    remote = client.retrieve(held.authorization_ref)
    assert remote.full_object
    assert remote.amount_cents == observed
    with SqliteTransactionStore(tmp_path / "money.sqlite") as store:
        case = _start(store, held)
        if release:
            case = _letter(store, case, cs.WindowExpired())
        else:
            for event in (
                cs.ResearchComplete(),
                cs.DraftReady(),
                cs.CustomerApproved(),
                cs.SubmitRequested(),
                cs.SubmitConfirmed("lob-case"),
            ):
                case = _letter(store, case, event)
        assert not payment_compatible(case.payment_state, remote, case.amount_cents)
        cursor = _step(store, client, case.case_token)
        assert cursor.outcome.status == "uncertain"
        assert not cursor.done
        assert cursor.outcome.amount_cents == case_amount
        with store.submission_ownership():
            claimed = store.claim_payment(
                case.case_token,
                case.revision,
                cursor.revision,
                now=cursor.next_at,
                operation_id=uuid4().hex,
                key="case-money",
                retry_effect_id="",
                authorization_evidence_required=True,
            )
            with pytest.raises(ValueError, match="provider anchor mismatch"):
                store.finish_payment(claimed, remote, now=claimed.next_at)
        cursor = _step(store, client, case.case_token)
        assert cursor.call_kind == "retrieve"
        assert not cursor.done
        assert cursor.outcome.status == "uncertain"
        assert store.get_case(case.case_token) == case
        assert obj["status"] == status


@pytest.mark.parametrize("sdk", [False, True], ids=["payment-fake", "stripe-transport"])
@pytest.mark.parametrize("amount", [2498, 2901])
def test_disallowed_amount_rejected_at_authorization_and_retrieval(
    monkeypatch: pytest.MonkeyPatch, sdk: bool, amount: int
) -> None:
    monkeypatch.setattr(prices, "ALLOWED_PRICE_CENTS", frozenset({2900, 2499}))
    client, transport = _client(sdk)
    with pytest.raises(ValueError, match="full service authorization"):
        client.authorize(amount, "pm_card_visa", "bad-hold", {})
    held = _authorize(client, 2499)
    if isinstance(client, FakePaymentClient):
        client.object = full_outcome(replace(held, amount_cents=amount))
        with pytest.raises(ValueError, match="invalid payment outcome"):
            outcome_json(client.retrieve(held.authorization_ref))
    else:
        assert transport is not None
        obj = transport.objects[held.authorization_ref]
        obj.update(amount=amount, amount_capturable=amount)
        charge = obj["latest_charge"]
        assert isinstance(charge, dict)
        charge["amount"] = amount
        remote = client.retrieve(held.authorization_ref)
        assert remote.status == "uncertain"
        assert not remote.full_object


def test_fake_pi_amount_and_refund_key_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prices, "ALLOWED_PRICE_CENTS", frozenset({2900, 2499}))
    client = FakePaymentClient("pi_Fake1")
    first = client.authorize(2499, "pm_card_visa", "one", {})
    second = client.authorize(2900, "pm_card_visa", "two", {})
    assert first.authorization_ref != second.authorization_ref
    assert first.amount_cents is not None
    assert second.amount_cents is not None
    for held in (first, second):
        assert held.amount_cents is not None
        captured = client.capture(
            held.authorization_ref,
            held.amount_cents,
            f"capture-{held.authorization_ref}",
            final_capture=True,
        )
        assert captured.amount_cents == held.amount_cents
    captured_first = client.retrieve(first.authorization_ref)
    assert captured_first.charge_ref is not None
    client.refund(
        first.authorization_ref,
        captured_first.charge_ref,
        2499,
        "refund-key",
        captured_cents=first.amount_cents,
    )
    captured_second = client.retrieve(second.authorization_ref)
    assert captured_second.charge_ref is not None
    with pytest.raises(ValueError, match="idempotency parameters changed"):
        client.refund(
            second.authorization_ref,
            captured_second.charge_ref,
            2499,
            "refund-key",
            captured_cents=second.amount_cents,
        )
    assert client.refunds == ()
    assert client.retrieve(first.authorization_ref).amount_received == 2499
    assert client.retrieve(second.authorization_ref).amount_received == 2900


def test_fresh_stripe_refund_uses_recorded_captured_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prices, "ALLOWED_PRICE_CENTS", frozenset({2900, 2499}))
    original, transport = _client(True)
    assert transport is not None
    held = _authorize(original, 2499)
    captured = original.capture(held.authorization_ref, 2499, "capture", final_capture=True)
    assert captured.charge_ref is not None
    fresh = StripePaymentClient(
        secret_key="offline-placeholder", api_version=STRIPE_API_VERSION, transport=transport
    )
    result = fresh.refund(
        held.authorization_ref, captured.charge_ref, 2499, "refund", captured_cents=2499
    )
    assert result.status == "succeeded"
    assert result.amount_cents == result.amount_received == 2499
    assert result.original_charge_ref == captured.charge_ref
    assert result.refund_settled(2499)
    assert len(transport.refunds) == 1
    for amount, total in ((2500, 2499), (1, 2500), (1, 2499.0), (1, True), (True, 2499)):
        with pytest.raises(ValueError, match="captured service amount"):
            fresh.refund(
                held.authorization_ref,
                captured.charge_ref,
                amount,
                "invalid-refund",
                captured_cents=total,  # type: ignore[arg-type]
            )
    assert len(transport.refunds) == 1
    assert "invalid-refund" not in transport.refund_keys
