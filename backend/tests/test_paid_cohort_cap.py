"""Paid beta capacity is reserved before payment and survives recovery and restart."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest

from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.beta_store import BETA_DISCLOSURE_SHA256
from parkingbreaker.case_service import CaseService
from parkingbreaker.config import ConfigError, ProviderMode, Settings, build_services, load_settings
from parkingbreaker.intake_store import IntakeConflict
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_beta_admission import NOW as BETA_NOW
from test_beta_payment_boundary import prepare
from test_case_service import INTAKE, PNG_B64, Clock, service
from test_case_service import fake_resolution as fake_resolution
from test_runtime_service_phase import authorization_identity
from test_service_phase import live_values
from test_t14_authorization_recovery import BODY, RecoveryCase
from test_t14_authorization_recovery import recovery as recovery


def prepare_intake(real: CaseService, owner: str) -> str:
    workspace = real.create_workspace(owner, s.CreateWorkspace(notes=""))
    photo = real.upload_photo(
        workspace.workspace_id, s.PhotoUpload(content_type="image/png", data_base64=PNG_B64)
    )
    return real.submit(
        workspace.workspace_id,
        s.SubmitIntake.model_validate(
            {**INTAKE, "photo_refs": [photo.photo_ref], "citation_photo_ref": photo.photo_ref}
        ),
    ).case_id


@pytest.mark.parametrize("raw", ["", "-1", "+1", "1.5", "1e4", " 1", chr(0xFF11), str(2**63)])
def test_invalid_cap_refuses_before_storage(tmp_path: Path, raw: str) -> None:
    with pytest.raises(ConfigError, match="PB_PAID_COHORT_CAP"):
        load_settings(
            live_values() | {"PB_SERVICE_PHASE": "paid", "PB_PAID_COHORT_CAP": raw},
            tmp_path / "missing.env",
        )
    assert not list(tmp_path.iterdir())


def test_configuration_defaults_and_durable_mode(tmp_path: Path) -> None:
    from parkingbreaker.config import _paid_cohort_cap

    assert _paid_cohort_cap(ProviderMode.LIVE, {}) == 0
    assert _paid_cohort_cap(ProviderMode.TEST, {}) is None
    assert _paid_cohort_cap(ProviderMode.LIVE, {"PB_PAID_COHORT_CAP": "0"}) == 0
    with pytest.raises(ConfigError, match="durable"):
        load_settings({"PB_PAID_COHORT_CAP": "2"}, tmp_path / "missing.env")
    real = build_services(Settings(ProviderMode.FAKE, tmp_path, "", {"PB_PAID_COHORT_CAP": "2"}))
    assert isinstance(real, CaseService)
    try:
        assert real._deps.paid_cohort_cap == 2
    finally:
        real._store.close()


def test_last_place_serializes_two_connections_then_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "cohort.sqlite"
    clock = Clock()
    payments = FakePaymentClient()
    base = service(path, payments, clock)
    real = CaseService(replace(base._deps, paid_cohort_cap=1))
    try:
        intakes = [prepare_intake(real, owner) for owner in ("driver-one", "driver-two")]
        for intake in intakes:
            real.choose(intake, s.ChooseTier(tier=s.MailTier.CERTIFIED))
        # Each customer has already seen a valid offer. Race the actual durable boundary.
        ready = Barrier(2)

        def contend(intake: str) -> tuple[str, str]:
            with SqliteTransactionStore(path) as store:
                key, request = authorization_identity(intake, BODY.payment_method_id)
                ready.wait(timeout=10)
                try:
                    attempt = store.begin_authorization_attempt(
                        intake,
                        key,
                        "CERTIFIED",
                        2999,
                        BODY.payment_method_id,
                        request,
                        clock(),
                        paid_cohort_cap=1,
                    )
                except IntakeConflict as exc:
                    return intake, str(exc)
                return intake, attempt.state

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = {outcome: intake for intake, outcome in pool.map(contend, intakes)}
        assert sorted(outcomes) == ["paid_cohort_full", "pending"]
    finally:
        real._store.close()

    restarted = service(path, payments, clock)
    real = CaseService(replace(restarted._deps, paid_cohort_cap=1))
    try:
        refused = outcomes["paid_cohort_full"]
        stop = real.pre_hold(refused)
        assert isinstance(stop, s.Stop)
        assert stop.reason_key == "paid_cohort_full"
        assert stop.work_preserved
        assert stop.no_charge
        assert stop.official_url
        assert real.status(refused).stop == stop
        with pytest.raises(ServiceConflict, match=r"^paid_cohort_full$"):
            real.choose(refused, s.ChooseTier(tier=s.MailTier.CERTIFIED))
        with pytest.raises(ServiceConflict, match=r"^paid_cohort_full$"):
            real.authorize(refused, BODY)
        assert payments.calls == []
        admitted = outcomes["pending"]
        auth = real.authorize(admitted, BODY)
        assert auth.status == "authorized"
        assert real.authorize(admitted, BODY) == auth
        assert len(payments.authorizations) == 1
        assert real._store.paid_cohort_full(refused, 1)
        assert not real._store.paid_cohort_full(refused, 2)
    finally:
        real._store.close()


def test_decline_keeps_one_place_and_allows_new_card_for_same_intake(
    recovery: RecoveryCase,
) -> None:
    real = CaseService(replace(recovery.real._deps, paid_cohort_cap=1))
    attempt = recovery.reserve()
    real._store.record_authorization_outcome(
        recovery.case_id, attempt.idempotency_key, "declined", None, recovery.clock()
    )
    assert real._store.paid_cohort_full("another-intake", 1)
    assert isinstance(real.pre_hold(recovery.case_id), s.Offer)
    new_card = BODY.model_copy(update={"payment_method_id": "pm_replacement"})
    assert real.authorize(recovery.case_id, new_card).status == "authorized"
    assert not real._store.paid_cohort_full("another-intake", 2)


def test_existing_paid_cases_and_free_entitlements_have_distinct_accounting(tmp_path: Path) -> None:
    clock = Clock()
    real = service(tmp_path / "history.sqlite", FakePaymentClient(), clock)
    try:
        store = real._store
        prepare(store)
        store.select_beta_batch("beta-1", "batch-1", "approval-1", BETA_NOW)
        store.start_beta_case("case_1", BETA_DISCLOSURE_SHA256, BETA_NOW)
        assert not store.paid_cohort_full("new-intake", 1)
        workspace = real.create_workspace("older-driver", s.CreateWorkspace(notes=""))
        store.start_case(
            workspace.workspace_id, "legacy-hold", clock(), clock() + timedelta(days=5)
        )
        assert store.paid_cohort_full("new-intake", 1)
        assert not store.paid_cohort_full("new-intake", 2)
    finally:
        real._store.close()
