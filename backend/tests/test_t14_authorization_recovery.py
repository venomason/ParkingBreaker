"""T14 authorization recovery through CaseService, real SQLite, and fake payments."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

import pytest

from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.case_service import CaseService
from parkingbreaker.intake_store import AuthorizationAttempt
from parkingbreaker.payment_fakes import FakePaymentClient, full_outcome
from parkingbreaker.payment_records import PaymentOutcome, mutation_key
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_case_service import INTAKE, PNG_B64, Clock, service
from test_case_service import fake_resolution as fake_resolution

PM = "pm_recovery_original"
BODY = s.StartAuthorization(payment_method_id=PM, terms_accepted=True)


def assert_dispatch_claim(observed: AuthorizationAttempt, saved: AuthorizationAttempt) -> None:
    # Identity and outcome fields stay identical; only the dispatch watermark changes.
    assert observed == replace(saved, updated_at=observed.updated_at, revision=observed.revision)
    assert observed.updated_at >= saved.updated_at
    assert observed.revision == saved.revision + 1


class RecoveryPayments(FakePaymentClient):
    """Keep the real fake's idempotency cache, with one-shot response faults."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.requests: list[tuple[int, str, str, dict[str, str]]] = []
        self.observed_attempts: list[AuthorizationAttempt] = []
        self.lose_next_response = False
        self.next_response: PaymentOutcome | None = None

    def authorize(
        self, amount_cents: int, payment_method_id: str, key: str, metadata: Mapping[str, str]
    ) -> PaymentOutcome:
        self.requests.append((amount_cents, payment_method_id, key, dict(metadata)))
        # A separate connection must see the committed intent before the provider call.
        observer = SqliteTransactionStore(self.path)
        try:
            attempt = observer.get_authorization_attempt(metadata["case"], key)
            assert attempt is not None
            self.observed_attempts.append(attempt)
        finally:
            observer.close()
        result = super().authorize(amount_cents, payment_method_id, key, metadata)
        if self.lose_next_response:
            self.lose_next_response = False
            raise RuntimeError("synthetic response lost after creating the hold")
        if self.next_response is not None:
            response, self.next_response = self.next_response, None
            if response.status != "uncertain":
                self.object = response
                self.authorizations[key] = (
                    amount_cents,
                    payment_method_id,
                    dict(metadata),
                    response,
                )
            return response
        return result


@dataclass
class RecoveryCase:
    real: CaseService
    payments: RecoveryPayments
    clock: Clock
    path: Path
    case_id: str

    @property
    def key(self) -> str:
        return mutation_key("authorization", self.case_id, "CERTIFIED:2999", PM)

    @property
    def metadata(self) -> dict[str, str]:
        return {"case": self.case_id, "tier": "CERTIFIED", "service_amount_cents": "2999"}

    @property
    def request_json(self) -> str:
        return json.dumps(
            {
                "amount_cents": 2999,
                "currency": "usd",
                "metadata": self.metadata,
                "payment_method_id": PM,
                "tier": "CERTIFIED",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def attempt(self) -> AuthorizationAttempt:
        saved = self.real._store.get_authorization_attempt(self.case_id, self.key)
        assert saved is not None
        return saved

    def reserve(self) -> AuthorizationAttempt:
        return self.real._store.begin_authorization_attempt(
            self.case_id, self.key, "CERTIFIED", 2999, PM, self.request_json, self.clock()
        )

    def restart(self) -> None:
        self.real._store.close()
        self.real = service(self.path, self.payments, self.clock)

    def assert_no_case(self) -> None:
        intake = self.real._store.get_intake(self.case_id)
        assert intake is not None
        assert intake.case_token is None
        assert self.real._store._require_open().execute(
            "SELECT COUNT(*) FROM pb_cases"
        ).fetchone() == (0,)

    def assert_one_case(self, authorization: s.Authorization) -> None:
        assert authorization.status == "authorized"
        assert authorization.amount_cents == 2999
        assert self.attempt.state == "authorized"
        assert self.attempt.authorization_ref == authorization.payment_intent_id
        intake = self.real._store.get_intake(self.case_id)
        assert intake is not None
        assert intake.case_token is not None
        saved = self.real._store.get_case(intake.case_token)
        assert saved is not None
        assert saved.authorization_ref == authorization.payment_intent_id
        assert saved.amount_cents == 2999
        assert saved.workspace_token == intake.workspace_token
        assert self.real._store._require_open().execute(
            "SELECT COUNT(*) FROM pb_cases"
        ).fetchone() == (1,)
        assert self.real._store._require_open().execute(
            "SELECT COUNT(*) FROM pb_intake_authorization_attempts"
        ).fetchone() == (1,)
        assert len(self.payments.authorizations) == 1


@pytest.fixture
def recovery(tmp_path: Path) -> Iterator[RecoveryCase]:
    path = tmp_path / "recovery.sqlite"
    clock = Clock()
    payments = RecoveryPayments(path)
    real = service(path, payments, clock)
    case: RecoveryCase | None = None
    try:
        workspace = real.create_workspace("owner_recovery", s.CreateWorkspace(notes=""))
        photo = real.upload_photo(
            workspace.workspace_id,
            s.PhotoUpload(content_type="image/png", data_base64=PNG_B64),
        )
        body = s.SubmitIntake.model_validate(
            {**INTAKE, "photo_refs": [photo.photo_ref], "citation_photo_ref": photo.photo_ref}
        )
        receipt = real.submit(workspace.workspace_id, body)
        real.choose(receipt.case_id, s.ChooseTier(tier=s.MailTier.CERTIFIED))
        case = RecoveryCase(real, payments, clock, path, receipt.case_id)
        yield case
    finally:
        (real if case is None else case.real)._store.close()


@pytest.mark.parametrize("elapsed", [timedelta(), timedelta(hours=25)])
def test_known_reference_retry_retrieves_and_completes_once(
    recovery: RecoveryCase, elapsed: timedelta
) -> None:
    payments = recovery.payments
    payments.next_response = PaymentOutcome("uncertain", payments.object.authorization_ref)
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    saved = recovery.attempt
    assert saved.state == "uncertain"
    assert saved.authorization_ref == payments.object.authorization_ref
    recovery.assert_no_case()

    recovery.restart()
    recovery.clock.now += elapsed
    authorization = recovery.real.authorize(recovery.case_id, BODY)
    calls = list(payments.calls)
    assert calls == [
        ("authorize", PM, 2999, recovery.key),
        ("retrieve", saved.authorization_ref, 2999, ""),
    ]
    recovery.assert_one_case(authorization)
    assert recovery.real.authorize(recovery.case_id, BODY) == authorization
    assert payments.calls == calls
    recovery.assert_one_case(authorization)


@pytest.mark.parametrize("elapsed", [timedelta(hours=23), timedelta(hours=24), timedelta(hours=25)])
def test_unknown_reference_at_or_after_replay_window_never_calls_provider(
    recovery: RecoveryCase, elapsed: timedelta
) -> None:
    recovery.payments.lose_next_response = True
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    saved = recovery.attempt
    assert saved.state == "uncertain"
    assert saved.authorization_ref is None
    calls = list(recovery.payments.calls)
    assert calls == [("authorize", PM, 2999, recovery.key)]
    recovery.restart()
    recovery.clock.now = saved.created_at + elapsed

    with pytest.raises(ServiceConflict, match=r"^authorization_outcome_unresolved$"):
        recovery.real.authorize(recovery.case_id, BODY)
    assert recovery.payments.calls == calls
    assert recovery.attempt == saved
    assert len(recovery.payments.authorizations) == 1
    recovery.assert_no_case()


def test_unknown_reference_one_second_before_window_replays_exact_request(
    recovery: RecoveryCase,
) -> None:
    recovery.payments.lose_next_response = True
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    saved = recovery.attempt
    assert saved.state == "uncertain"
    assert saved.authorization_ref is None
    recovery.restart()
    recovery.clock.now = saved.created_at + timedelta(hours=23, seconds=-1)

    authorization = recovery.real.authorize(recovery.case_id, BODY)
    assert recovery.payments.requests == [(2999, PM, recovery.key, recovery.metadata)] * 2
    assert recovery.payments.calls == [("authorize", PM, 2999, recovery.key)] * 2
    assert_dispatch_claim(recovery.payments.observed_attempts[-1], saved)
    assert recovery.payments.authorizations[recovery.key][:3] == (
        2999,
        PM,
        recovery.metadata,
    )
    recovery.assert_one_case(authorization)


def test_definitive_decline_is_final_without_another_provider_call(recovery: RecoveryCase) -> None:
    recovery.payments.next_response = full_outcome(PaymentOutcome("declined", "auth_declined"))
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    saved = recovery.attempt
    assert saved.state == "declined"
    assert saved.authorization_ref == "auth_declined"
    calls = list(recovery.payments.calls)
    assert calls == [("authorize", PM, 2999, recovery.key)]
    recovery.restart()

    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    assert recovery.payments.calls == calls
    assert recovery.attempt == saved
    recovery.assert_no_case()


def test_response_loss_records_uncertainty_and_reuses_the_single_hold(
    recovery: RecoveryCase,
) -> None:
    recovery.payments.lose_next_response = True
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    saved = recovery.attempt
    assert saved.state == "uncertain"
    assert saved.authorization_ref is None
    assert saved.idempotency_key == recovery.key
    original_hold = recovery.payments.authorizations[recovery.key][3]
    assert original_hold.status == "requires_capture"
    recovery.assert_no_case()
    recovery.restart()

    authorization = recovery.real.authorize(recovery.case_id, BODY)
    assert authorization.payment_intent_id == original_hold.authorization_ref
    expected_request = (2999, PM, recovery.key, recovery.metadata)
    assert recovery.payments.requests == [expected_request, expected_request]
    assert recovery.payments.calls == [("authorize", PM, 2999, recovery.key)] * 2
    assert recovery.payments.authorizations[recovery.key][3] is original_hold
    recovery.assert_one_case(authorization)
    assert recovery.real.authorize(recovery.case_id, BODY) == authorization
    assert len(recovery.payments.calls) == 2
    recovery.assert_one_case(authorization)


def test_clock_before_creation_refuses_without_calling_provider(recovery: RecoveryCase) -> None:
    saved = recovery.reserve()
    recovery.restart()
    recovery.clock.now = saved.created_at - timedelta(seconds=1)

    with pytest.raises(ServiceConflict, match=r"^authorization_outcome_unresolved$"):
        recovery.real.authorize(recovery.case_id, BODY)
    assert recovery.payments.calls == []
    assert recovery.payments.requests == []
    assert recovery.payments.authorizations == {}
    assert recovery.attempt == saved
    recovery.assert_no_case()


@pytest.mark.parametrize("remaining", [timedelta(), timedelta(seconds=-1)])
def test_nonfuture_hold_expiry_cannot_complete_a_case(
    recovery: RecoveryCase, remaining: timedelta
) -> None:
    expires = recovery.clock() + remaining
    recovery.payments.next_response = replace(
        recovery.payments.object,
        evidence_version=1,
        original_charge_ref="ch_recovery",
        capture_before=int(expires.timestamp()),
    )
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    assert recovery.payments.calls == [("authorize", PM, 2999, recovery.key)]
    recovery.restart()
    saved = recovery.attempt
    assert saved.state == "uncertain"
    assert saved.authorization_ref == recovery.payments.object.authorization_ref
    assert saved.authorized_at is None
    assert saved.authorization_expires_at is None
    recovery.assert_no_case()


@pytest.mark.parametrize("state", ["pending", "uncertain"])
def test_persisted_identity_precedes_provider_and_rejects_a_different_card(
    recovery: RecoveryCase, state: str
) -> None:
    if state == "pending":
        original = recovery.reserve()
    else:
        recovery.payments.lose_next_response = True
        with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
            recovery.real.authorize(recovery.case_id, BODY)
        assert len(recovery.payments.observed_attempts) == 1
        original = recovery.payments.observed_attempts[0]
        assert recovery.payments.requests == [(2999, PM, recovery.key, recovery.metadata)]
    assert original.state == "pending"
    assert (original.tier, original.amount_cents, original.payment_method_id) == (
        "CERTIFIED",
        2999,
        PM,
    )
    assert original.request_json == recovery.request_json
    recovery.restart()
    saved = recovery.attempt
    assert saved.state == state
    assert (saved.tier, saved.amount_cents, saved.payment_method_id) == ("CERTIFIED", 2999, PM)
    assert saved.idempotency_key == recovery.key
    assert saved.request_json == recovery.request_json
    assert json.loads(saved.request_json)["metadata"] == recovery.metadata
    calls = list(recovery.payments.calls)
    hold_count = len(recovery.payments.authorizations)

    with pytest.raises(ServiceConflict, match=r"^authorization_outcome_unresolved$"):
        recovery.real.authorize(
            recovery.case_id,
            s.StartAuthorization(payment_method_id="pm_different", terms_accepted=True),
        )
    assert recovery.payments.calls == calls
    assert len(recovery.payments.authorizations) == hold_count
    assert recovery.attempt == saved
    assert recovery.real._store._require_open().execute(
        "SELECT COUNT(*) FROM pb_intake_authorization_attempts"
    ).fetchone() == (1,)
    recovery.assert_no_case()

    authorization = recovery.real.authorize(recovery.case_id, BODY)
    observed = recovery.payments.observed_attempts[-1]
    assert_dispatch_claim(observed, saved)
    assert (observed.tier, observed.amount_cents, observed.payment_method_id) == (
        "CERTIFIED",
        2999,
        PM,
    )
    assert observed.request_json == recovery.request_json
    assert recovery.payments.requests[-1] == (
        observed.amount_cents,
        observed.payment_method_id,
        observed.idempotency_key,
        json.loads(observed.request_json)["metadata"],
    )
    recovery.assert_one_case(authorization)
