"""Real cookie-authenticated application flows with fake provider I/O."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient, Request

from parkingbreaker import config, pricing_store
from parkingbreaker.api.app import create_app
from parkingbreaker.api.customer_auth import CSRF_COOKIE, PRICING_COOKIE, SESSION_COOKIE, AuthHttp
from parkingbreaker.config import ProviderMode, Settings, build_services
from parkingbreaker.config_fakes import FakeStripeTransport
from parkingbreaker.customer_access import AccessService, token_digest
from parkingbreaker.prices import ALLOWED_PRICE_CENTS, assigned_price
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_config import test_fake_mode_runs_one_transaction_offline as run_existing_flow
from test_customer_auth import ORIGIN, FakeRequester

pytestmark = pytest.mark.anyio
AT = datetime(2026, 11, 1, 12, tzinfo=UTC)


def headers(client: AsyncClient) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-CSRF-Token": client.cookies.get(CSRF_COOKIE, "") or ""}


def auth_app(store: SqliteTransactionStore) -> FastAPI:
    app = FastAPI()
    from parkingbreaker.api.customer_auth import install_auth_routes

    install_auth_routes(
        app,
        AuthHttp(
            AccessService(store, clock=lambda: AT), FakeRequester(), origin=ORIGIN, clock=lambda: AT
        ),
    )
    return app


async def test_price_cookie_is_sticky_and_never_a_private_access_credential(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "http.db") as store:
        app = auth_app(store)
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as one,
            AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as stranger,
        ):
            before = await one.post("/auth/pricing", json={})
            assert before.status_code == 403
            assert store._require_open().execute(
                "SELECT count(*) FROM pb_price_visitors"
            ).fetchone() == (0,)
            first = await one.post("/auth/pricing", headers={"Origin": ORIGIN}, json={})
            assert first.status_code == 200
            shown = first.json()
            raw = one.cookies[PRICING_COOKIE]
            assert SESSION_COOKIE not in one.cookies
            assert raw not in first.text
            cookie = first.headers["set-cookie"]
            assert "HttpOnly" in cookie
            assert "Secure" in cookie
            assert "SameSite=lax" in cookie
            assert first.headers["cache-control"] == "no-store"
            assert (await one.post("/auth/pricing", headers=headers(one), json={})).json() == shown
            assert (
                await one.post("/auth/appeals", headers=headers(one), json={})
            ).status_code == 201
            old_session = store.identity_session(token_digest(one.cookies[SESSION_COOKIE]), now=AT)
            assert old_session is not None
            assignment = store.pricing_for_scope(old_session.customer_id)
            assert assignment is not None
            assert assignment.amount_cents == shown["amount_cents"]
            # Possessing the price token does not authenticate a private session.
            stranger.cookies.set(PRICING_COOKIE, raw, domain="portal.example.test", path="/")
            assert (await stranger.get("/auth/session")).status_code == 401
            stranger.cookies.set(SESSION_COOKIE, raw, domain="portal.example.test", path="/")
            assert (await stranger.get("/auth/session")).status_code == 401
            assert (
                await one.post("/auth/pricing", headers={"Origin": ORIGIN}, json={})
            ).status_code == 403
            assert (
                await one.post("/auth/appeals", headers=headers(one), json={})
            ).status_code == 201
            new_session = store.identity_session(token_digest(one.cookies[SESSION_COOKIE]), now=AT)
            assert new_session is not None
            assert new_session.customer_id != old_session.customer_id
            assert store.pricing_for_scope(new_session.customer_id) == store.pricing_for_scope(
                old_session.customer_id
            )
            assert (await one.post("/auth/pricing", headers=headers(one), json={})).json() == shown


async def test_authenticated_scope_restores_price_after_cookie_loss_and_legacy_stays_unlabelled(
    tmp_path: Path,
) -> None:
    with SqliteTransactionStore(tmp_path / "recover.db") as store:
        app = auth_app(store)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
            await client.post("/auth/appeals", headers=headers(client), json={})
            first = (await client.post("/auth/pricing", headers=headers(client), json={})).json()
            client.cookies.delete(PRICING_COOKIE)
            assert (
                await client.post("/auth/pricing", headers=headers(client), json={})
            ).json() == first
            assert PRICING_COOKIE in client.cookies
        legacy = AccessService(store, clock=lambda: AT).start_appeal()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=ORIGIN,
            cookies={SESSION_COOKIE: legacy.session_token, CSRF_COOKIE: legacy.csrf_token},
        ) as client:
            response = await client.post("/auth/pricing", headers=headers(client), json={})
            assert response.status_code == 200
            assert response.json()["amount_cents"] is None
            assert response.json()["arm"] is None
            assert store.pricing_for_scope(legacy.customer_id) is None
            assert (
                await client.post(
                    "/auth/pricing", headers=headers(client), json={"amount_cents": 1}
                )
            ).status_code == 400


@pytest.mark.parametrize("price", sorted(ALLOWED_PRICE_CENTS))
async def test_each_real_browser_assignment_follows_original_flow_through_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    price: int,
) -> None:
    # Inject only visitor entropy, preserving the real pinned assignment algorithm.
    visitor = next(UUID(int=i) for i in range(1, 100) if assigned_price(UUID(int=i).hex) == price)
    monkeypatch.setattr(pricing_store, "uuid4", lambda: visitor)
    transport = FakeStripeTransport()
    monkeypatch.setattr(config, "FakeStripeTransport", lambda: transport)
    with SqliteTransactionStore(tmp_path / "flow.db") as store:
        services = build_services(
            Settings(ProviderMode.FAKE, tmp_path, "worker-secret-0123456789", {}), store=store
        )
        assert services is not None
        app = create_app(
            services,
            worker_secret="worker-secret-0123456789",
            customer_auth=AuthHttp(
                AccessService(store, clock=lambda: datetime.now(UTC)),
                FakeRequester(),
                origin=ORIGIN,
                clock=lambda: datetime.now(UTC),
            ),
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
            shown = await client.post("/auth/pricing", headers=headers(client), json={})
            assert shown.json()["amount_cents"] == price
            assert (
                await client.post("/auth/appeals", headers=headers(client), json={})
            ).status_code == 201

            async def use_real_cookie_auth(request: Request) -> None:
                # Reuse the original whole-flow assertions while converting only its
                # preview-customer headers; server receives real cookies and CSRF.
                if request.headers.get("authorization") == "Bearer worker-secret-0123456789":
                    # The original whole-flow test's WORKER constant is the configured
                    # worker credential; it never uses customer CSRF authority.
                    return
                if request.headers.get("authorization") == "Bearer fake-alice":
                    del request.headers["authorization"]
                    request.headers.update(headers(client))

            client.event_hooks["request"] = [use_real_cookie_auth]
            await run_existing_flow(client)
            assert len(transport.objects) == 1
            (payment,) = transport.objects.values()
            assert payment["amount"] == price
            assert payment["amount_received"] == price
            assert payment["status"] == "succeeded"
            cases = store._require_open().execute("SELECT amount_cents FROM pb_cases").fetchall()
            assert cases == [(price,)]
            offers = (
                store._require_open()
                .execute("SELECT prehold_json FROM pb_intake_records")
                .fetchall()
            )
            import json

            assert [tier["amount_cents"] for tier in json.loads(offers[0][0])["tiers"]] == [price]
            assert (
                await client.post("/auth/pricing", headers=headers(client), json={})
            ).json() == shown.json()
            workspace = (await client.get("/auth/appeal")).json()
            assert workspace is not None
            prior_workspace = workspace["workspace_id"]
            (prior_case,) = (
                store._require_open().execute("SELECT case_token FROM pb_cases").fetchone()
            )
            (prior_offer,) = (
                store._require_open()
                .execute("SELECT prehold_json FROM pb_intake_records")
                .fetchone()
            )
            # New private scope keeps this visitor price without exposing the prior appeal.
            assert (
                await client.post("/auth/appeals", headers=headers(client), json={})
            ).status_code == 201
            assert (await client.get(f"/workspaces/{prior_workspace}")).status_code == 404
            assert store._require_open().execute(
                "SELECT amount_cents FROM pb_cases WHERE case_token=?", (prior_case,)
            ).fetchone() == (price,)
            assert store._require_open().execute(
                "SELECT prehold_json FROM pb_intake_records"
            ).fetchone() == (prior_offer,)
            assert (
                await client.post("/auth/pricing", headers=headers(client), json={})
            ).json() == shown.json()
