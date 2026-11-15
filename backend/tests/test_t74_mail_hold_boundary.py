"""T74 criterion 1/4: an incident hold gates the durable physical create-intent.

Real SQLite store, the real dispatcher and submission coordinator, and the existing fake
Lob seam. A hold is an overlay, not a state: it pauses new creates while expiry stops,
provider truth, lookups, capture, release and evidence projection keep running. It never
recalls a letter already created.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from mail_samples import RECIPIENT, make_request, seed_approval, seed_destination
from parkingbreaker import case_state as cs
from parkingbreaker.case_hold_store import (
    CaseHold,
    CommandOutcome,
    HoldCommand,
    HoldReason,
)
from parkingbreaker.case_service import CaseService
from parkingbreaker.case_state import CaseState, Effect
from parkingbreaker.config import ProviderMode, Settings, build_services
from parkingbreaker.effect_dispatch import dispatch_once
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import (
    Accepted,
    LetterEvidence,
    LetterRequest,
    LobResponse,
    MetadataLookup,
    Uncertain,
    idempotency_key,
)
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.submission_coordinator import run_submission_step
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore, SubmissionPaused
from parkingbreaker.workspace import Workspace
from prepared_packet_support import redirected_pdf
from test_beta_admission import NOW
from test_beta_dispatch_timing import (
    ORIGINAL,
    TOKEN,
    WINDOW,
    Beta,
    Clock,
    Deadline,
    Ids,
    LookupFake,
    LostAnswer,
)
from test_case_service import NOW as PAID_NOW
from test_case_service import Clock as ServiceClock
from test_case_service import fake_resolution, stripe
from test_config import SECRET
from test_effect_dispatch import approved, dispatch, payment_fixture
from test_payment_stress_failures import _stripe_money
from test_stripe_payment import REF

PAID_FROZEN = "paid-frozen"
assert fake_resolution  # the existing fixture, used by the API-level test below


def seed_hold(
    store: SqliteTransactionStore,
    token: str,
    *,
    command_id: str = "seed-hold",
    at: datetime = NOW,
) -> CaseHold:
    """Seed an already-effective overlay, as if before worker startup (T74 limit)."""
    outcome, hold = store.record_hold_command(
        command_id,
        token,
        HoldCommand.HOLD,
        at,
        expected_generation=None,
        reason=HoldReason.INCIDENT,
        review_due_at=at,
    )
    assert outcome is CommandOutcome.APPLIED
    assert hold is not None
    assert hold.held
    return hold


def paid_frozen(path: Path, expiry_delta: timedelta) -> tuple[SqliteTransactionStore, CaseRecord]:
    """A real paid FROZEN case with one prepared identity and natural timings."""
    store = SqliteTransactionStore(path)
    store.save_workspace("workspace", Workspace(), NOW)
    case = store.start_case("workspace", REF, NOW, NOW + expiry_delta)
    for event in (cs.ResearchComplete(), cs.DraftReady(), cs.CustomerApproved()):
        case = store.apply_letter_event(
            case.case_token,
            event,
            case.revision,
            occurred_at=NOW,
            new_effect_id=lambda: uuid4().hex,
        )
    seed_destination(store, case, NOW)
    assert case.letter_state is CaseState.FROZEN
    store.prepare(case.case_token, 1, make_request(token=TOKEN), NOW)
    seed_approval(store, case.case_token)
    return store, case


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_a_held_paid_frozen_case_starts_no_create_until_a_valid_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(200, "ltr_held_paid", evidence=LetterEvidence.PRODUCTION)])
    client, real, case = await approved(tmp_path, monkeypatch, lob)
    payments, transport = stripe(payment_fixture(), payment_fixture("succeeded"))
    object.__setattr__(real._deps, "payments", payments)
    object.__setattr__(real._deps, "clock", ServiceClock())
    try:
        token = real._intake(case).case_token
        assert token is not None
        before = real._store.get(token, 1)
        assert before is not None
        outcome, hold = real.hold_case(token, HoldReason.INCIDENT, "hold-paid")
        assert outcome is CommandOutcome.APPLIED
        assert hold is not None
        assert hold.held
        # Repeated sweeps while held: zero provider calls and no durable create intent.
        for _ in range(3):
            result = await dispatch(client)
            assert result["failed"] == 0
        assert lob.calls == []
        assert lob.lookups == []
        assert real._store.submission_cursor(token, 1) is None
        assert transport.calls == []
        assert real.status(case).state is CaseState.FROZEN
        assert real._store.get(token, 1) == before
        assert real._store.case_hold(token) == hold

        assert real.resume_case(token, "resume-paid", hold.generation)[0] is CommandOutcome.APPLIED
        assert len(lob.calls) == 1
        assert lob.calls[0][0] == before.key
        assert lob.calls[0][1] == before.request
        assert real.status(case).state is CaseState.IN_TRANSIT
    finally:
        await client.aclose()


def test_a_held_beta_frozen_case_never_creates_across_repeats_and_reopen(
    tmp_path: Path,
) -> None:
    beta = Beta(tmp_path / "pb.sqlite")
    hold = seed_hold(beta.store, beta.token)
    frozen = beta.store.get(beta.token, 1)
    assert frozen is not None
    # The beta review deadline derives from the customer window and may already be due.
    assert hold.review_due_at is not None
    assert hold.review_due_at <= NOW
    for offset in (timedelta(minutes=1), timedelta(minutes=2)):
        assert beta.dispatch(NOW + offset) == 0
        assert beta.case.letter_state is CaseState.FROZEN
        assert beta.store.submission_cursor(beta.token, 1) is None
        assert beta.lob.calls == []
        assert beta.lob.lookups == []
        assert beta.store.get(beta.token, 1) == frozen
        assert beta.store.case_hold(beta.token) == hold
    # No new submission intent was written, and any queued create effect stayed paused.
    submits = [row for row in beta.store.effects(beta.token) if row.effect is Effect.SUBMIT_LETTER]
    assert submits == []
    assert all(row.claimed_at is None for row in beta.store.effects(beta.token))
    beta.restart()
    assert beta.dispatch(NOW + timedelta(minutes=3)) == 0
    assert beta.lob.calls == []
    assert beta.store.case_hold(beta.token) == hold


def test_a_held_beta_frozen_case_still_takes_window_and_deadline_stops(
    tmp_path: Path,
) -> None:
    stopped = Beta(tmp_path / "window.sqlite")
    seed_hold(stopped.store, stopped.token)
    assert stopped.dispatch(NOW + WINDOW) == 0
    assert stopped.case.letter_state is CaseState.STOPPED_NO_CHARGE
    assert stopped.store.transition_history(stopped.token)[-1].event_name == "WindowExpired"
    assert stopped.lob.calls == []

    unsafe = Beta(tmp_path / "deadline.sqlite")
    seed_hold(unsafe.store, unsafe.token)
    unsafe.deadline = Deadline(False)
    assert unsafe.dispatch(NOW + timedelta(minutes=1)) == 0
    assert unsafe.case.letter_state is CaseState.STOPPED_NO_CHARGE
    assert unsafe.store.transition_history(unsafe.token)[-1].event_name == "Stopped"
    assert unsafe.lob.calls == []


def test_a_held_paid_frozen_case_still_takes_authorization_expiry_and_releases_once(
    tmp_path: Path,
) -> None:
    # A short authorization makes its expiry the applicable FROZEN refusal, not the 30m
    # customer window. A hold must not suppress this funding-appropriate stop.
    store, case = paid_frozen(tmp_path / "paid.sqlite", timedelta(minutes=20))
    try:
        seed_hold(store, case.case_token)
        payments, transport = stripe(payment_fixture(), payment_fixture("canceled"))
        lob = FakeLobClient()
        at = NOW + timedelta(minutes=1)
        ids = Ids("paid")
        # The first pass applies the stop and records the release intent; the next pass
        # performs the one existing release. Both run while the hold is effective.
        for _ in range(3):
            assert (
                dispatch_once(
                    store,
                    lob,
                    payments,
                    Clock(at),
                    ids,
                    case_id=case.case_token,
                    deadline_feasible=None,
                )
                >= 0
            )
        stopped = store.get_case(case.case_token)
        assert stopped is not None
        assert stopped.letter_state is CaseState.STOPPED_NO_CHARGE
        assert stopped.payment_state is PaymentState.RELEASED
        assert "AuthorizationExpired" in [
            row.event_name for row in store.transition_history(case.case_token)
        ]
        assert lob.calls == []
        _stripe_money(transport, captures=0, releases=1)
        # The overlay survives payment truth; it did not become permission to send.
        saved_hold = store.case_hold(case.case_token)
        assert saved_hold is not None
        assert saved_hold.held
    finally:
        store.close()


class BlockingLob(FakeLobClient):
    """A create that is blocked inside the provider call, under submission ownership."""

    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.entered = entered
        self.release = release

    def create_letter(self, request: LetterRequest, idempotency_key: str) -> LobResponse:
        self.entered.set()
        assert self.release.wait(timeout=10)
        return super().create_letter(request, idempotency_key)


def test_hold_is_busy_while_a_create_owns_submission_then_becomes_effective(
    tmp_path: Path,
) -> None:
    beta = Beta(tmp_path / "race.sqlite")
    entered, release = threading.Event(), threading.Event()
    blocking = BlockingLob(entered, release)
    blocking.script.append(LobResponse(200, ORIGINAL, evidence=LetterEvidence.PRODUCTION))
    processed: list[int] = []

    def create() -> None:
        store = SqliteTransactionStore(beta.path)
        try:
            processed.append(
                dispatch_once(
                    store,
                    blocking,
                    beta.payments,
                    beta.clock,
                    Ids("bg"),
                    case_id=beta.token,
                    deadline_feasible=beta.deadline,
                )
            )
        finally:
            store.close()

    worker = threading.Thread(target=create)
    worker.start()
    try:
        assert entered.wait(timeout=10)
        peer_store = SqliteTransactionStore(beta.path)
        try:
            peer = build_services(
                Settings(ProviderMode.FAKE, tmp_path, SECRET, {}), store=peer_store
            )
            assert isinstance(peer, CaseService)
            outcome, hold = peer.hold_case(beta.token, HoldReason.INCIDENT, "race-1")
            assert outcome is CommandOutcome.BUSY
            assert hold is None or not hold.held
        finally:
            peer_store.close()
    finally:
        release.set()
        worker.join(timeout=30)
    assert not worker.is_alive()
    assert processed
    assert processed[0] > 0
    assert len(blocking.calls) == 1
    assert beta.case.letter_state is CaseState.IN_TRANSIT
    # The same open command takes effect once the call and its result commit complete.
    peer_store = SqliteTransactionStore(beta.path)
    try:
        peer = build_services(Settings(ProviderMode.FAKE, tmp_path, SECRET, {}), store=peer_store)
        assert isinstance(peer, CaseService)
        outcome, hold = peer.hold_case(beta.token, HoldReason.INCIDENT, "race-1")
        assert outcome is CommandOutcome.APPLIED
        assert hold is not None
        assert hold.held
        assert peer.hold_case(beta.token, HoldReason.INCIDENT, "race-1")[0] is (
            CommandOutcome.APPLIED
        )
    finally:
        peer_store.close()
    assert len(blocking.calls) == 1


def test_direct_coordinator_call_on_a_held_submitting_case_is_paused(
    tmp_path: Path,
) -> None:
    beta = Beta(tmp_path / "direct.sqlite")
    case = beta.store.apply_letter_event(
        beta.token,
        cs.SubmitRequested(),
        beta.case.revision,
        occurred_at=NOW,
        new_effect_id=lambda: uuid4().hex,
    )
    assert case.letter_state is CaseState.SUBMITTING
    hold = seed_hold(beta.store, beta.token)
    packet = beta.store.get(beta.token, 1)
    assert packet is not None

    with pytest.raises(SubmissionPaused):
        run_submission_step(
            beta.store,
            beta.lob,
            beta.token,
            1,
            expected_case_revision=case.revision,
            expected_cursor_revision=None,
            operation_id="direct-1",
            clock=beta.clock,
            new_effect_id=lambda: uuid4().hex,
        )
    assert beta.store.submission_cursor(beta.token, 1) is None
    operations = beta.store._require_open().execute(
        "SELECT COUNT(*) FROM pb_submission_operations WHERE case_token=?", (beta.token,)
    )
    assert operations.fetchone() == (0,)
    assert beta.lob.calls == []
    assert beta.store.get(beta.token, 1) == packet

    # A valid resume gates a later create again, which reuses the one approved identity.
    outcome, resumed = beta.store.record_hold_command(
        "resume-direct",
        beta.token,
        HoldCommand.RESUME,
        NOW,
        expected_generation=hold.generation,
        reason=None,
    )
    assert outcome is CommandOutcome.APPLIED
    assert resumed is not None
    assert not resumed.held
    beta.lob.script.append(LobResponse(200, ORIGINAL, evidence=LetterEvidence.PRODUCTION))
    cursor = run_submission_step(
        beta.store,
        beta.lob,
        beta.token,
        1,
        expected_case_revision=beta.store.get_case(beta.token).revision,  # type: ignore[union-attr]
        expected_cursor_revision=None,
        operation_id="direct-2",
        clock=beta.clock,
        new_effect_id=lambda: uuid4().hex,
    )
    assert not isinstance(cursor.outcome, Uncertain)
    assert beta.case.letter_state is CaseState.IN_TRANSIT
    assert len(beta.lob.calls) == 1
    assert beta.lob.calls[0][0] == idempotency_key(beta.token, 1)
    assert beta.lob.calls[0][1] == packet.request


def test_a_held_uncertain_original_is_looked_up_not_created_again(
    tmp_path: Path,
) -> None:
    beta = Beta(tmp_path / "lookup.sqlite")
    beta.lob = LostAnswer(evidence=LetterEvidence.PRODUCTION)
    beta.dispatch(NOW + timedelta(minutes=1))
    original = beta.cursor
    assert isinstance(original.outcome, Uncertain)
    first_sent_at, first_next_at = original.first_sent_at, original.next_at
    packet = beta.store.get(beta.token, 1)
    assert packet is not None
    seed_hold(beta.store, beta.token)

    beta.dispatch(first_next_at)

    assert beta.case.letter_state is CaseState.IN_TRANSIT
    assert len(beta.lob.calls) == 1  # no create retry while held, even under the same key
    assert beta.lob.lookups == [TOKEN]
    settled = beta.cursor
    assert settled.first_sent_at == first_sent_at
    assert settled.sends == original.sends
    assert beta.store.get(beta.token, 1) == packet
    saved_hold = beta.store.case_hold(beta.token)
    assert saved_hold is not None
    assert saved_hold.held


def test_evidence_projection_still_settles_a_held_case_without_a_create(
    tmp_path: Path,
) -> None:
    beta = Beta(tmp_path / "evidence.sqlite")
    beta.lob = LostAnswer(evidence=LetterEvidence.CREATED)
    beta.dispatch(NOW + timedelta(minutes=1))
    cursor = beta.cursor
    assert isinstance(cursor.outcome, Uncertain)
    seed_hold(beta.store, beta.token)
    case = beta.store.get_case(beta.token)
    assert case is not None
    at = cursor.next_at
    with beta.store.submission_ownership():
        claimed, kind = beta.store.claim_submission(
            beta.token,
            1,
            case.revision,
            cursor.revision,
            now=at,
            operation_id=beta.ids(),
            evidence=True,
        )
        assert kind == "lookup"
        beta.store.finish_submission(
            claimed,
            Accepted(ORIGINAL, evidence=LetterEvidence.PRODUCTION),
            now=at,
            effect_ids=(),
            new_effect_id=beta.ids,
        )
    assert beta.case.letter_state is CaseState.IN_TRANSIT
    assert beta.lob.calls
    assert len(beta.lob.calls) == 1
    assert beta.lob.lookups == []


def test_a_temporary_held_lookup_does_not_make_later_retries_lookup_only(
    tmp_path: Path,
) -> None:
    beta = Beta(tmp_path / "h1-sticky.sqlite")
    beta.lob.script.append(LobResponse(None, error="connection refused"))
    beta.dispatch(NOW + timedelta(minutes=1))
    cursor = beta.cursor
    assert isinstance(cursor.outcome, Uncertain)
    assert cursor.outcome.letter_id is None
    assert cursor.sends == 1
    packet = beta.store.get(beta.token, 1)
    assert packet is not None
    hold = seed_hold(beta.store, beta.token, at=cursor.next_at)
    absent = LookupFake(MetadataLookup(None, authoritative=False))

    beta.dispatch(cursor.next_at, lob=absent)

    looked = beta.cursor
    assert isinstance(looked.outcome, Uncertain)
    assert looked.lookup_only is False
    assert looked.sends == cursor.sends
    assert looked.next_at > cursor.next_at
    assert absent.calls == []
    assert beta.store.get(beta.token, 1) == packet
    outcome, _ = beta.store.record_hold_command(
        "resume-h1",
        beta.token,
        HoldCommand.RESUME,
        looked.next_at,
        expected_generation=hold.generation,
    )
    assert outcome is CommandOutcome.APPLIED
    beta.restart()
    reopened = beta.cursor
    assert reopened.lookup_only is False
    # Before the saved due time no provider call; at due one same-key create succeeds.
    assert beta.dispatch(reopened.next_at - timedelta(seconds=20)) == 0
    assert len(beta.lob.calls) == 1
    beta.lob.script.append(LobResponse(200, ORIGINAL, evidence=LetterEvidence.PRODUCTION))
    beta.dispatch(reopened.next_at)
    assert beta.case.letter_state is CaseState.IN_TRANSIT
    assert len(beta.lob.calls) == 2
    assert {call[0] for call in beta.lob.calls} == {packet.key}
    assert beta.lob.letters == {TOKEN: ORIGINAL}
    assert beta.store.get(beta.token, 1) == packet


def test_a_crash_during_a_temporary_held_lookup_reserves_a_retry(
    tmp_path: Path,
) -> None:
    beta = Beta(tmp_path / "h1-crash.sqlite")
    beta.lob.script.append(LobResponse(None, error="connection refused"))
    beta.dispatch(NOW + timedelta(minutes=1))
    cursor = beta.cursor
    assert isinstance(cursor.outcome, Uncertain)
    assert cursor.outcome.letter_id is None
    hold = seed_hold(beta.store, beta.token, at=cursor.next_at)
    with beta.store.submission_ownership():
        claimed, kind = beta.store.claim_submission(
            beta.token,
            1,
            beta.case.revision,
            cursor.revision,
            now=cursor.next_at,
            operation_id=beta.ids(),
        )
    assert kind == "lookup"
    assert claimed.lookup_only is False
    assert claimed.sends == cursor.sends
    # Process death before finish: reopen must not persist an accidental sticky flag.
    beta.restart()
    reopened = beta.cursor
    assert reopened.lookup_only is False
    outcome, _ = beta.store.record_hold_command(
        "resume-h1-crash",
        beta.token,
        HoldCommand.RESUME,
        claimed.next_at,
        expected_generation=hold.generation,
    )
    assert outcome is CommandOutcome.APPLIED
    beta.restart()
    beta.lob.script.append(LobResponse(200, ORIGINAL, evidence=LetterEvidence.PRODUCTION))
    beta.dispatch(claimed.next_at)
    assert beta.case.letter_state is CaseState.IN_TRANSIT
    assert len(beta.lob.calls) == 2
    assert len({call[0] for call in beta.lob.calls}) == 1
    assert beta.lob.letters == {TOKEN: ORIGINAL}


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_held_paid_attempt_two_remail_stays_paused_until_valid_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from parkingbreaker.case_state import RemailRequested

    lob = FakeLobClient(
        [
            LobResponse(200, "ltr_original", evidence=LetterEvidence.PRODUCTION),
            LobResponse(200, "ltr_remail", evidence=LetterEvidence.PRODUCTION),
        ]
    )
    client, real, case = await approved(tmp_path, monkeypatch, lob)
    object.__setattr__(real._deps, "lob_webhook_secret", "secret")
    payments, transport = stripe(payment_fixture(), payment_fixture("succeeded"))
    object.__setattr__(real._deps, "payments", payments)
    try:
        await dispatch(client)
        await dispatch(client)
        token = real._intake(case).case_token
        assert token is not None
        at = str(int(PAID_NOW.timestamp()))

        async def returned(event_id: str) -> None:
            payload = json.dumps(
                {
                    "id": event_id,
                    "event_type": {"id": "letter.certified.returned_to_sender"},
                    "date_created": PAID_NOW.isoformat(),
                    "body": {"id": "ltr_original"},
                }
            ).encode()
            digest = hmac.new(b"secret", at.encode() + b"." + payload, hashlib.sha256).hexdigest()
            response = await client.post(
                "/webhooks/lob",
                content=payload,
                headers={"Lob-Signature": digest, "Lob-Signature-Timestamp": at},
            )
            assert response.status_code == 200

        await returned("evt_returned")
        assert real.status(case).state is CaseState.RETURNED
        original = real._store.get(token, 1)
        assert original is not None
        corrected = replace(RECIPIENT, line1="987 Corrected Avenue")
        returned_case = real._store.get_case(token)
        assert returned_case is not None
        seed_destination(real._store, returned_case, PAID_NOW, corrected)
        remail_request = replace(
            original.request,
            submission_token="sub_remail",
            recipient=corrected,
            pdf=redirected_pdf(real, case, corrected),
        )
        packet = real._store.prepare(token, 2, remail_request, PAID_NOW)
        seed_approval(real._store, token, 2)
        real._apply(token, RemailRequested())
        submitting = real._store.get_case(token)
        assert submitting is not None
        assert submitting.letter_state is CaseState.SUBMITTING
        assert submitting.attempt == 2
        outcome, hold = real.hold_case(token, HoldReason.INCIDENT, "h2-hold")
        assert outcome is CommandOutcome.APPLIED
        assert hold is not None
        assert hold.held
        # Repeated dispatches and a direct coordinator call write no attempt-two intent.
        for _ in range(2):
            await dispatch(client)
        assert len(lob.calls) == 1
        assert real._store.submission_cursor(token, 2) is None
        assert real._store.get(token, 2) == packet
        active = real._store.get_case(token)
        assert active is not None
        assert real._deps.lob is not None
        with pytest.raises(SubmissionPaused):
            run_submission_step(
                real._store,
                real._deps.lob,
                token,
                2,
                expected_case_revision=active.revision,
                expected_cursor_revision=None,
                operation_id="direct-h2",
                clock=real._deps.clock,
                new_effect_id=real._deps.new_id,
            )
        assert len(lob.calls) == 1
        assert real._store.submission_cursor(token, 2) is None
        # Store reopen keeps the hold; a wrong-generation resume stays paused.
        real._store.close()
        real._store = SqliteTransactionStore(tmp_path / "pb.sqlite")
        assert real._store.case_hold(token) == hold
        assert real.resume_case(token, "h2-wrong", hold.generation - 1)[0] is (CommandOutcome.STALE)
        await dispatch(client)
        assert len(lob.calls) == 1
        assert real._store.case_hold(token) == hold
        # A valid resume permits exactly the one saved attempt-two identity.
        assert real.resume_case(token, "h2-valid", hold.generation)[0] is CommandOutcome.APPLIED
        assert real.status(case).state is CaseState.IN_TRANSIT
        mailed = real._store.get_case(token)
        assert mailed is not None
        assert mailed.attempt == 2
        assert len(lob.calls) == 2
        assert lob.calls[1][0] == packet.key
        assert lob.calls[1][1] == packet.request
        assert lob.calls[0][0] != lob.calls[1][0]
        assert lob.letters["sub_remail"] == "ltr_remail"
        assert set(lob.letters.values()) == {"ltr_original", "ltr_remail"}
        assert real.status(case).payment_state is PaymentState.CAPTURED
        assert len(transport.calls) == 2
        await dispatch(client)
        assert len(lob.calls) == 2
    finally:
        await client.aclose()


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_held_paid_attempt_two_destination_mismatch_refuses_the_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legitimate current-destination change must block the held attempt-two create."""
    from parkingbreaker.case_state import RemailRequested

    lob = FakeLobClient([LobResponse(200, "ltr_original", evidence=LetterEvidence.PRODUCTION)])
    client, real, case = await approved(tmp_path, monkeypatch, lob)
    object.__setattr__(real._deps, "lob_webhook_secret", "secret")
    payments, transport = stripe(payment_fixture(), payment_fixture("succeeded"))
    object.__setattr__(real._deps, "payments", payments)
    try:
        await dispatch(client)
        await dispatch(client)
        token = real._intake(case).case_token
        assert token is not None
        at = str(int(PAID_NOW.timestamp()))

        async def returned(event_id: str) -> None:
            payload = json.dumps(
                {
                    "id": event_id,
                    "event_type": {"id": "letter.certified.returned_to_sender"},
                    "date_created": PAID_NOW.isoformat(),
                    "body": {"id": "ltr_original"},
                }
            ).encode()
            digest = hmac.new(b"secret", at.encode() + b"." + payload, hashlib.sha256).hexdigest()
            response = await client.post(
                "/webhooks/lob",
                content=payload,
                headers={"Lob-Signature": digest, "Lob-Signature-Timestamp": at},
            )
            assert response.status_code == 200

        await returned("evt_returned")
        original = real._store.get(token, 1)
        assert original is not None
        corrected = replace(RECIPIENT, line1="987 Corrected Avenue")
        returned_case = real._store.get_case(token)
        assert returned_case is not None
        seed_destination(real._store, returned_case, PAID_NOW, corrected)
        remail_request = replace(
            original.request,
            submission_token="sub_remail",
            recipient=corrected,
            pdf=redirected_pdf(real, case, corrected),
        )
        packet = real._store.prepare(token, 2, remail_request, PAID_NOW)
        seed_approval(real._store, token, 2)
        real._apply(token, RemailRequested())
        outcome, hold = real.hold_case(token, HoldReason.INCIDENT, "h2m-hold")
        assert outcome is CommandOutcome.APPLIED
        assert hold is not None
        before_payments = [call for call in transport.calls if call[0] == "post"]
        # Replace the latest decision through the existing store API, so it no longer
        # equals the frozen attempt-two destination.
        current = real._store.get_case(token)
        assert current is not None
        seed_destination(
            real._store, current, PAID_NOW, replace(corrected, line1="111 Other Street")
        )
        assert real.resume_case(token, "h2m-valid", hold.generation)[0] is (CommandOutcome.APPLIED)
        await dispatch(client)
        # The guard refuses/waits: no second provider create and no extra payment call.
        assert len(lob.calls) == 1
        assert real._store.get(token, 2) == packet
        current = real._store.get_case(token)
        assert current is not None
        assert current.letter_state is not CaseState.IN_TRANSIT
        assert current.payment_state is PaymentState.CAPTURED
        assert [call for call in transport.calls if call[0] == "post"] == before_payments
    finally:
        await client.aclose()
