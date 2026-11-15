"""Private runtime phase switch: persistent, idempotent, fail closed and enforced in place."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.api.customer_auth import AuthHttp
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.beta_store import BETA_DISCLOSURE_SHA256
from parkingbreaker.case_service import CaseService
from parkingbreaker.config import ProviderMode, Settings, build_services
from parkingbreaker.customer_access import AccessService
from parkingbreaker.intake_store import IntakeConflict
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.payment_records import mutation_key
from parkingbreaker.service_phase import ServicePhase
from parkingbreaker.service_phase_cli import main as cli_main
from parkingbreaker.service_phase_store import (
    ServicePhaseConflict,
    ServicePhaseStatus,
)
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_beta_admission import NOW as BETA_NOW
from test_beta_payment_boundary import prepare
from test_case_service import INTAKE, PNG_B64, Clock, service
from test_case_service import fake_resolution as fake_resolution
from test_customer_auth import ORIGIN, FakeRequester
from test_t14_authorization_recovery import BODY, PM, RecoveryCase
from test_t14_authorization_recovery import recovery as recovery

pytestmark = pytest.mark.anyio
SECRET = "worker-secret-0123456789"
PROCESSED_AT = datetime(2026, 11, 9, 18, tzinfo=UTC)
SWITCH_PM = "pm_runtime_switch"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def with_startup_phase(real: CaseService, phase: ServicePhase) -> CaseService:
    """The same durable data and providers, composed with another configured fallback."""
    return CaseService(replace(real._deps, service_phase=phase))


def submitted_service(
    path: Path, payments: FakePaymentClient, clock: Clock
) -> tuple[CaseService, str]:
    """One chosen but not authorized intake, through the existing service seams."""
    real = service(path, payments, clock)
    workspace = real.create_workspace("owner_runtime", s.CreateWorkspace(notes=""))
    photo = real.upload_photo(
        workspace.workspace_id,
        s.PhotoUpload(content_type="image/png", data_base64=PNG_B64),
    )
    receipt = real.submit(
        workspace.workspace_id,
        s.SubmitIntake.model_validate(
            {
                **INTAKE,
                "photo_refs": [photo.photo_ref],
                "citation_photo_ref": photo.photo_ref,
            }
        ),
    )
    real.choose(receipt.case_id, s.ChooseTier(tier=s.MailTier.CERTIFIED))
    return real, receipt.case_id


def authorization_identity(case_id: str, payment_method_id: str = SWITCH_PM) -> tuple[str, str]:
    key = mutation_key("authorization", case_id, "CERTIFIED:2999", payment_method_id)
    request_json = json.dumps(
        {
            "amount_cents": 2999,
            "currency": "usd",
            "metadata": {
                "case": case_id,
                "tier": "CERTIFIED",
                "service_amount_cents": "2999",
            },
            "payment_method_id": payment_method_id,
            "tier": "CERTIFIED",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return key, request_json


def test_no_override_preserves_the_exact_configured_startup_phase(tmp_path: Path) -> None:
    for phase in (ServicePhase.PAID, ServicePhase.CONTROLLED_FREE_BETA):
        path = tmp_path / f"{phase.value}.sqlite"
        with SqliteTransactionStore(path) as store:
            assert store.runtime_service_phase_status() == ServicePhaseStatus(
                None, None, None, None, None, None
            )
            assert store.effective_service_phase(phase) is phase
        real = with_startup_phase(
            service(path, FakePaymentClient(), Clock()), ServicePhase.CONTROLLED_FREE_BETA
        )
        try:
            assert real.service_phase is ServicePhase.CONTROLLED_FREE_BETA
        finally:
            real._store.close()


def test_override_persists_across_two_connections_and_a_reopen(tmp_path: Path) -> None:
    path = tmp_path / "two-connections.sqlite"
    writer = SqliteTransactionStore(path)
    reader = SqliteTransactionStore(path)
    try:
        assert reader.effective_service_phase(ServicePhase.PAID) is ServicePhase.PAID
        change = writer.set_runtime_service_phase(
            ServicePhase.CONTROLLED_FREE_BETA,
            0,
            "beta-on-1",
            "Amir",
            "Pause new paid authorizations",
            now=PROCESSED_AT,
        )
        assert change.revision == 1
        assert change.replayed is False
        # The second connection was opened before the write and now reads the override.
        assert reader.effective_service_phase(ServicePhase.PAID) is (
            ServicePhase.CONTROLLED_FREE_BETA
        )
        assert reader.runtime_service_phase_status() == ServicePhaseStatus(
            ServicePhase.CONTROLLED_FREE_BETA,
            1,
            "beta-on-1",
            "Amir",
            "Pause new paid authorizations",
            PROCESSED_AT,
        )
    finally:
        reader.close()
        writer.close()
    with SqliteTransactionStore(path) as reopened:
        assert reopened.effective_service_phase(ServicePhase.PAID) is (
            ServicePhase.CONTROLLED_FREE_BETA
        )


def test_command_replay_is_idempotent_and_a_changed_payload_refuses(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "replay.sqlite") as store:
        saved = store.set_runtime_service_phase(
            ServicePhase.CONTROLLED_FREE_BETA,
            0,
            "beta-on-1",
            "Amir",
            "Pause paid",
            now=PROCESSED_AT,
        )
        replay = store.set_runtime_service_phase(
            ServicePhase.CONTROLLED_FREE_BETA,
            0,
            "beta-on-1",
            "Amir",
            "Pause paid",
            now=PROCESSED_AT + timedelta(minutes=5),
        )
        assert replay.replayed is True
        assert replay.revision == saved.revision == 1
        assert replay.recorded_at == saved.recorded_at == PROCESSED_AT
        assert store.runtime_service_phase_status().revision == 1

        changed_payloads = (
            (ServicePhase.PAID, 0, "Amir", "Pause paid"),
            (ServicePhase.CONTROLLED_FREE_BETA, 1, "Amir", "Pause paid"),
            (ServicePhase.CONTROLLED_FREE_BETA, 0, "Someone else", "Pause paid"),
            (ServicePhase.CONTROLLED_FREE_BETA, 0, "Amir", "A different reason"),
        )
        for phase, expected_revision, actor, reason in changed_payloads:
            with pytest.raises(ServicePhaseConflict, match="command_id_payload_changed"):
                store.set_runtime_service_phase(
                    phase,
                    expected_revision,
                    "beta-on-1",
                    actor,
                    reason,
                    now=PROCESSED_AT,
                )
        assert store.runtime_service_phase_status().revision == 1
        assert store.effective_service_phase(ServicePhase.PAID) is (
            ServicePhase.CONTROLLED_FREE_BETA
        )


def test_stale_expected_revision_refuses_without_writing(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "stale.sqlite") as store:
        store.set_runtime_service_phase(
            ServicePhase.CONTROLLED_FREE_BETA,
            0,
            "beta-on-1",
            "Amir",
            "Pause paid",
            now=PROCESSED_AT,
        )
        for expected in (0, 2):
            with pytest.raises(ServicePhaseConflict, match="stale_expected_revision"):
                store.set_runtime_service_phase(
                    ServicePhase.PAID,
                    expected,
                    f"paid-on-{expected}",
                    "Amir",
                    "Resume paid",
                    now=PROCESSED_AT,
                )
        status = store.runtime_service_phase_status()
        assert status.revision == 1
        assert status.stored_override is ServicePhase.CONTROLLED_FREE_BETA
        next_change = store.set_runtime_service_phase(
            ServicePhase.PAID, 1, "paid-on-1", "Amir", "Resume paid", now=PROCESSED_AT
        )
        assert next_change.revision == 2
        assert store.effective_service_phase(ServicePhase.CONTROLLED_FREE_BETA) is (
            ServicePhase.PAID
        )


def test_malformed_runtime_phase_schema_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing-table.sqlite"
    with SqliteTransactionStore(missing):
        pass
    database = sqlite3.connect(missing)
    database.execute("DROP TABLE pb_runtime_service_phase")
    database.commit()
    database.close()
    with pytest.raises(StorageCorruption, match="runtime service phase"):
        SqliteTransactionStore(missing)

    unversioned = tmp_path / "unversioned.sqlite"
    with SqliteTransactionStore(unversioned):
        pass
    database = sqlite3.connect(unversioned)
    database.execute("DELETE FROM pb_store_versions WHERE component='runtime_service_phase'")
    database.commit()
    database.close()
    with pytest.raises(StorageCorruption, match="runtime service phase"):
        SqliteTransactionStore(unversioned)

    wrong_version = tmp_path / "wrong-version.sqlite"
    with SqliteTransactionStore(wrong_version):
        pass
    database = sqlite3.connect(wrong_version)
    database.execute(
        "UPDATE pb_store_versions SET version=2 WHERE component='runtime_service_phase'"
    )
    database.commit()
    database.close()
    with pytest.raises(StorageCorruption, match="runtime service phase"):
        SqliteTransactionStore(wrong_version)


def test_malformed_stored_phase_blocks_a_new_paid_intent(
    recovery: RecoveryCase,
) -> None:
    store = recovery.real._store
    store.set_runtime_service_phase(
        ServicePhase.PAID, 0, "paid-on-1", "Amir", "Resume paid", now=recovery.clock.now
    )
    database = store._require_open()
    database.execute("DROP TRIGGER pb_runtime_service_phase_no_update")
    database.execute("PRAGMA ignore_check_constraints=ON")
    database.execute("UPDATE pb_runtime_service_phase SET phase='free'")
    database.execute("PRAGMA ignore_check_constraints=OFF")

    with pytest.raises(StorageCorruption):
        _ = recovery.real.service_phase
    with pytest.raises(StorageCorruption):
        store.begin_authorization_attempt(
            recovery.case_id,
            recovery.key,
            "CERTIFIED",
            2999,
            PM,
            recovery.request_json,
            recovery.clock(),
            allow_new_paid_authorization=True,
        )
    assert database.execute(
        "SELECT COUNT(*) FROM pb_intake_authorization_attempts WHERE intake_id=?",
        (recovery.case_id,),
    ).fetchone() == (0,)


def test_runtime_paid_override_of_startup_beta_permits_a_new_intent(tmp_path: Path) -> None:
    clock = Clock()
    payments = FakePaymentClient()
    real, case_id = submitted_service(tmp_path / "resume.sqlite", payments, clock)
    beta = with_startup_phase(real, ServicePhase.CONTROLLED_FREE_BETA)
    try:
        assert beta.service_phase.value == "controlled_free_beta"
        with pytest.raises(ServiceConflict, match=r"^paid_orders_paused$"):
            beta.authorize(case_id, BODY)
        real._store.set_runtime_service_phase(
            ServicePhase.PAID, 0, "paid-on-1", "Amir", "Resume paid", now=clock.now
        )
        assert beta.service_phase.value == "paid"
        authorization = beta.authorize(case_id, BODY)
        assert authorization.status == "authorized"
        assert authorization.amount_cents == 2999
        assert payments.calls == [
            (
                "authorize",
                BODY.payment_method_id,
                2999,
                mutation_key("authorization", case_id, "CERTIFIED:2999", BODY.payment_method_id),
            )
        ]
    finally:
        beta._store.close()


def test_stale_pre_transaction_paid_boolean_is_beaten_by_beta_on(tmp_path: Path) -> None:
    path = tmp_path / "switch.sqlite"
    clock = Clock()
    real, case_id = submitted_service(path, FakePaymentClient(), clock)
    key, request_json = authorization_identity(case_id)
    operator = SqliteTransactionStore(path)
    try:
        operator.set_runtime_service_phase(
            ServicePhase.CONTROLLED_FREE_BETA,
            0,
            "beta-on-1",
            "Amir",
            "Pause new paid authorizations",
            now=clock.now,
        )
        # The caller resolved PAID before the commit and still passes True.
        with pytest.raises(IntakeConflict, match=r"^paid_orders_paused$"):
            real._store.begin_authorization_attempt(
                case_id,
                key,
                "CERTIFIED",
                2999,
                SWITCH_PM,
                request_json,
                clock.now,
                allow_new_paid_authorization=True,
            )
        assert real._store._require_open().execute(
            "SELECT COUNT(*) FROM pb_intake_authorization_attempts WHERE intake_id=?",
            (case_id,),
        ).fetchone() == (0,)

        # The opposite stale boolean loses to a committed paid override too.
        operator.set_runtime_service_phase(
            ServicePhase.PAID, 1, "paid-on-1", "Amir", "Resume paid", now=clock.now
        )
        attempt = real._store.begin_authorization_attempt(
            case_id,
            key,
            "CERTIFIED",
            2999,
            SWITCH_PM,
            request_json,
            clock.now,
            allow_new_paid_authorization=False,
        )
        assert attempt.state == "pending"
        assert attempt.intake_id == case_id
    finally:
        operator.close()
        real._store.close()


def test_selected_beta_stays_free_under_a_paid_runtime_override(tmp_path: Path) -> None:
    services = build_services(
        Settings(
            ProviderMode.FAKE,
            tmp_path,
            SECRET,
            {},
            ServicePhase.CONTROLLED_FREE_BETA,
        )
    )
    assert isinstance(services, CaseService)
    store = services._store
    try:
        prepare(store)
        store.select_beta_batch("beta-1", "batch-1", "approval-1", BETA_NOW)
        store.start_beta_case("case_1", BETA_DISCLOSURE_SHA256, BETA_NOW)
        store.set_runtime_service_phase(
            ServicePhase.PAID, 0, "paid-on-1", "Amir", "Resume paid", now=BETA_NOW
        )
        assert services.service_phase is ServicePhase.PAID
        before = tuple(store._require_open().iterdump())
        with pytest.raises(ServiceConflict, match=r"^beta_no_payment_required$"):
            services.authorize(
                "case_1",
                s.StartAuthorization(terms_accepted=True, payment_method_id="pm_fixture"),
            )
        assert tuple(store._require_open().iterdump()) == before
    finally:
        store.close()


def test_existing_uncertain_intent_recovery_survives_a_switch(recovery: RecoveryCase) -> None:
    payments = recovery.payments
    payments.lose_next_response = True
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    saved = recovery.attempt
    assert saved.state == "uncertain"
    assert saved.authorization_ref is None
    calls = list(payments.calls)

    recovery.real._store.set_runtime_service_phase(
        ServicePhase.CONTROLLED_FREE_BETA,
        0,
        "beta-on-1",
        "Amir",
        "Pause new paid authorizations",
        now=recovery.clock.now,
    )
    assert recovery.real.service_phase is ServicePhase.CONTROLLED_FREE_BETA
    authorization = recovery.real.authorize(recovery.case_id, BODY)
    assert payments.calls == [
        *calls,
        ("authorize", BODY.payment_method_id, 2999, recovery.key),
    ]
    assert len(payments.authorizations) == 1
    recovery.assert_one_case(authorization)
    assert payments.captures == 0
    assert payments.releases == 0


async def test_pricing_reports_the_runtime_phase_without_recreating_the_app(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pricing.sqlite"
    with SqliteTransactionStore(path) as store:
        services = build_services(
            Settings(
                ProviderMode.FAKE,
                tmp_path,
                SECRET,
                {},
                ServicePhase.CONTROLLED_FREE_BETA,
            ),
            store=store,
        )
        assert isinstance(services, CaseService)
        app = create_app(
            services,
            worker_secret=SECRET,
            customer_auth=AuthHttp(
                AccessService(store, clock=lambda: PROCESSED_AT),
                FakeRequester(),
                origin=ORIGIN,
                clock=lambda: PROCESSED_AT,
            ),
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
            first = await client.post("/auth/pricing", headers={"Origin": ORIGIN}, json={})
            assert first.status_code == 200
            assert first.json()["service_phase"] == "controlled_free_beta"
            # The same app and the same store now report the operator's paid override.
            store.set_runtime_service_phase(
                ServicePhase.PAID, 0, "paid-on-1", "Amir", "Resume paid", now=PROCESSED_AT
            )
            second = await client.post("/auth/pricing", headers={"Origin": ORIGIN}, json={})
            assert second.status_code == 200
            assert second.json()["service_phase"] == "paid"
            assert second.json()["amount_cents"] == first.json()["amount_cents"]
            store.set_runtime_service_phase(
                ServicePhase.CONTROLLED_FREE_BETA,
                1,
                "beta-on-1",
                "Amir",
                "Pause new paid authorizations",
                now=PROCESSED_AT,
            )
            third = await client.post("/auth/pricing", headers={"Origin": ORIGIN}, json={})
            assert third.json()["service_phase"] == "controlled_free_beta"


def test_cli_reports_honestly_and_refuses_a_missing_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.sqlite"
    assert cli_main(["--database", str(missing), "status"]) == 2
    assert not missing.exists()
    assert json.loads(capsys.readouterr().err)["error"] == "database_missing"

    path = tmp_path / "cli.sqlite"
    with SqliteTransactionStore(path):
        pass
    assert cli_main(["--database", str(path), "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["stored_override"] is None
    assert status["revision"] is None
    assert status["effective_phase"] is None
    assert status["effective_source"] == "configured_fallback"

    assert (
        cli_main(
            [
                "--database",
                str(path),
                "set",
                "--phase",
                "controlled_free_beta",
                "--expected-revision",
                "0",
                "--command-id",
                "beta-on-cli",
                "--actor",
                "Amir",
                "--reason",
                "Pause new paid authorizations",
            ]
        )
        == 0
    )
    saved = json.loads(capsys.readouterr().out)
    assert saved["revision"] == 1
    assert saved["phase"] == "controlled_free_beta"
    assert saved["replayed"] is False

    assert cli_main(["--database", str(path), "status"]) == 0
    stored = json.loads(capsys.readouterr().out)
    assert stored["stored_override"] == "controlled_free_beta"
    assert stored["revision"] == 1
    assert stored["effective_source"] == "stored_override"

    # A stale expected revision is a clear nonzero failure and writes nothing.
    assert (
        cli_main(
            [
                "--database",
                str(path),
                "set",
                "--phase",
                "paid",
                "--expected-revision",
                "0",
                "--command-id",
                "paid-on-cli",
                "--actor",
                "Amir",
                "--reason",
                "Resume paid",
            ]
        )
        == 2
    )
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "stale_expected_revision"
    with SqliteTransactionStore(path) as store:
        assert store.runtime_service_phase_status().revision == 1
