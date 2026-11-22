"""T16 contracts through durable payment dispatch and the real Stripe SDK boundary.

Offline transports supply provider evidence; settlement decisions stay in production code.
The uncertainty envelope (128 calls spanning expiry plus 30 days) is deliberately generous,
not a new retry schedule. Local expiry alone must never be fabricated as provider evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

import test_case_service
from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.case_service import STANDARD_OFFER
from parkingbreaker.effect_dispatch import dispatch_once
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import Accepted, LetterEvidence, LobResponse
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.payment_coordinator import run_payment_step
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.payment_records import (
    PaymentClient,
    PaymentCursor,
    PaymentOutcome,
    mutation_key,
)
from parkingbreaker.prices import CERTIFIED_CENTS
from parkingbreaker.transaction_store import EffectConflict, SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_case_service import Clock
from test_case_service import fake_resolution as fake_resolution
from test_effect_dispatch import approved
from test_stripe_payment import (
    CHARGE,
    EXPIRY,
    REF,
    Transport,
    client,
    fixture,
    make_case,
    refund_fixture,
    refund_page,
)
from test_t14_authorization_recovery import BODY, RecoveryCase
from test_t14_authorization_recovery import recovery as recovery
from test_transaction_lifecycle import NOW, letter, start


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def advance(
    store: SqliteTransactionStore, payments: PaymentClient, token: str, at: datetime
) -> PaymentCursor:
    """Use fresh durable revisions and production effect allocation, never fake settlement."""
    case = store.get_case(token)
    assert case is not None
    cursor = store.payment_cursor(token)
    return run_payment_step(
        store,
        payments,
        token,
        expected_case_revision=case.revision,
        expected_cursor_revision=None if cursor is None else cursor.revision,
        operation_id=uuid4().hex,
        key="t16-original" if cursor is None else cursor.key,
        clock=lambda: at,
        new_effect_id=lambda: uuid4().hex,
        retry_effect_id=uuid4().hex,
    )


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_capture_uses_t14_identity_only_after_durable_lob_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient(
        [
            LobResponse(
                200, "ltr_original", tracking_number="940000001", evidence=LetterEvidence.PRODUCTION
            )
        ]
    )
    api, real, intake_id = await approved(tmp_path, monkeypatch, lob)
    try:
        intake = real._intake(intake_id)
        token = intake.case_token
        assert token is not None
        key = mutation_key("authorization", intake_id, "CERTIFIED:2999", "pm_card_visa")
        original = real._store.get_authorization_attempt(intake_id, key)
        assert original is not None
        assert (original.state, original.tier, original.amount_cents) == (
            "authorized",
            "CERTIFIED",
            2999,
        )

        class AcceptedCaptureTransport(Transport):
            def request(
                self,
                method: str,
                url: str,
                headers: Mapping[str, str] | None,
                post_data: object = None,
            ) -> tuple[str, int, Mapping[str, str]]:
                if method == "post" and url.endswith("/capture"):
                    # Observe the committed Lob result at the actual provider call boundary.
                    assert token is not None
                    saved = real._store.submission_cursor(token, 1)
                    assert saved is not None
                    assert isinstance(saved.outcome, Accepted)
                    assert saved.outcome.letter_id == "ltr_original"
                    assert real._store.get_authorization_attempt(intake_id, key) == original
                return super().request(method, url, headers, post_data)

        transport = AcceptedCaptureTransport(fixture(), fixture(), fixture("succeeded"))
        payments = client(transport)
        with pytest.raises(EffectConflict):
            advance(real._store, payments, token, test_case_service.NOW)
        assert not transport.calls
        for _ in range(4):
            dispatch_once(real._store, lob, payments, real._deps.clock, lambda: uuid4().hex)
        case = real._store.get_case(token)
        cursor = real._store.payment_cursor(token)
        assert case is not None
        assert cursor is not None
        assert cursor.done
        assert case.payment_state is ps.PaymentState.CAPTURED
        assert (case.authorization_ref, case.amount_cents) == (
            original.authorization_ref,
            original.amount_cents,
        )
        assert len(lob.calls) == 1
        mutations = [call for call in transport.calls if call[0] == "post"]
        assert len(mutations) == 1
        assert mutations[0][1] == f"/v1/payment_intents/{original.authorization_ref}/capture"
        assert mutations[0][2] == {
            "amount_to_capture": [str(original.amount_cents)],
            "final_capture": ["true"],
            "expand[0]": ["latest_charge"],
        }
        assert mutations[0][3] == cursor.mutation_key
    finally:
        await api.aclose()
        real._store.close()


@pytest.mark.usefixtures("fake_resolution")
def test_changed_offer_cannot_replace_t14_authorization_or_capture_amount(
    recovery: RecoveryCase,
) -> None:
    authorized = recovery.real.authorize(recovery.case_id, BODY)
    original = recovery.attempt
    offer = recovery.real.pre_hold(recovery.case_id)
    assert isinstance(offer, s.Offer)
    # A later price/offer update is not customer permission to change an existing hold.
    changed = offer.model_copy(
        update={
            "tiers": [
                STANDARD_OFFER,
                *[tier.model_copy(update={"amount_cents": 5900}) for tier in offer.tiers],
            ]
        }
    )
    with recovery.real._store._require_open() as database:
        database.execute(
            "UPDATE pb_intake_records SET prehold_json=? WHERE intake_id=?",
            (changed.model_dump_json(), recovery.case_id),
        )
    saved_offer = recovery.real.pre_hold(recovery.case_id)
    assert isinstance(saved_offer, s.Offer)
    assert next(t.amount_cents for t in saved_offer.tiers if t.tier is s.MailTier.CERTIFIED) == 5900
    before = list(recovery.payments.calls)
    with pytest.raises(ServiceConflict, match="tier_locked_at_authorization"):
        recovery.real.choose(recovery.case_id, s.ChooseTier(tier=s.MailTier.STANDARD))
    assert recovery.real.authorize(recovery.case_id, BODY) == authorized
    assert recovery.payments.calls == before
    assert recovery.attempt == original
    intake = recovery.real._intake(recovery.case_id)
    assert intake.case_token is not None
    case = recovery.real._store.get_case(intake.case_token)
    assert case is not None
    for event in (
        cs.ResearchComplete(),
        cs.DraftReady(),
        cs.CustomerApproved(),
        cs.SubmitRequested(),
        cs.SubmitConfirmed("lob-t16-original"),
    ):
        case = recovery.real._store.apply_letter_event(
            case.case_token,
            event,
            case.revision,
            occurred_at=recovery.clock(),
            new_effect_id=lambda: uuid4().hex,
        )
    cursor = advance(recovery.real._store, recovery.payments, case.case_token, recovery.clock())
    cursor = advance(recovery.real._store, recovery.payments, case.case_token, cursor.next_at)
    assert cursor.done
    assert recovery.attempt == original
    assert len(recovery.payments.authorizations) == 1
    assert [call[1:3] for call in recovery.payments.calls if call[0] == "capture"] == [
        (original.authorization_ref, original.amount_cents)
    ]


@pytest.mark.parametrize("changed_amount", [2500, 5900])
@pytest.mark.usefixtures("fake_resolution")
def test_changed_case_amount_cannot_reach_provider(
    recovery: RecoveryCase, changed_amount: int
) -> None:
    recovery.real.authorize(recovery.case_id, BODY)
    original = recovery.attempt
    intake = recovery.real._intake(recovery.case_id)
    assert intake.case_token is not None
    with recovery.real._store._require_open() as database:
        database.execute(
            "UPDATE pb_cases SET amount_cents=? WHERE case_token=?",
            (changed_amount, intake.case_token),
        )
    calls = list(recovery.payments.calls)
    with pytest.raises(StorageCorruption):
        advance(recovery.real._store, recovery.payments, intake.case_token, recovery.clock())
    assert recovery.payments.calls == calls
    assert recovery.attempt == original


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_recorded_authorization_expiry_before_send_stops_and_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A 45-minute hold allows approval before the 30-minute payment safety margin.
    # It still expires within the default review window before dispatch can create mail.
    expiry = test_case_service.NOW + timedelta(minutes=45)
    monkeypatch.setattr(
        test_case_service, "fixture", lambda: fixture(expiry=int(expiry.timestamp()))
    )
    lob = FakeLobClient([LobResponse(200, "ltr_must_not_send", evidence=LetterEvidence.PRODUCTION)])
    api, real, intake_id = await approved(tmp_path, monkeypatch, lob)
    try:
        token = real._intake(intake_id).case_token
        assert token is not None
        case = real._store.get_case(token)
        assert case is not None
        assert case.authorization_expires_at == expiry
        assert case.letter_state is cs.CaseState.FROZEN
        clock = Clock()
        clock.now = expiry + timedelta(seconds=1)
        transport = Transport(fixture("canceled", expiry=int(expiry.timestamp())))
        payments = client(transport)
        for _ in range(3):
            dispatch_once(real._store, lob, payments, clock, lambda: uuid4().hex)
        saved = real._store.get_case(token)
        cursor = real._store.payment_cursor(token)
        assert not lob.calls, "Recorded expired authorization must stop before Lob create"
        assert saved is not None
        assert saved.letter_state is cs.CaseState.STOPPED_NO_CHARGE
        assert saved.payment_state is ps.PaymentState.RELEASED
        assert cursor is not None
        assert cursor.done
        assert all(call[0] == "get" for call in transport.calls)
        assert real._store._require_open().execute(
            "SELECT COUNT(*) FROM pb_intake_authorization_attempts WHERE intake_id=?", (intake_id,)
        ).fetchone() == (1,)
    finally:
        await api.aclose()
        real._store.close()


def test_provider_confirmed_expiry_after_acceptance_writes_off_without_new_charge(
    tmp_path: Path,
) -> None:
    transport = Transport(fixture("canceled"))
    with SqliteTransactionStore(tmp_path / "settlement.sqlite") as store:
        case = make_case(store, datetime.fromtimestamp(EXPIRY, UTC))
        cursor = advance(
            store,
            client(transport),
            case.case_token,
            case.authorization_expires_at + timedelta(seconds=1),
        )
        saved = store.get_case(case.case_token)
        assert cursor.done
        assert saved is not None
        assert saved.payment_state is ps.PaymentState.WRITTEN_OFF
        assert saved.letter_state is cs.CaseState.IN_TRANSIT
        assert [
            e.effect for e in store.pending_effects(case.case_token) if e.machine == "payment"
        ] == [ps.PaymentEffect.SEND_PAYMENT_LINK, ps.PaymentEffect.LOG_WRITE_OFF]
        assert [call[:2] for call in transport.calls] == [("get", f"/v1/payment_intents/{REF}")]


@pytest.mark.parametrize("resolves", [True, False], ids=["eventually-captured", "never-resolves"])
def test_uncertain_capture_has_bounded_original_object_settlement(
    tmp_path: Path, resolves: bool
) -> None:
    responses: list[object] = [fixture(), TimeoutError("lost capture response")]
    responses.extend([TimeoutError("lookup unavailable")] * 128)
    if resolves:
        responses = [fixture(), TimeoutError(), TimeoutError(), fixture("succeeded")]
    transport = Transport(*responses)
    adapter = client(transport)
    with SqliteTransactionStore(tmp_path / "settlement.sqlite") as store:
        case = make_case(store, datetime.fromtimestamp(EXPIRY, UTC))
        cursor = advance(store, adapter, case.case_token, NOW)
        cursor = advance(store, adapter, case.case_token, cursor.next_at)
        assert cursor.outcome.status == "uncertain"
        assert not cursor.done
        horizon = case.authorization_expires_at + timedelta(days=30)
        at = NOW
        for attempt in range(126):
            if cursor.done:
                break
            at = max(cursor.next_at, cursor.first_at + timedelta(hours=12))
            if not resolves:
                at = max(at, horizon - timedelta(hours=12 * (125 - attempt)))
            cursor = advance(store, adapter, case.case_token, at)
        saved = store.get_case(case.case_token)
        assert saved is not None
        assert len(transport.calls) <= 128
        assert len([call for call in transport.calls if call[0] == "post"]) == 1
        assert all(
            call[1] in {f"/v1/payment_intents/{REF}", f"/v1/payment_intents/{REF}/capture"}
            for call in transport.calls
        )
        assert cursor.done, (
            f"T16: no terminal settlement after {len(transport.calls)} provider calls; "
            f"last lookup={at.isoformat()}, expiry={case.authorization_expires_at.isoformat()}, "
            f"payment={saved.payment_state}, next_at={cursor.next_at.isoformat()}"
        )
        assert saved.payment_state in {
            ps.PaymentState.CAPTURED,
            ps.PaymentState.WRITTEN_OFF,
            ps.PaymentState.RELEASED,
        }
        if resolves:
            assert saved.payment_state is ps.PaymentState.CAPTURED
            assert len(transport.calls) == 4
        else:
            assert at >= horizon
            # A written-off unknown result needs a durable decision, not invented money evidence.
            assert any(
                row.machine == "payment" and row.target == saved.payment_state
                for row in store.transition_history(case.case_token)
            )


def test_pre_mail_release_is_exactly_once_across_dispatch_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "settlement.sqlite"
    payments = FakePaymentClient()
    lob = FakeLobClient()
    with SqliteTransactionStore(path) as store:
        case = letter(store, start(store), cs.WindowExpired())
        for _ in range(4):
            dispatch_once(store, lob, payments, lambda: NOW, lambda: uuid4().hex)
        cursor = store.payment_cursor(case.case_token)
        assert cursor is not None
        assert cursor.done
        effects = store.effects(case.case_token)
    with SqliteTransactionStore(path) as store:
        for _ in range(4):
            dispatch_once(store, lob, payments, lambda: NOW, lambda: uuid4().hex)
        saved = store.get_case(case.case_token)
        assert saved is not None
        assert saved.payment_state is ps.PaymentState.RELEASED
        assert store.payment_cursor(case.case_token) == cursor
        assert store.effects(case.case_token) == effects
        assert not store.pending_effects(case.case_token)
    assert payments.releases == 1
    assert [call[0] for call in payments.calls] == ["retrieve", "release"]
    assert not lob.calls
    assert payments.captures == 0


def test_capture_confirmed_after_release_is_fully_refunded_exactly_once(tmp_path: Path) -> None:
    transport = Transport(
        fixture(),
        fixture("succeeded"),  # Cancel reports original capture instead of release.
        fixture("succeeded"),
        refund_page(),
        refund_fixture(),
        fixture("succeeded"),
        refund_page(refund_fixture()),
    )
    payments = client(transport)
    lob = FakeLobClient()
    path = tmp_path / "settlement.sqlite"
    clock = Clock()
    clock.now = NOW
    with SqliteTransactionStore(path) as store:
        store.save_workspace("workspace", Workspace(), NOW)
        case = store.start_case("workspace", REF, NOW, datetime.fromtimestamp(EXPIRY, UTC))
        case = letter(store, case, cs.WindowExpired())
        assert case.payment_state is ps.PaymentState.RELEASED
        for _ in range(5):
            pending = store.payment_cursor(case.case_token)
            if pending is not None:
                clock.now = max(clock.now, pending.next_at)
            dispatch_once(store, lob, payments, clock, lambda: uuid4().hex)
        saved = store.get_case(case.case_token)
        cursor = store.payment_cursor(case.case_token)
        assert saved is not None
        assert cursor is not None
        assert cursor.done
        assert saved.payment_state is ps.PaymentState.REFUNDED
        assert saved.letter_state is cs.CaseState.STOPPED_NO_CHARGE
        events = [row.event_name for row in store.transition_history(case.case_token)]
        assert events.count("UnexpectedCaptureConfirmed") == events.count("RefundIssued") == 1
        assert (
            sum(e.effect is ps.PaymentEffect.REFUND_FULL for e in store.effects(case.case_token))
            == 1
        )
        assert not store.pending_effects(case.case_token)
    with SqliteTransactionStore(path) as store:
        for _ in range(3):
            dispatch_once(store, lob, payments, clock, lambda: uuid4().hex)
        assert store.get_case(case.case_token) == saved
        assert store.payment_cursor(case.case_token) == cursor
    mutations = [call for call in transport.calls if call[0] == "post"]
    assert [call[1] for call in mutations] == [f"/v1/payment_intents/{REF}/cancel", "/v1/refunds"]
    assert mutations[1][2] == {"charge": [CHARGE], "amount": ["2999"]}
    assert mutations[1][3] is not None
    assert not lob.calls


@pytest.mark.parametrize("first", ["capture", "release"])
@pytest.mark.parametrize("second", ["capture", "release"])
def test_fake_refuses_fresh_mutation_after_terminal_money_state(first: str, second: str) -> None:
    payments = FakePaymentClient(REF)
    if first == "capture":
        result = payments.capture(REF, 2999, "original", final_capture=True)
    else:
        result = payments.release(REF, "original")
    assert result.status == ("succeeded" if first == "capture" else "canceled")
    counters = (payments.captures, payments.releases)
    with pytest.raises(ValueError, match=r"state|capture|cancel|release|transition"):
        mutate(payments, second, "fresh-invalid")
    assert (payments.captures, payments.releases) == counters


@pytest.mark.parametrize("kind", ["capture", "release"])
def test_fake_exact_mutation_key_replay_is_idempotent(kind: str) -> None:
    payments = FakePaymentClient(REF)
    if kind == "capture":
        first = payments.capture(REF, 2999, "same", final_capture=True)
        assert payments.capture(REF, 2999, "same", final_capture=True) == first
    else:
        first = payments.release(REF, "same")
        assert payments.release(REF, "same") == first
    assert (payments.captures, payments.releases) == (
        int(kind == "capture"),
        int(kind == "release"),
    )


@pytest.mark.parametrize("changed", ["amount", "final", "operation", "metadata", "card"])
def test_fake_rejects_same_key_with_changed_parameters(changed: str) -> None:
    payments = FakePaymentClient(REF)
    payments.authorize(2999, "pm_original", "authorization", {"case": "original"})
    if changed in {"metadata", "card"}:
        with pytest.raises(ValueError, match="idempotency parameters"):
            payments.authorize(
                2999,
                "pm_other" if changed == "card" else "pm_original",
                "authorization",
                {"case": "other" if changed == "metadata" else "original"},
            )
    else:
        payments.capture(REF, 2999, "capture", final_capture=True)
        with pytest.raises(ValueError, match=r"idempotency parameters|full final capture"):
            mutate(
                payments,
                "release" if changed == "operation" else "capture",
                "capture",
                amount=5900 if changed == "amount" else 2999,
                final=changed != "final",
            )
    assert payments.captures <= 1
    assert len(payments.authorizations) == 1


@pytest.mark.parametrize("kind", ["capture", "release"])
def test_fake_authorization_key_cannot_be_reused_for_different_operation(kind: str) -> None:
    payments = FakePaymentClient(REF)
    payments.authorize(2999, "pm_original", "shared", {"case": "original"})
    with pytest.raises(ValueError, match="idempotency"):
        mutate(payments, kind, "shared")
    assert payments.captures == payments.releases == 0


def test_fake_caches_uncertain_mutation_without_inventing_capture() -> None:
    payments = FakePaymentClient(REF)
    payments.responses = [PaymentOutcome("uncertain", REF)]
    first = payments.capture(REF, 2999, "cached-500", final_capture=True)
    assert first.status == "uncertain"
    assert payments.capture(REF, 2999, "cached-500", final_capture=True) == first
    assert payments.retrieve(REF).status == "requires_capture"
    assert payments.captures == 0


@pytest.mark.parametrize("changed", ["amount", "charge", "operation"])
def test_fake_full_refund_replay_and_changed_parameters(changed: str) -> None:
    payments = FakePaymentClient(REF)
    payments.object = client(Transport(fixture("succeeded"))).retrieve(REF)
    first = payments.refund(REF, CHARGE, 2999, "refund", captured_cents=CERTIFIED_CENTS)
    assert first.full_refund
    assert payments.refund(REF, CHARGE, 2999, "refund", captured_cents=CERTIFIED_CENTS) == first
    assert payments.refund_calls == 1
    if changed == "operation":
        with pytest.raises(ValueError, match="idempotency parameters"):
            payments.release(REF, "refund")
    else:
        with pytest.raises(ValueError, match="original full refund"):
            payments.refund(
                REF,
                "ch_Other" if changed == "charge" else CHARGE,
                5900 if changed == "amount" else 2999,
                "refund",
                captured_cents=CERTIFIED_CENTS,
            )
    assert payments.refund_calls == 1


def test_fake_refuses_refund_before_capture() -> None:
    payments = FakePaymentClient(REF)
    with pytest.raises(ValueError, match="original full refund"):
        payments.refund(REF, CHARGE, 2999, "premature-refund", captured_cents=CERTIFIED_CENTS)
    assert payments.refund_calls == 0


def mutate(
    payments: FakePaymentClient, kind: str, key: str, *, amount: int = 2999, final: bool = True
) -> PaymentOutcome:
    if kind == "capture":
        return payments.capture(REF, amount, key, final_capture=final)
    assert kind == "release"
    return payments.release(REF, key)
