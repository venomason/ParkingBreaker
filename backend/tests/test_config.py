"""Provider modes: offline fake mode runs a whole transaction; keys are checked, never shown."""

from __future__ import annotations

import base64
import io
from collections.abc import AsyncIterator, Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx2 import ASGITransport, AsyncClient
from PIL import Image, ImageDraw

from parkingbreaker import llm_config
from parkingbreaker.api.app import create_app
from parkingbreaker.brave_search import BraveSearcher
from parkingbreaker.config import ConfigError, ProviderMode, build_services, load_settings
from parkingbreaker.llm import LlmClient
from parkingbreaker.llm_models import ModelPricing

pytestmark = pytest.mark.anyio
ALICE = {"Authorization": "Bearer fake-alice"}
SECRET = "worker-secret-0123456789"
WORKER = {"Authorization": f"Bearer {SECRET}"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def png(ink: bool = False) -> str:
    image = Image.new("RGB", (120, 40), "white")
    if ink:
        ImageDraw.Draw(image).line((5, 30, 110, 8), fill="black", width=3)
    out = io.BytesIO()
    image.save(out, "PNG")
    return base64.b64encode(out.getvalue()).decode()


@pytest.fixture
async def fake_api(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    settings = load_settings(
        {"PB_PROVIDER_MODE": "fake", "PB_WORKER_SECRET": SECRET, "PB_DATA_DIR": str(tmp_path)},
        tmp_path / "no.env",
    )
    assert settings.mode is ProviderMode.FAKE
    app = create_app(build_services(settings), worker_secret=settings.worker_secret)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fake.test") as c:
        yield c


@pytest.mark.parametrize("mode", ["fake", "test"])
def test_real_service_compositions_use_nationwide_default(tmp_path: Path, mode: str) -> None:
    values = {"PB_PROVIDER_MODE": mode, "PB_WORKER_SECRET": SECRET, "PB_DATA_DIR": str(tmp_path)}
    if mode == "test":
        snapshot = tmp_path / "dotgov-2026-10-17.csv"
        snapshot.write_text(
            "Domain name,Domain type,Organization name,Suborganization name,City,State,"
            "Security contact email\n",
            encoding="utf-8",
        )
        values.update(
            {
                "STRIPE_SECRET_KEY": "sk_test_offline",
                "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": "pk_test_offline",
                "LOB_API_KEY": "test_offline",
                "STRIPE_WEBHOOK_SECRET": "whsec_offline",
                "LOB_WEBHOOK_SECRET": "lob-offline-secret",
                "OPENAI_API_KEY": "test-placeholder",
                "MINIMAX_API_KEY": "test-minimax-placeholder",
                "BRAVE_SEARCH_API_KEY": "test-placeholder",
                "PB_DOTGOV_SNAPSHOT": str(snapshot),
            }
        )
    settings = load_settings(values, tmp_path / "no.env")
    services = build_services(settings)
    assert services is not None
    assert services._deps.launch_allowlist is None  # type: ignore[attr-defined]


def _test_mode_values(tmp_path: Path) -> dict[str, str]:
    snapshot = tmp_path / "dotgov-2026-10-19.csv"
    snapshot.write_text(
        "Domain name,Domain type,Organization name,Suborganization name,City,State,"
        "Security contact email\n",
        encoding="utf-8",
    )
    return {
        "PB_PROVIDER_MODE": "test",
        "PB_WORKER_SECRET": SECRET,
        "PB_DATA_DIR": str(tmp_path),
        "STRIPE_SECRET_KEY": "sk_test_offline",
        "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": "pk_test_offline",
        "LOB_API_KEY": "test_offline",
        "STRIPE_WEBHOOK_SECRET": "whsec_offline",
        "LOB_WEBHOOK_SECRET": "lob-offline-secret",
        "OPENAI_API_KEY": "test-openai-placeholder",
        "MINIMAX_API_KEY": "test-minimax-placeholder",
        "ANTHROPIC_API_KEY": "test-anthropic-placeholder",
        "BRAVE_SEARCH_API_KEY": "test-brave-placeholder",
        "PB_DOTGOV_SNAPSHOT": str(snapshot),
    }


@pytest.mark.parametrize(
    ("explicit_preset", "expected_preset"),
    [(None, "minimax-m3.1"), ("gpt-6-luna", "gpt-6-luna"), ("sonnet-5.5", "sonnet-5.5")],
)
def test_test_mode_uses_minimax_default_and_keeps_brave_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_preset: str | None,
    expected_preset: str,
) -> None:
    values = _test_mode_values(tmp_path)
    if explicit_preset is not None:
        values["PB_LLM_PRESET"] = explicit_preset
    settings = load_settings(values, tmp_path / "no.env")
    seen: list[str] = []
    original = llm_config.build_llm_client

    def observe(
        preset: str,
        environ: Mapping[str, str],
        *,
        minimax_pricing: ModelPricing | None = None,
    ) -> LlmClient:
        seen.append(preset)
        return original(preset, environ, minimax_pricing=minimax_pricing)

    monkeypatch.setattr(llm_config, "build_llm_client", observe)
    services = build_services(settings)
    assert seen == [expected_preset]
    assert services is not None
    assert isinstance(services._deps.prehold.searcher, BraveSearcher)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("missing_key", "expected_error"),
    [
        ("MINIMAX_API_KEY", "Missing MINIMAX_API_KEY"),
        ("BRAVE_SEARCH_API_KEY", "Missing BRAVE_SEARCH_API_KEY"),
    ],
)
def test_provider_absence_fails_closed_without_a_fallback(
    tmp_path: Path, missing_key: str, expected_error: str
) -> None:
    values = _test_mode_values(tmp_path)
    values.pop(missing_key)
    settings = load_settings(values, tmp_path / "no.env")
    with pytest.raises(ValueError, match=expected_error):
        build_services(settings)


async def ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


async def held_case(c: AsyncClient) -> str:
    """Intake through the card hold at the customer's assigned price, in fake mode."""
    workspace = (
        await ok(await c.post("/workspaces", headers=ALICE, json={"notes": "Meter broken."}), 201)
    )["workspace_id"]
    photo = {"content_type": "image/png", "data_base64": png()}
    ref = (
        await ok(await c.post(f"/workspaces/{workspace}/photos", headers=ALICE, json=photo), 201)
    )["photo_ref"]
    issued = (date.today() - timedelta(days=3)).isoformat()
    intake = {
        "rental_company_paid": "NOT_RENTAL",
        "ticket": {
            "citation_number": "SYN-0001",
            "license_plate": "SYN123",
            "license_plate_state": "CA",
            "issuer_hint": "San Francisco Municipal Transportation Agency",
            "issued_city": "San Francisco",
            "issued_state": "CA",
            "violation_code": "SYN-METER",
            "violation_description": "Meter expired",
            "issue_date": issued,
            "stage": "first-level review",  # the synthetic fixture's wording
        },
        "customer_position": {"statement": "The meter was broken when I parked."},
        "contact": {
            "name": "Alex Example",
            "phone": "415-555-0100",
            "email": "alex@example.test",
            "mailing_address": {
                "line1": "123 Example Street",
                "city": "San Francisco",
                "state": "CA",
                "zip_code": "94103",
            },
        },
        "citation_photo_ref": ref,
        "photo_refs": [ref],
    }
    case = (await ok(await c.post(f"/workspaces/{workspace}/intake", headers=ALICE, json=intake)))[
        "case_id"
    ]
    offer = await ok(await c.get(f"/cases/{case}/pre-hold", headers=ALICE))
    assert offer["kind"] == "offer", offer
    await ok(await c.put(f"/cases/{case}/tier", headers=ALICE, json={"tier": "CERTIFIED"}))
    hold = await ok(
        await c.post(
            f"/cases/{case}/authorization",
            headers=ALICE,
            json={"terms_accepted": True, "payment_method_id": "pm_card_visa"},
        )
    )
    assert hold["status"] == "authorized"
    return str(case)


async def test_fake_mode_runs_one_transaction_offline(fake_api: AsyncClient) -> None:
    c = fake_api
    case = await held_case(c)
    workspace = (await ok(await c.get(f"/cases/{case}/status", headers=ALICE)))["workspace_id"]
    status = await ok(
        await c.post(f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"})
    )
    if status["state"] == "NEEDS_CUSTOMER":
        status = await ok(
            await c.post(
                f"/cases/{case}/answers",
                headers=ALICE,
                json={"question_id": "VEHICLE_RELATIONSHIP", "answer": "REGISTERED_OWNER"},
            )
        )
    assert status["state"] == "DRAFTING"
    await ok(
        await c.post(f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "draft"})
    )
    draft = await ok(await c.get(f"/cases/{case}/draft", headers=ALICE))
    await ok(
        await c.post(
            f"/cases/{case}/adoption",
            headers=ALICE,
            json={"text": draft["text"], "text_sha256": draft["text_sha256"]},
        )
    )
    signature = {"content_type": "image/png", "data_base64": png(ink=True)}
    sig = (
        await ok(
            await c.post(f"/workspaces/{workspace}/photos", headers=ALICE, json=signature), 201
        )
    )["photo_ref"]
    preview = await ok(
        await c.post(
            f"/cases/{case}/packet-preview",
            headers=ALICE,
            json={"adopted_sha256": draft["text_sha256"], "signature_ref": sig},
        )
    )
    frozen = await ok(
        await c.post(
            f"/cases/{case}/approval",
            headers=ALICE,
            json={
                "adopted_sha256": draft["text_sha256"],
                "signature_ref": sig,
                "packet_sha256": preview["packet_sha256"],
                "envelope_sha256": preview["envelope_sha256"],
            },
        )
    )
    assert frozen["state"] == "FROZEN", frozen
    for _ in range(4):
        await ok(
            await c.post(
                f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "dispatch"}
            )
        )
    final = await ok(await c.get(f"/cases/{case}/status", headers=ALICE))
    assert final["payment_state"] == "CAPTURED", final
    tracking = await ok(await c.get(f"/cases/{case}/tracking", headers=ALICE))
    assert tracking


async def test_worker_routes_refuse_a_wrong_secret(fake_api: AsyncClient) -> None:
    response = await fake_api.post(
        "/internal/dispatch", headers={"Authorization": "Bearer nope"}, json={}
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("values", "variable"),
    [
        (
            {
                "PB_PROVIDER_MODE": "test",
                "STRIPE_SECRET_KEY": "sk_live_SECRETVALUE",
                "LOB_API_KEY": "test_x",
            },
            "STRIPE_SECRET_KEY",
        ),
        (
            {
                "PB_PROVIDER_MODE": "live",
                "STRIPE_SECRET_KEY": "sk_live_SECRETVALUE",
                "LOB_API_KEY": "live_x",
            },
            "PB_ALLOW_LIVE",
        ),
        (
            {
                "PB_PROVIDER_MODE": "live",
                "PB_ALLOW_LIVE": "1",
                "STRIPE_SECRET_KEY": "sk_live_SECRETVALUE",
                "LOB_API_KEY": "test_SECRETVALUE",
            },
            "LOB_API_KEY",
        ),
        ({"PB_PROVIDER_MODE": "fake"}, "PB_WORKER_SECRET"),
        ({"PB_PROVIDER_MODE": "bogus"}, "PB_PROVIDER_MODE"),
    ],
)
def test_bad_configuration_names_the_variable_never_the_value(
    tmp_path: Path, values: dict[str, str], variable: str
) -> None:
    values = {"PB_WORKER_SECRET": SECRET, **values} if variable != "PB_WORKER_SECRET" else values
    with pytest.raises(ConfigError) as error:
        load_settings(values, tmp_path / "no.env")
    assert variable in str(error.value)
    assert "SECRETVALUE" not in str(error.value)


async def test_scheduler_sweep_alone_carries_a_case_to_capture(fake_api: AsyncClient) -> None:
    c = fake_api
    case = await held_case(c)
    sweep = {"Authorization": f"Bearer {SECRET}"}
    await ok(await c.post("/internal/dispatch", headers=sweep))
    status = await ok(await c.get(f"/cases/{case}/status", headers=ALICE))
    if status["state"] == "NEEDS_CUSTOMER":
        await ok(
            await c.post(
                f"/cases/{case}/answers",
                headers=ALICE,
                json={"question_id": "VEHICLE_RELATIONSHIP", "answer": "REGISTERED_OWNER"},
            )
        )
        await ok(await c.post("/internal/dispatch", headers=sweep))
    draft = await ok(await c.get(f"/cases/{case}/draft", headers=ALICE))
    await ok(
        await c.post(
            f"/cases/{case}/adoption",
            headers=ALICE,
            json={"text": draft["text"], "text_sha256": draft["text_sha256"]},
        )
    )
    preview = await ok(
        await c.post(
            f"/cases/{case}/packet-preview",
            headers=ALICE,
            json={"adopted_sha256": draft["text_sha256"]},
        )
    )
    await ok(
        await c.post(
            f"/cases/{case}/approval",
            headers=ALICE,
            json={
                "adopted_sha256": draft["text_sha256"],
                "packet_sha256": preview["packet_sha256"],
                "envelope_sha256": preview["envelope_sha256"],
            },
        )
    )
    for _ in range(3):
        await ok(await c.post("/internal/dispatch", headers=sweep))
    final = await ok(await c.get(f"/cases/{case}/status", headers=ALICE))
    assert final["payment_state"] == "CAPTURED", final


@pytest.mark.parametrize("mode", ["test", "live"])
@pytest.mark.parametrize(
    "key", ["", "pk_test_", "pk_live_", "pk_test_private ", "pk_live_private\n", "pk_test_privateé"]
)
def test_missing_or_mixed_browser_payment_key_refused_before_io(
    tmp_path: Path, mode: str, key: str
) -> None:
    values = {
        "PB_PROVIDER_MODE": mode,
        "PB_WORKER_SECRET": SECRET,
        "PB_ALLOW_LIVE": "1",
        "STRIPE_SECRET_KEY": f"sk_{mode}_synthetic",
        "LOB_API_KEY": f"{mode}_synthetic",
        "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": key,
    }
    with pytest.raises(ConfigError, match="NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY") as error:
        load_settings(values, tmp_path / "missing.env")
    if key:
        assert key not in str(error.value)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("mode", ["test", "live"])
@pytest.mark.parametrize(
    "secret",
    [
        "local-worker-secret-0123456789",
        "dev-worker-secret-0123456789",
        " a-long-private-worker-value",
        "private-worker-value\n",
        "private-worker-🔑-value",
    ],
)
def test_public_or_malformed_worker_secret_refused(tmp_path: Path, mode: str, secret: str) -> None:
    values = {
        "PB_PROVIDER_MODE": mode,
        "PB_WORKER_SECRET": secret,
        "PB_ALLOW_LIVE": "1",
        "STRIPE_SECRET_KEY": f"sk_{mode}_synthetic",
        "LOB_API_KEY": f"{mode}_synthetic",
        "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": f"pk_{mode}_synthetic",
    }
    with pytest.raises(ConfigError, match="PB_WORKER_SECRET") as error:
        load_settings(values, tmp_path / "missing.env")
    assert secret not in str(error.value)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("mode", ["test", "live"])
def test_valid_customer_mode_configuration_uses_one_matching_browser_key(
    tmp_path: Path, mode: str
) -> None:
    values = {
        "PB_PROVIDER_MODE": mode,
        "PB_WORKER_SECRET": SECRET,
        "PB_ALLOW_LIVE": "1",
        "PB_SERVICE_PHASE": "paid",
        "STRIPE_SECRET_KEY": f"sk_{mode}_synthetic",
        "LOB_API_KEY": f"{mode}_synthetic",
        "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": f"pk_{mode}_synthetic",
        "STRIPE_WEBHOOK_SECRET": "whsec_synthetic",
        "LOB_WEBHOOK_SECRET": "lob-synthetic-secret",
    }
    assert load_settings(values, tmp_path / "missing.env").mode.value == mode
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("missing", "bad"),
    [
        ("STRIPE_WEBHOOK_SECRET", ""),
        ("STRIPE_WEBHOOK_SECRET", "whsec_"),
        ("STRIPE_WEBHOOK_SECRET", "SECRETVALUE"),
        ("LOB_WEBHOOK_SECRET", ""),
        ("LOB_WEBHOOK_SECRET", "SECRET VALUE"),
    ],
)
def test_test_mode_refuses_to_start_without_webhook_secrets(
    tmp_path: Path, missing: str, bad: str
) -> None:
    values = _test_mode_values(tmp_path) | {missing: bad}
    with pytest.raises(ConfigError) as error:
        load_settings(values, tmp_path / "no.env")
    assert missing in str(error.value)
    assert "SECRET" not in str(error.value).replace(missing, "")


def test_role_routing_composes_dedicated_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = _test_mode_values(tmp_path)
    values.update(
        {
            "PB_LLM_ROLE_ROUTING": "1",
            "GEMINI_API_KEY": "test-gemini-key",
            "DEEPSEEK_API_KEY": "test-deepseek-key",
        }
    )
    settings = load_settings(values, tmp_path / "no.env")
    seen_presets: list[str] = []
    original_builder = llm_config.build_llm_client

    def observe(
        preset: str,
        environ: Mapping[str, str],
        *,
        minimax_pricing: ModelPricing | None = None,
        mimo_model_config: Any = None,
    ) -> LlmClient:
        seen_presets.append(preset)
        return original_builder(
            preset,
            environ,
            minimax_pricing=minimax_pricing,
            mimo_model_config=mimo_model_config,
        )

    monkeypatch.setattr(llm_config, "build_llm_client", observe)
    services = build_services(settings)
    assert services is not None
    # Customer-authored drafting composes no drafting provider: only the reading and
    # research roles route to a model now.
    assert seen_presets == ["gemini-3.8-flash", "deepseek-v4-pro"]
    from parkingbreaker.appeal.drafting import CustomerTextDrafter
    from parkingbreaker.case_service import CaseService

    assert isinstance(services, CaseService)
    assert isinstance(services._deps.drafter, CustomerTextDrafter)
