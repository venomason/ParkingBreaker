"""Review regressions for authorization migration, outcomes and replay timing."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier, Event, Lock
from typing import NoReturn
from unittest.mock import Mock

import pytest

from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.case_service import AUTHORIZATION_REPLAY_LIMIT, STANDARD_OFFER, CaseService
from parkingbreaker.intake_store import AuthorizationAttempt, initialize_intake
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.payment_fakes import full_outcome
from parkingbreaker.payment_records import PaymentClient, PaymentOutcome, mutation_key
from parkingbreaker.stripe_payment import STRIPE_API_VERSION, StripePaymentClient
from parkingbreaker.transaction_records import CaseRecord
from test_case_service import INTAKE, PNG_B64, Clock, service
from test_case_service import fake_resolution as fake_resolution
from test_stripe_payment import charge, fixture
from test_t14_authorization_recovery import BODY, PM, RecoveryCase
from test_t14_authorization_recovery import recovery as recovery


def downgrade_to_v1(case: RecoveryCase) -> None:
    case.real._store.close()
    with closing(sqlite3.connect(case.path)) as database:
        database.execute("DROP TABLE pb_intake_authorization_attempts")
        database.execute("UPDATE pb_store_versions SET version=1 WHERE component='intake'")
        database.commit()


def assert_migration_rolled_back(path: Path) -> None:
    with closing(sqlite3.connect(path)) as database:
        assert database.execute(
            "SELECT version FROM pb_store_versions WHERE component='intake'"
        ).fetchone() == (1,)
        assert (
            database.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='pb_intake_authorization_attempts'"
            ).fetchone()
            is None
        )


def test_v1_selected_intake_without_case_refuses_migration(recovery: RecoveryCase) -> None:
    record = recovery.real._store.get_intake(recovery.case_id)
    assert record is not None
    assert (record.selected_tier, record.case_token) == ("CERTIFIED", None)
    recovery.assert_no_case()
    downgrade_to_v1(recovery)

    with pytest.raises(StorageCorruption, match=r"^legacy authorization unreconciled$"):
        recovery.restart()

    assert_migration_rolled_back(recovery.path)
    assert recovery.payments.calls == []


def test_v1_orphan_case_alone_refuses_migration(recovery: RecoveryCase) -> None:
    store = recovery.real._store
    record = store.get_intake(recovery.case_id)
    assert record is not None
    # Isolate the orphan-case guard: there is no selected, unlinked intake.
    store.update_intake(recovery.case_id, record.revision, selected_tier=None, case_token=None)
    orphan = store.start_case(
        record.workspace_token,
        "auth_legacy_orphan",
        recovery.clock(),
        recovery.clock() + timedelta(days=6),
    )
    assert store._require_open().execute(
        "SELECT COUNT(*) FROM pb_intake_records WHERE case_token=?", (orphan.case_token,)
    ).fetchone() == (0,)
    downgrade_to_v1(recovery)

    with pytest.raises(StorageCorruption, match=r"^legacy authorization unreconciled$"):
        recovery.restart()

    assert_migration_rolled_back(recovery.path)
    assert recovery.payments.calls == []


def test_v1_linked_case_and_unselected_intake_migrate_and_retry(recovery: RecoveryCase) -> None:
    store = recovery.real._store
    record = store.get_intake(recovery.case_id)
    assert record is not None
    legacy = store.start_case(
        record.workspace_token,
        "auth_legacy_linked",
        recovery.clock(),
        recovery.clock() + timedelta(days=6),
    )
    linked = store.update_intake(
        recovery.case_id,
        record.revision,
        selected_tier="CERTIFIED",
        case_token=legacy.case_token,
    )
    expected = recovery.real.authorize(recovery.case_id, BODY)
    workspace = recovery.real.create_workspace("owner_unselected", s.CreateWorkspace(notes=""))
    photo = recovery.real.upload_photo(
        workspace.workspace_id, s.PhotoUpload(content_type="image/png", data_base64=PNG_B64)
    )
    receipt = recovery.real.submit(
        workspace.workspace_id,
        s.SubmitIntake.model_validate(
            {**INTAKE, "photo_refs": [photo.photo_ref], "citation_photo_ref": photo.photo_ref}
        ),
    )
    unselected = store.get_intake(receipt.case_id)
    assert unselected is not None
    assert (unselected.selected_tier, unselected.case_token) == (None, None)
    downgrade_to_v1(recovery)

    recovery.restart()
    store = recovery.real._store
    assert store._require_open().execute(
        "SELECT version FROM pb_store_versions WHERE component='intake'"
    ).fetchone() == (2,)
    assert store.get_intake(recovery.case_id) == linked
    assert store.get_case(legacy.case_token) == legacy
    assert store.get_intake(receipt.case_id) == unselected
    assert recovery.real.authorize(recovery.case_id, BODY) == expected
    assert recovery.real.authorize(recovery.case_id, BODY) == expected
    with pytest.raises(ServiceConflict, match=r"^choose_tier_first$"):
        recovery.real.authorize(receipt.case_id, BODY)
    assert recovery.payments.calls == []
    assert store._require_open().execute(
        "SELECT COUNT(*) FROM pb_intake_authorization_attempts"
    ).fetchone() == (0,)

    recovery.real.choose(receipt.case_id, s.ChooseTier(tier=s.MailTier.CERTIFIED))
    authorization = recovery.real.authorize(receipt.case_id, BODY)
    assert authorization.status == "authorized"
    assert recovery.real.authorize(receipt.case_id, BODY) == authorization
    assert recovery.payments.calls == [
        (
            "authorize",
            PM,
            2999,
            mutation_key("authorization", receipt.case_id, "CERTIFIED:2999", PM),
        )
    ]
    assert store._require_open().execute(
        "SELECT COUNT(*) FROM pb_intake_authorization_attempts"
    ).fetchone() == (1,)
    assert store._require_open().execute("SELECT COUNT(*) FROM pb_cases").fetchone() == (2,)
    assert len(recovery.payments.authorizations) == 1


def test_v1_intake_component_migrates_without_cases_table(tmp_path: Path) -> None:
    with closing(sqlite3.connect(tmp_path / "intake_only.sqlite")) as database:
        database.execute(
            "CREATE TABLE pb_store_versions (component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
        )
        initialize_intake(database)
        database.execute("DROP TABLE pb_intake_authorization_attempts")
        database.execute("UPDATE pb_store_versions SET version=1 WHERE component='intake'")
        database.commit()
        assert (
            database.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='pb_cases'"
            ).fetchone()
            is None
        )

        initialize_intake(database)

        assert database.execute(
            "SELECT version FROM pb_store_versions WHERE component='intake'"
        ).fetchone() == (2,)
        assert database.execute(
            "SELECT COUNT(*) FROM pb_intake_authorization_attempts"
        ).fetchone() == (0,)


def test_late_uncertain_outcome_cannot_overwrite_decline(recovery: RecoveryCase) -> None:
    pending = recovery.reserve()
    store = recovery.real._store
    declined = store.record_authorization_outcome(
        recovery.case_id, recovery.key, "declined", "auth_declined", recovery.clock()
    )
    assert declined.state == "declined"
    assert declined.authorization_ref == "auth_declined"
    assert declined.revision == pending.revision + 1
    late = store.record_authorization_outcome(
        recovery.case_id, recovery.key, "uncertain", None, recovery.clock() + timedelta(seconds=1)
    )

    assert late == declined
    assert late.revision == declined.revision
    assert not store._require_open().in_transaction
    assert recovery.attempt == declined
    recovery.restart()
    assert recovery.attempt == declined
    assert recovery.payments.calls == []


def test_decline_race_keeps_same_card_final_and_allows_one_new_card(recovery: RecoveryCase) -> None:
    payments = recovery.payments
    payments.next_response = full_outcome(PaymentOutcome("declined", "auth_declined"))
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    declined = recovery.attempt
    assert declined.state == "declined"
    calls = list(payments.calls)
    assert calls == [("authorize", PM, 2999, recovery.key)]
    recovery.clock.now += timedelta(seconds=1)
    assert (
        recovery.real._store.record_authorization_outcome(
            recovery.case_id, recovery.key, "uncertain", None, recovery.clock()
        )
        == declined
    )
    recovery.restart()

    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    assert payments.calls == calls
    assert recovery.attempt == declined
    recovery.assert_no_case()

    payments.object = full_outcome(PaymentOutcome("requires_capture", "auth_new_card"))
    new_card = s.StartAuthorization(payment_method_id="pm_new_card", terms_accepted=True)
    authorization = recovery.real.authorize(recovery.case_id, new_card)
    new_key = mutation_key("authorization", recovery.case_id, "CERTIFIED:2999", "pm_new_card")
    assert authorization.payment_intent_id == "auth_new_card"
    assert authorization.status == "authorized"
    assert payments.calls == [*calls, ("authorize", "pm_new_card", 2999, new_key)]
    assert recovery.real._store._require_open().execute(
        "SELECT attempt_number, payment_method_id, state FROM pb_intake_authorization_attempts "
        "ORDER BY attempt_number"
    ).fetchall() == [(1, PM, "declined"), (2, "pm_new_card", "authorized")]
    assert recovery.attempt == declined
    assert len(payments.authorizations) == 2
    assert sum(a[3].status == "requires_capture" for a in payments.authorizations.values()) == 1
    assert recovery.real._store._require_open().execute(
        "SELECT COUNT(*) FROM pb_cases"
    ).fetchone() == (1,)
    assert recovery.real.authorize(recovery.case_id, new_card) == authorization
    assert len(payments.calls) == 2


@pytest.mark.parametrize("state", ["pending", "uncertain"])
def test_tier_change_cannot_replace_an_unresolved_or_authorized_hold(
    recovery: RecoveryCase, state: str
) -> None:
    # Advertise both tiers so choose() must exercise the persisted intent lock.
    offer = recovery.real.pre_hold(recovery.case_id)
    assert isinstance(offer, s.Offer)
    offer = offer.model_copy(update={"tiers": [*offer.tiers, STANDARD_OFFER]})
    with recovery.real._store._require_open() as database:
        database.execute(
            "UPDATE pb_intake_records SET prehold_json=? WHERE intake_id=?",
            (offer.model_dump_json(), recovery.case_id),
        )
    if state == "pending":
        recovery.reserve()
    else:
        recovery.payments.lose_next_response = True
        with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
            recovery.real.authorize(recovery.case_id, BODY)
    saved = recovery.attempt
    assert (saved.state, saved.tier, saved.amount_cents) == (state, "CERTIFIED", 2999)
    calls = list(recovery.payments.calls)
    assert len(calls) == (0 if state == "pending" else 1)
    recovery.restart()
    intake = recovery.real._store.get_intake(recovery.case_id)

    with pytest.raises(ServiceConflict, match=r"^tier_locked_at_authorization$"):
        recovery.real.choose(recovery.case_id, s.ChooseTier(tier=s.MailTier.STANDARD))
    assert recovery.real._store.get_intake(recovery.case_id) == intake
    assert recovery.payments.calls == calls
    assert recovery.attempt == saved
    authorization = recovery.real.authorize(recovery.case_id, BODY)
    assert recovery.payments.calls == [*calls, ("authorize", PM, 2999, recovery.key)]
    assert recovery.payments.requests == [(2999, PM, recovery.key, recovery.metadata)] * len(
        recovery.payments.calls
    )
    recovery.assert_one_case(authorization)

    with pytest.raises(ServiceConflict, match=r"^tier_locked_at_authorization$"):
        recovery.real.choose(recovery.case_id, s.ChooseTier(tier=s.MailTier.STANDARD))
    assert recovery.real.authorize(recovery.case_id, BODY) == authorization
    assert len(recovery.payments.calls) == (1 if state == "pending" else 2)
    recovery.assert_one_case(authorization)


class PruningTransport:
    """Retain keys for 24h, with independent holds and simulated request transit."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.cache: dict[str, tuple[datetime, dict[str, object]]] = {}
        self.holds: list[str] = []
        self.keys: list[str] = []
        self.arrivals: list[datetime] = []
        self.delays: dict[int, timedelta] = {}
        self.lock = Lock()
        self.concurrent_replays = False
        self.first_ready, self.second_processed = Event(), Event()

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        post_data: object = None,
    ) -> tuple[str, int, Mapping[str, str]]:
        assert method == "post"
        assert url.endswith("/v1/payment_intents")
        assert headers is not None
        with self.lock:
            number = len(self.keys) + 1
            self.clock.now += self.delays.get(number, timedelta())
            now = self.clock()
            key = headers["Idempotency-Key"]
            self.keys.append(key)
            self.arrivals.append(now)
            cached = self.cache.get(key)
            if cached is not None and now - cached[0] >= timedelta(hours=24):
                del self.cache[key]
            if key not in self.cache:
                reference = f"pi_Hold{len(self.holds) + 1}"
                obj = fixture(expiry=int((now + timedelta(days=5)).timestamp()))
                obj["id"] = reference
                charge(obj).update(
                    id=f"ch_Hold{len(self.holds) + 1}",
                    payment_intent=reference,
                    created=int(now.timestamp()),
                )
                self.holds.append(reference)
                self.cache[key] = (now, obj)
            if number == 1:
                raise TimeoutError("response lost after original hold")
            response = json.dumps(self.cache[key][1])
        if self.concurrent_replays:
            if number == 2:
                self.first_ready.set()
                assert self.second_processed.wait(10)
            elif number == 3:
                self.second_processed.set()
        return response, 200, {}


@pytest.fixture
def pruning_transport(recovery: RecoveryCase, monkeypatch: pytest.MonkeyPatch) -> PruningTransport:
    def forbid_network(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("network forbidden")

    monkeypatch.setattr("socket.socket.connect", forbid_network)
    transport = PruningTransport(recovery.clock)
    payments = StripePaymentClient(
        secret_key="sk_test_offline", api_version=STRIPE_API_VERSION, transport=transport
    )
    recovery.real = CaseService(replace(recovery.real._deps, payments=payments))
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    assert recovery.attempt.state == "uncertain"
    assert recovery.attempt.authorization_ref is None
    assert transport.keys == [recovery.key]
    assert transport.holds == ["pi_Hold1"]
    recovery.assert_no_case()
    return transport


def assert_single_retained_hold(
    case: RecoveryCase, transport: PruningTransport, authorization: s.Authorization
) -> None:
    assert transport.holds == ["pi_Hold1"]
    assert authorization.status == "authorized"
    assert authorization.amount_cents == 2999
    assert authorization.payment_intent_id == transport.holds[0]
    assert case.attempt.state == "authorized"
    assert case.attempt.authorization_ref == authorization.payment_intent_id
    assert case.real._store._require_open().execute(
        "SELECT idempotency_key, authorization_ref FROM pb_intake_authorization_attempts"
    ).fetchall() == [(case.key, authorization.payment_intent_id)]
    intake = case.real._store.get_intake(case.case_id)
    assert intake is not None
    assert intake.case_token is not None
    saved = case.real._store.get_case(intake.case_token)
    assert saved is not None
    assert saved.authorization_ref == authorization.payment_intent_id
    assert saved.amount_cents == 2999
    assert saved.workspace_token == intake.workspace_token
    assert case.real._store._require_open().execute("SELECT COUNT(*) FROM pb_cases").fetchone() == (
        1,
    )


@pytest.mark.parametrize("transit", [timedelta(seconds=2), timedelta(seconds=80)])
def test_pruning_replay_before_limit_reaches_retained_key(
    recovery: RecoveryCase, pruning_transport: PruningTransport, transit: timedelta
) -> None:
    saved = recovery.attempt
    admitted_at = saved.created_at + timedelta(hours=23, seconds=-1)
    recovery.clock.now = admitted_at
    pruning_transport.delays[2] = transit

    authorization = recovery.real.authorize(recovery.case_id, BODY)

    assert pruning_transport.keys == [recovery.key] * 2
    assert pruning_transport.arrivals == [saved.created_at, admitted_at + transit]
    assert (
        timedelta(hours=23)
        < pruning_transport.arrivals[-1] - saved.created_at
        < timedelta(hours=24)
    )
    assert_single_retained_hold(recovery, pruning_transport, authorization)


@pytest.mark.parametrize(
    "elapsed",
    [
        timedelta(hours=23),
        timedelta(hours=24, seconds=-1),
        timedelta(hours=24),
        timedelta(hours=25),
    ],
)
def test_pruning_replay_at_or_after_limit_never_reaches_provider(
    recovery: RecoveryCase, pruning_transport: PruningTransport, elapsed: timedelta
) -> None:
    saved = recovery.attempt
    recovery.clock.now = saved.created_at + elapsed
    pruning_transport.delays[2] = timedelta(seconds=2)

    with pytest.raises(ServiceConflict, match=r"^authorization_outcome_unresolved$"):
        recovery.real.authorize(recovery.case_id, BODY)

    assert pruning_transport.keys == [recovery.key]
    assert pruning_transport.arrivals == [saved.created_at]
    assert pruning_transport.holds == ["pi_Hold1"]
    assert recovery.clock() == saved.created_at + elapsed
    assert recovery.attempt == saved
    recovery.assert_no_case()


@pytest.mark.parametrize("concurrent", [False, True])
@pytest.mark.parametrize("known_reference", [False, True])
def test_clock_rollback_before_uncertain_update_never_reaches_provider(
    recovery: RecoveryCase,
    pruning_transport: PruningTransport,
    monkeypatch: pytest.MonkeyPatch,
    concurrent: bool,
    known_reference: bool,
) -> None:
    original = recovery.attempt
    provider_clock = Clock()
    pruning_transport.clock = provider_clock
    provider_clock.now = original.created_at + timedelta(hours=23, seconds=-1)
    recovery.clock.now = provider_clock.now
    request = pruning_transport.request

    def lose_retained_reply(
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        post_data: object = None,
    ) -> NoReturn:
        request(method, url, headers, post_data)
        raise TimeoutError("lost retained replay response")

    with monkeypatch.context() as lost_reply:
        lost_reply.setattr(pruning_transport, "request", lose_retained_reply)
        with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
            recovery.real.authorize(recovery.case_id, BODY)
    saved = recovery.attempt
    assert (saved.state, saved.authorization_ref) == ("uncertain", None)
    assert saved.updated_at == recovery.clock()
    assert saved.revision > original.revision
    assert pruning_transport.keys == [recovery.key] * 2
    assert pruning_transport.holds == ["pi_Hold1"]
    if known_reference:
        saved = recovery.real._store.record_authorization_outcome(
            recovery.case_id, recovery.key, "uncertain", "pi_Hold1", recovery.clock()
        )

    # Provider time reaches key pruning while the local clock visibly rolls back.
    provider_clock.now = original.created_at + timedelta(hours=24)
    recovery.clock.now = original.created_at + timedelta(hours=23, seconds=-2)
    assert original.created_at < recovery.clock() < saved.updated_at
    payments = recovery.real._deps.payments
    authorize = Mock(wraps=payments.authorize)
    retrieve = Mock(wraps=payments.retrieve)
    monkeypatch.setattr(payments, "authorize", authorize)
    monkeypatch.setattr(payments, "retrieve", retrieve)
    ready = Barrier(2) if concurrent else None

    def call() -> None:
        real = service(recovery.path, payments, recovery.clock)
        try:
            if ready is not None:
                ready.wait(timeout=10)
            with pytest.raises(ServiceConflict, match=r"^authorization_outcome_unresolved$"):
                real.authorize(recovery.case_id, BODY)
        finally:
            real._store.close()

    if concurrent:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(call) for _ in range(2)]
            for result in results:
                result.result(timeout=10)
    else:
        call()

    authorize.assert_not_called()
    retrieve.assert_not_called()
    assert pruning_transport.keys == [recovery.key] * 2
    assert pruning_transport.arrivals == [original.created_at, saved.updated_at]
    assert pruning_transport.holds == ["pi_Hold1"]
    assert recovery.attempt == saved
    assert recovery.real._store._require_open().execute(
        "SELECT COUNT(*) FROM pb_intake_authorization_attempts"
    ).fetchone() == (1,)
    recovery.assert_no_case()


def test_concurrent_pruning_replays_keep_one_hold_and_one_reference(
    recovery: RecoveryCase, pruning_transport: PruningTransport
) -> None:
    saved = recovery.attempt
    admitted_at = saved.created_at + timedelta(hours=23, seconds=-1)
    recovery.clock.now = admitted_at
    pruning_transport.concurrent_replays = True
    pruning_transport.delays[3] = timedelta(seconds=2)

    def call() -> s.Authorization:
        real = service(recovery.path, recovery.real._deps.payments, recovery.clock)
        try:
            return real.authorize(recovery.case_id, BODY)
        finally:
            real._store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(call)
        assert pruning_transport.first_ready.wait(10)
        second = pool.submit(call)
        results = [first.result(10), second.result(10)]

    assert results[0] == results[1]
    assert pruning_transport.keys == [recovery.key] * 3
    assert pruning_transport.arrivals == [
        saved.created_at,
        admitted_at,
        admitted_at + timedelta(seconds=2),
    ]
    assert pruning_transport.arrivals[-1] - saved.created_at < timedelta(hours=24)
    assert_single_retained_hold(recovery, pruning_transport, results[0])


def call_paused_after_begin(
    case: RecoveryCase, payments: PaymentClient, begun: Event, resume: Event
) -> s.Authorization:
    real = service(case.path, payments, case.clock)
    begin = Mock(wraps=real._store.begin_authorization_attempt)

    def pause_begin(*args: object, **kwargs: object) -> AuthorizationAttempt:
        snapshot = begin(*args, **kwargs)
        assert isinstance(snapshot, AuthorizationAttempt)
        begun.set()
        assert resume.wait(10)
        return snapshot

    try:
        with pytest.MonkeyPatch.context() as hook:
            hook.setattr(real._store, "begin_authorization_attempt", pause_begin)
            return real.authorize(case.case_id, BODY)
    finally:
        real._store.close()


@pytest.mark.parametrize("known_reference", [False, True])
def test_round4_newer_retry_watermark_refuses_paused_dispatch(
    recovery: RecoveryCase, monkeypatch: pytest.MonkeyPatch, known_reference: bool
) -> None:
    payments = recovery.payments
    payments.lose_next_response = True
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    if known_reference:
        recovery.real._store.record_authorization_outcome(
            recovery.case_id,
            recovery.key,
            "uncertain",
            payments.object.authorization_ref,
            recovery.clock(),
        )
    original = recovery.attempt
    begun, resume = Event(), Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        paused = pool.submit(call_paused_after_begin, recovery, payments, begun, resume)
        try:
            assert begun.wait(10)
            recovery.clock.now = original.created_at + timedelta(seconds=2)
            if known_reference:
                retrieve = payments.retrieve

                def lost_retrieve(reference: str) -> NoReturn:
                    retrieve(reference)
                    raise TimeoutError("lost retrieve response")

                monkeypatch.setattr(payments, "retrieve", lost_retrieve)
            else:
                payments.lose_next_response = True
            with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
                recovery.real.authorize(recovery.case_id, BODY)
            saved = recovery.attempt
            assert saved.updated_at == recovery.clock()
            assert saved.revision > original.revision
            assert saved.state == "uncertain"
            assert saved.authorization_ref == original.authorization_ref
            calls = list(payments.calls)
            assert calls == [
                ("authorize", PM, 2999, recovery.key),
                (
                    ("retrieve", saved.authorization_ref, 2999, "")
                    if known_reference
                    else ("authorize", PM, 2999, recovery.key)
                ),
            ]
            recovery.clock.now -= timedelta(seconds=1)
        finally:
            resume.set()
        with pytest.raises(ServiceConflict, match=r"^authorization_outcome_unresolved$"):
            paused.result(timeout=10)

    assert payments.calls == calls
    assert len(payments.authorizations) == 1
    assert recovery.attempt == saved
    recovery.assert_no_case()


def test_round4_pruned_provider_key_never_gets_a_third_call(
    recovery: RecoveryCase, pruning_transport: PruningTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = recovery.attempt
    provider_clock = Clock()
    pruning_transport.clock = provider_clock
    begun, resume = Event(), Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        paused = pool.submit(
            call_paused_after_begin, recovery, recovery.real._deps.payments, begun, resume
        )
        try:
            assert begun.wait(10)
            provider_clock.now = original.created_at + timedelta(seconds=2)
            recovery.clock.now = provider_clock.now
            request = pruning_transport.request

            def lose_reply(
                method: str, url: str, headers: Mapping[str, str] | None, post_data: object = None
            ) -> NoReturn:
                request(method, url, headers, post_data)
                raise TimeoutError("lost retained replay response")

            monkeypatch.setattr(pruning_transport, "request", lose_reply)
            with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
                recovery.real.authorize(recovery.case_id, BODY)
            saved = recovery.attempt
            assert (saved.state, saved.authorization_ref) == ("uncertain", None)
            assert saved.updated_at == recovery.clock()
            assert saved.revision > original.revision
            provider_clock.now = original.created_at + timedelta(hours=24)
            recovery.clock.now -= timedelta(seconds=1)
        finally:
            resume.set()
        with pytest.raises(ServiceConflict, match=r"^authorization_outcome_unresolved$"):
            paused.result(timeout=10)

    assert pruning_transport.keys == [recovery.key] * 2
    assert pruning_transport.holds == ["pi_Hold1"]
    assert recovery.attempt == saved
    recovery.assert_no_case()


def test_round4_forward_claims_reuse_key_and_complete_once(
    recovery: RecoveryCase, pruning_transport: PruningTransport
) -> None:
    original = recovery.attempt
    begun, resume = Event(), Event()
    pruning_transport.concurrent_replays = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        paused = pool.submit(
            call_paused_after_begin, recovery, recovery.real._deps.payments, begun, resume
        )
        try:
            assert begun.wait(10)
            recovery.clock.now = original.created_at + timedelta(seconds=1)

            def call() -> s.Authorization:
                real = service(recovery.path, recovery.real._deps.payments, recovery.clock)
                try:
                    return real.authorize(recovery.case_id, BODY)
                finally:
                    real._store.close()

            retry = pool.submit(call)
            assert pruning_transport.first_ready.wait(10)
            recovery.clock.now += timedelta(seconds=1)
        finally:
            resume.set()
        results = [paused.result(timeout=10), retry.result(timeout=10)]

    assert results[0] == results[1]
    assert pruning_transport.keys == [recovery.key] * 3
    assert pruning_transport.arrivals == [
        original.created_at,
        original.created_at + timedelta(seconds=1),
        original.created_at + timedelta(seconds=2),
    ]
    assert_single_retained_hold(recovery, pruning_transport, results[0])


@pytest.mark.parametrize(
    "outcome", ["verified", "uncertain", "declined", "reference_changed", "exception"]
)
def test_newer_watermark_during_outcome_write_is_a_handled_conflict(
    recovery: RecoveryCase, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    store = recovery.real._store
    if outcome == "reference_changed":
        recovery.reserve()
        store.record_authorization_outcome(
            recovery.case_id, recovery.key, "uncertain", "auth_original", recovery.clock()
        )
        monkeypatch.setattr(
            recovery.payments,
            "retrieve",
            Mock(return_value=full_outcome(PaymentOutcome("requires_capture", "auth_other"))),
        )
    elif outcome == "exception":
        recovery.payments.lose_next_response = True
    elif outcome in {"declined", "uncertain"}:
        recovery.payments.next_response = full_outcome(PaymentOutcome(outcome, "auth_result"))
    record = Mock(wraps=store.record_authorization_outcome)
    watermarks: list[AuthorizationAttempt] = []

    def race_record(*args: object, **kwargs: object) -> AuthorizationAttempt:
        watermark = store.claim_authorization_dispatch(
            recovery.case_id,
            recovery.key,
            recovery.clock() + timedelta(seconds=2),
            AUTHORIZATION_REPLAY_LIMIT,
        )
        watermarks.append(watermark)
        result = record(*args, **kwargs)
        assert isinstance(result, AuthorizationAttempt)
        return result

    monkeypatch.setattr(store, "record_authorization_outcome", race_record)
    with pytest.raises(ServiceConflict, match=r"^authorization_outcome_unresolved$"):
        recovery.real.authorize(recovery.case_id, BODY)
    assert len(watermarks) == 1
    assert recovery.attempt == watermarks[0]
    assert recovery.attempt.state == ("uncertain" if outcome == "reference_changed" else "pending")
    assert not store._require_open().in_transaction
    recovery.assert_no_case()


def test_newer_watermark_during_completion_is_a_handled_conflict(
    recovery: RecoveryCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = recovery.real._store
    complete = Mock(wraps=store.complete_authorization_attempt)
    watermarks: list[AuthorizationAttempt] = []

    def race_complete(*args: object, **kwargs: object) -> CaseRecord:
        watermarks.append(
            store.claim_authorization_dispatch(
                recovery.case_id,
                recovery.key,
                recovery.clock() + timedelta(seconds=2),
                AUTHORIZATION_REPLAY_LIMIT,
            )
        )
        result = complete(*args, **kwargs)
        assert isinstance(result, CaseRecord)
        return result

    monkeypatch.setattr(store, "complete_authorization_attempt", race_complete)
    with pytest.raises(ServiceConflict, match=r"^authorization_outcome_unresolved$"):
        recovery.real.authorize(recovery.case_id, BODY)
    assert len(watermarks) == 1
    assert recovery.attempt == watermarks[0]
    assert recovery.attempt.state == "uncertain"
    assert recovery.attempt.authorization_ref == recovery.payments.object.authorization_ref
    assert not store._require_open().in_transaction
    recovery.assert_no_case()


@pytest.mark.parametrize("regress_at", [None, "record", "complete"])
def test_each_outcome_write_reads_a_fresh_clock(
    recovery: RecoveryCase, regress_at: str | None
) -> None:
    now = recovery.clock()
    record_at = now + timedelta(seconds=2)
    complete_at = now + timedelta(seconds=3)
    ticks = [now, now, now + timedelta(seconds=1), record_at, complete_at]
    if regress_at == "record":
        ticks[3] = now - timedelta(microseconds=1)
    elif regress_at == "complete":
        ticks[4] = now + timedelta(seconds=1)
    ticks.insert(2, now)
    clock = Mock(side_effect=ticks)
    recovery.real = CaseService(replace(recovery.real._deps, clock=clock))
    if regress_at is None:
        authorization = recovery.real.authorize(recovery.case_id, BODY)
        assert recovery.attempt.authorized_at == complete_at
        assert recovery.attempt.updated_at == complete_at
        recovery.assert_one_case(authorization)
    else:
        with pytest.raises(ServiceConflict, match=r"^authorization_outcome_unresolved$"):
            recovery.real.authorize(recovery.case_id, BODY)
        assert recovery.attempt.state == ("pending" if regress_at == "record" else "uncertain")
        assert recovery.attempt.updated_at == (now if regress_at == "record" else record_at)
        recovery.assert_no_case()
    assert clock.call_count == (5 if regress_at == "record" else 6)


def test_authorized_between_begin_and_claim_returns_existing_case(
    recovery: RecoveryCase,
) -> None:
    begun, resume = Event(), Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        paused = pool.submit(call_paused_after_begin, recovery, recovery.payments, begun, resume)
        try:
            assert begun.wait(10)
            authorization = recovery.real.authorize(recovery.case_id, BODY)
            saved = recovery.attempt
        finally:
            resume.set()
        assert paused.result(timeout=10) == authorization
    assert recovery.payments.calls == [("authorize", PM, 2999, recovery.key)]
    assert recovery.attempt == saved
    recovery.assert_one_case(authorization)
