"""Controlled free beta: new paid intents pause, every existing money path keeps working."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.api.customer_auth import AuthHttp
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.case_service import CaseService
from parkingbreaker.config import ConfigError, ProviderMode, Settings, build_services, load_settings
from parkingbreaker.customer_access import AccessService
from parkingbreaker.payment_fakes import FakePaymentClient, full_outcome
from parkingbreaker.payment_records import PaymentOutcome
from parkingbreaker.service_phase import ServicePhase, parse_service_phase
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_case_service import INTAKE, PNG_B64, Clock, service
from test_case_service import fake_resolution as fake_resolution
from test_customer_auth import ORIGIN, FakeRequester
from test_t14_authorization_recovery import BODY, RecoveryCase
from test_t14_authorization_recovery import recovery as recovery

pytestmark = pytest.mark.anyio
ALICE = {"Authorization": "Bearer fake-alice"}
BOB = {"Authorization": "Bearer fake-bob"}
CARD = {"terms_accepted": True, "payment_method_id": "pm_card_visa"}
SECRET = "worker-secret-0123456789"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def controlled(real: CaseService) -> CaseService:
    """The same durable data and providers, composed with the free-beta phase."""
    return CaseService(replace(real._deps, service_phase=ServicePhase.CONTROLLED_FREE_BETA))


def live_values() -> dict[str, str]:
    return {
        "PB_PROVIDER_MODE": "live",
        "PB_WORKER_SECRET": SECRET,
        "PB_ALLOW_LIVE": "1",
        "STRIPE_SECRET_KEY": "sk_live_synthetic",
        "LOB_API_KEY": "live_synthetic",
        "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": "pk_live_synthetic",
        "STRIPE_WEBHOOK_SECRET": "whsec_synthetic",
        "LOB_WEBHOOK_SECRET": "lob-synthetic-secret",
    }


def test_service_phase_values_are_exact_and_parsed_without_guessing() -> None:
    assert ServicePhase.PAID.value == "paid"
    assert ServicePhase.CONTROLLED_FREE_BETA.value == "controlled_free_beta"
    assert parse_service_phase("paid") is ServicePhase.PAID
    assert parse_service_phase("controlled_free_beta") is ServicePhase.CONTROLLED_FREE_BETA
    for bad in ("", " paid", "PAID", "controlled_free_development_beta", "free"):
        with pytest.raises(ValueError, match="service phase"):
            parse_service_phase(bad)


@pytest.mark.parametrize("phase", ["paid", "controlled_free_beta"])
def test_live_requires_an_explicit_known_phase_before_any_data(tmp_path: Path, phase: str) -> None:
    values = live_values() | {"PB_SERVICE_PHASE": phase}
    settings = load_settings(values, tmp_path / "missing.env")
    assert settings.service_phase is ServicePhase(phase)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("phase", [None, "", "   ", "bogus"])
def test_live_missing_blank_or_unknown_phase_fails_before_building_anything(
    tmp_path: Path, phase: str | None
) -> None:
    values = live_values()
    if phase is not None:
        values["PB_SERVICE_PHASE"] = phase
    with pytest.raises(ConfigError, match="PB_SERVICE_PHASE") as error:
        load_settings(values, tmp_path / "missing.env")
    assert "synthetic" not in str(error.value)
    assert not list(tmp_path.iterdir())


def test_programmatic_live_settings_cannot_bypass_the_phase_requirement(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="PB_SERVICE_PHASE"):
        build_services(Settings(ProviderMode.LIVE, tmp_path, SECRET, {}))
    assert not list(tmp_path.iterdir())
    with pytest.raises(ConfigError, match="PB_SERVICE_PHASE"):
        build_services(Settings(ProviderMode.LIVE, tmp_path, SECRET, {}, "bogus"))  # type: ignore[arg-type]
    assert not list(tmp_path.iterdir())


def test_fake_defaults_paid_and_explicit_free_beta_is_style_shared(tmp_path: Path) -> None:
    paid = build_services(Settings(ProviderMode.FAKE, tmp_path / "paid", SECRET, {}))
    assert paid is not None
    assert paid.service_phase is ServicePhase.PAID
    free = build_services(
        Settings(
            ProviderMode.FAKE,
            tmp_path / "free",
            SECRET,
            {},
            ServicePhase.CONTROLLED_FREE_BETA,
        )
    )
    assert free is not None
    assert free.service_phase is ServicePhase.CONTROLLED_FREE_BETA
    # Preview fakes expose the same authoritative property and paid default.
    from parkingbreaker.api.fakes import FakeServices

    assert FakeServices().service_phase is ServicePhase.PAID
    assert FakeServices(service_phase=ServicePhase.CONTROLLED_FREE_BETA).service_phase is (
        ServicePhase.CONTROLLED_FREE_BETA
    )


@pytest.mark.parametrize(
    ("phase", "expected_status", "expected_body"),
    [
        (ServicePhase.CONTROLLED_FREE_BETA, 409, {"detail": "paid_orders_paused"}),
        (None, 200, None),
    ],
)
async def test_environment_preview_factory_keeps_the_resolved_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: ServicePhase | None,
    expected_status: int,
    expected_body: dict[str, str] | None,
) -> None:
    """The real environment factory must pass the configured phase to the preview fake."""
    import parkingbreaker.config as config
    from parkingbreaker.api.app import app_from_environment

    settings = Settings(ProviderMode.PREVIEW, tmp_path, "fake-worker", {}, phase)
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    app = app_from_environment()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://preview.test"
    ) as client:
        workspace = (await client.post("/workspaces", headers=ALICE, json={"notes": ""})).json()[
            "workspace_id"
        ]
        uploaded = await client.post(
            f"/workspaces/{workspace}/photos",
            headers=ALICE,
            json={"content_type": "image/png", "data_base64": PNG_B64},
        )
        ref = uploaded.json()["photo_ref"]
        body = {
            **INTAKE,
            "ticket": {**INTAKE["ticket"], "issue_date": datetime.now(UTC).date().isoformat()},
            "photo_refs": [ref],
            "citation_photo_ref": ref,
        }
        case = (
            await client.post(f"/workspaces/{workspace}/intake", headers=ALICE, json=body)
        ).json()["case_id"]
        assert (await client.get(f"/cases/{case}/pre-hold", headers=ALICE)).json()["kind"] == (
            "offer"
        )
        assert (
            await client.put(f"/cases/{case}/tier", headers=ALICE, json={"tier": "CERTIFIED"})
        ).status_code == 200
        response = await client.post(f"/cases/{case}/authorization", headers=ALICE, json=CARD)
    assert response.status_code == expected_status
    if expected_body is not None:
        assert response.json() == expected_body
    else:
        assert response.json()["status"] == "requires_confirmation"


def test_free_preview_fake_refuses_new_authorization_but_replays_existing() -> None:
    """Free phase gates only a new preview intent; a saved one keeps replaying."""
    from parkingbreaker.api.fakes import FakeConflict, FakeServices

    def submitted(services: FakeServices, owner: str) -> str:
        workspace = services.create_workspace(owner, s.CreateWorkspace(notes=""))
        receipt = services.submit(
            workspace.workspace_id,
            s.SubmitIntake.model_validate(
                {
                    **INTAKE,
                    "ticket": {
                        **INTAKE["ticket"],
                        "issue_date": datetime.now(UTC).date().isoformat(),
                    },
                    "photo_refs": [],
                }
            ),
        )
        services.choose(receipt.case_id, s.ChooseTier(tier=s.MailTier.CERTIFIED))
        return receipt.case_id

    body = s.StartAuthorization(terms_accepted=True, payment_method_id="pm_card_visa")
    paid = FakeServices()
    existing_case = submitted(paid, "fake-alice")
    saved = paid.authorize(existing_case, body)

    # The same volatile preview state, now composed with the frozen free phase.
    free = FakeServices(service_phase=ServicePhase.CONTROLLED_FREE_BETA)
    free.workspaces, free.cases = paid.workspaces, paid.cases
    assert free.authorize(existing_case, body) == saved

    new_case = submitted(free, "fake-bob")
    with pytest.raises(FakeConflict, match=r"^paid_orders_paused$"):
        free.authorize(new_case, body)
    assert free.cases[new_case].authorization is None


async def test_controlled_http_refuses_a_new_paid_intent_without_side_effects(
    tmp_path: Path, fake_resolution: list[str]
) -> None:
    path = tmp_path / "phase.sqlite"
    payments = FakePaymentClient()
    real = controlled(service(path, payments, Clock()))
    store = real._store
    app = create_app(real)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://phase.test"
    ) as client:
        workspace = (await client.post("/workspaces", headers=ALICE, json={"notes": ""})).json()[
            "workspace_id"
        ]
        uploaded = await client.post(
            f"/workspaces/{workspace}/photos",
            headers=ALICE,
            json={"content_type": "image/png", "data_base64": PNG_B64},
        )
        ref = uploaded.json()["photo_ref"]
        body = {**INTAKE, "photo_refs": [ref], "citation_photo_ref": ref}
        case = (
            await client.post(f"/workspaces/{workspace}/intake", headers=ALICE, json=body)
        ).json()["case_id"]
        offer = await client.get(f"/cases/{case}/pre-hold", headers=ALICE)
        assert offer.json()["kind"] == "offer"
        chosen = await client.put(f"/cases/{case}/tier", headers=ALICE, json={"tier": "CERTIFIED"})
        assert chosen.status_code == 200
        refused = await client.post(f"/cases/{case}/authorization", headers=ALICE, json=CARD)
        assert refused.status_code == 409
        assert refused.json() == {"detail": "paid_orders_paused"}
        assert (
            await client.post(f"/cases/{case}/authorization", headers=ALICE, json=CARD)
        ).status_code == 409
        assert (
            await client.post(f"/cases/{case}/authorization", headers=BOB, json=CARD)
        ).status_code == 404
    assert payments.calls == []
    assert payments.authorizations == {}
    assert store._require_open().execute(
        "SELECT COUNT(*) FROM pb_intake_authorization_attempts WHERE intake_id=?", (case,)
    ).fetchone() == (0,)

    # A restart on the same durable database still cannot create the intent.
    store.close()
    reopened = controlled(service(path, payments, Clock()))
    try:
        with pytest.raises(ServiceConflict, match=r"^paid_orders_paused$"):
            reopened.authorize(
                case, s.StartAuthorization(terms_accepted=True, payment_method_id="pm_card_visa")
            )
    finally:
        reopened._store.close()
    assert payments.calls == []


async def test_real_pricing_reports_the_single_service_phase_and_keeps_its_price(
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
        assert services is not None
        app = create_app(
            services,
            worker_secret=SECRET,
            customer_auth=AuthHttp(
                AccessService(store, clock=lambda: Clock()()),
                FakeRequester(),
                origin=ORIGIN,
                clock=lambda: Clock()(),
            ),
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
            first = await client.post("/auth/pricing", headers={"Origin": ORIGIN}, json={})
            second = await client.post("/auth/pricing", headers={"Origin": ORIGIN}, json={})
            assert first.status_code == 200
            shown = first.json()
            assert shown["service_phase"] == "controlled_free_beta"
            assert shown["amount_cents"] in {1999, 2499, 2999}
            assert second.json() == shown
            assert (
                await client.post("/auth/pricing", headers={"Origin": ORIGIN}, json={"x": 1})
            ).status_code == 400


def test_controlled_phase_still_recovers_an_uncertain_exact_intent_once(
    recovery: RecoveryCase,
) -> None:
    payments = recovery.payments
    payments.lose_next_response = True
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    saved = recovery.attempt
    assert saved.state == "uncertain"
    assert saved.authorization_ref is None
    calls = list(payments.calls)

    recovery.real = controlled(recovery.real)
    authorization = recovery.real.authorize(recovery.case_id, BODY)
    assert payments.calls == [
        *calls,
        ("authorize", BODY.payment_method_id, 2999, recovery.key),
    ]
    assert authorization.payment_intent_id == payments.object.authorization_ref
    assert len(payments.authorizations) == 1
    recovery.assert_one_case(authorization)
    # The now-linked paid case replays its saved authorization under the same phase.
    assert recovery.real.authorize(recovery.case_id, BODY) == authorization
    assert payments.captures == 0
    assert payments.releases == 0


def test_controlled_phase_recovers_a_known_reference_hold_once(recovery: RecoveryCase) -> None:
    payments = recovery.payments
    payments.next_response = PaymentOutcome("uncertain", payments.object.authorization_ref)
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    saved = recovery.attempt
    assert saved.state == "uncertain"
    assert saved.authorization_ref is not None

    recovery.real = controlled(recovery.real)
    authorization = recovery.real.authorize(recovery.case_id, BODY)
    calls = list(payments.calls)
    assert calls == [
        ("authorize", BODY.payment_method_id, 2999, recovery.key),
        ("retrieve", saved.authorization_ref, 2999, ""),
    ]
    recovery.assert_one_case(authorization)
    assert recovery.real.authorize(recovery.case_id, BODY) == authorization
    assert payments.calls == calls


def test_controlled_phase_cannot_replace_a_declined_intent(recovery: RecoveryCase) -> None:
    payments = recovery.payments
    payments.next_response = full_outcome(PaymentOutcome("declined", "auth_declined"))
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    saved = recovery.attempt
    assert saved.state == "declined"
    calls = list(payments.calls)

    recovery.real = controlled(recovery.real)
    # The exact declined identity still refuses without another provider call.
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    assert payments.calls == calls
    # A different card/tier cannot begin a new paid try during the free phase.
    with pytest.raises(ServiceConflict, match=r"^paid_orders_paused$"):
        recovery.real.authorize(
            recovery.case_id,
            s.StartAuthorization(payment_method_id="pm_different", terms_accepted=True),
        )
    assert payments.calls == calls
    assert recovery.real._store._require_open().execute(
        "SELECT COUNT(*) FROM pb_intake_authorization_attempts"
    ).fetchone() == (1,)
    recovery.assert_no_case()


def test_controlled_phase_keeps_a_linked_paid_case_readable_and_unchanged(
    recovery: RecoveryCase,
) -> None:
    authorization = recovery.real.authorize(recovery.case_id, BODY)
    payments = recovery.payments
    calls = list(payments.calls)
    recovery.real = controlled(recovery.real)

    assert recovery.real.authorize(recovery.case_id, BODY) == authorization
    status = recovery.real.status(recovery.case_id)
    assert status.case_id == recovery.case_id
    assert status.amount_cents == 2999
    assert status.payment_state is not None
    assert status.payment_state.value == "AUTHORIZED"
    assert payments.calls == calls
    recovery.assert_one_case(authorization)
