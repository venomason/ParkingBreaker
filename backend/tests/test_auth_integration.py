"""Email access through the real API, case service, shared database and worker route."""

from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.access_email import AccessEmailService
from parkingbreaker.api.app import app_from_environment, create_app
from parkingbreaker.api.customer_auth import CSRF_COOKIE, AuthHttp
from parkingbreaker.auth_config import build_customer_access, load_auth_settings
from parkingbreaker.config import ConfigError, ProviderMode, Settings, build_services
from parkingbreaker.customer_access import AccessService
from parkingbreaker.email_client import EmailMessage, EmailOutcome, FakeEmailClient
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_access_email import credential
from test_case_service import INTAKE

pytestmark = pytest.mark.anyio
ORIGIN = "https://portal.example.test"
NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
WORKER = {"Authorization": "Bearer worker-secret-0123456789"}
AUTH_VALUES = {
    "PB_AUTH_MODE": "email",
    "PB_PUBLIC_ORIGIN": ORIGIN,
    "PB_AUTH_LINK_KEY": credential(1),
    "RESEND_FROM_EMAIL": "access@example.test",
    "RESEND_API_KEY": "synthetic-provider-key",
}


class RecordingEmail(FakeEmailClient):
    def __init__(self) -> None:
        super().__init__([EmailOutcome("accepted", str(uuid.uuid4())) for _ in range(8)])
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage, key: str) -> EmailOutcome:
        self.sent.append(message)
        return super().send(message, key)


def app_for(store: SqliteTransactionStore, folder: Path, mail: RecordingEmail) -> FastAPI:
    services = build_services(Settings(ProviderMode.FAKE, folder, "unused-secret", {}), store=store)
    delivery = AccessEmailService(
        store,
        mail,
        key=credential(1),
        origin=ORIGIN,
        sender_identity="access@example.test",
        clock=lambda: NOW,
    )
    auth = AuthHttp(
        AccessService(store, clock=lambda: NOW), delivery, origin=ORIGIN, clock=lambda: NOW
    )
    return create_app(
        services,
        worker_secret="worker-secret-0123456789",
        provider_mode="live",
        customer_auth=auth,
        notifications=delivery,
    )


@pytest.fixture
def environment(tmp_path: Path) -> Iterator[tuple[FastAPI, SqliteTransactionStore, RecordingEmail]]:
    with SqliteTransactionStore(tmp_path / "auth.sqlite") as store:
        mail = RecordingEmail()
        yield app_for(store, tmp_path, mail), store, mail


def headers(client: AsyncClient) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-CSRF-Token": client.cookies[CSRF_COOKIE]}


async def sign_in(client: AsyncClient, mail: RecordingEmail, email: str) -> str:
    request_id = str(uuid.uuid4())
    for _ in range(2):
        response = await client.post(
            "/auth/link",
            headers={"Origin": ORIGIN},
            json={"email": email, "request_id": request_id},
        )
        assert response.status_code == 202
        assert response.json() == {"accepted": True}
    before = len(mail.sent)
    assert (await client.post("/internal/dispatch", headers=WORKER)).status_code == 200
    assert len(mail.sent) == before + 1
    assert mail.sent[-1].to == email
    token = mail.sent[-1].text.split("#token=", 1)[1].splitlines()[0]
    assert (await client.get("/auth/confirm", params={"token": token})).status_code == 200
    response = await client.post("/auth/confirm", headers={"Origin": ORIGIN}, json={"token": token})
    assert response.status_code == 200
    return token


async def test_two_owners_cookie_csrf_case_gates_and_email_retries(
    environment: tuple[FastAPI, SqliteTransactionStore, RecordingEmail],
) -> None:
    app, store, mail = environment
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as alice,
        AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as bob,
    ):
        for token in ["fake-alice", "fake-bob", "alice", "arbitrary"]:
            response = await alice.post(
                "/workspaces", headers={"Authorization": f"Bearer {token}"}, json={}
            )
            assert response.status_code == 401
        await sign_in(alice, mail, "Alice@example.test")
        assert store._require_open().execute(
            "SELECT count(*) FROM pb_intake_records"
        ).fetchone() == (0,)
        response = await alice.post(
            "/workspaces", headers=headers(alice), json={"notes": "Alice private notes"}
        )
        assert response.status_code == 201
        workspace = response.json()["workspace_id"]
        intake = copy.deepcopy(INTAKE)
        intake["photo_refs"] = []
        intake["ticket"]["issuer_kind"] = "PRIVATE"
        response = await alice.post(
            f"/workspaces/{workspace}/intake", headers=headers(alice), json=intake
        )
        assert response.status_code == 200
        case = response.json()["case_id"]
        await sign_in(bob, mail, "Bob@example.test")
        assert len(mail.sent) == 2
        for path in [
            f"/workspaces/{workspace}",
            f"/cases/{case}/status",
            f"/cases/{case}/draft",
            f"/cases/{case}/tracking",
        ]:
            denied = await bob.get(path)
            assert denied.status_code == 404
            assert "Alice" not in denied.text
        assert (await alice.get(f"/workspaces/{workspace}")).json()[
            "notes"
        ] == "Alice private notes"
        for bad in [
            {},
            {"Origin": ORIGIN},
            {"Origin": "https://attacker.example.test", "X-CSRF-Token": alice.cookies[CSRF_COOKIE]},
        ]:
            assert (await alice.post("/workspaces", headers=bad, json={})).status_code == 403
        alice_csrf = alice.cookies[CSRF_COOKIE]
        bob.cookies.set(CSRF_COOKIE, alice_csrf, domain="portal.example.test", path="/")
        assert (
            await bob.post(
                "/workspaces", headers={"Origin": ORIGIN, "X-CSRF-Token": alice_csrf}, json={}
            )
        ).status_code == 403
        assert (await alice.post("/internal/dispatch", headers=headers(alice))).status_code == 401
        assert (
            await alice.post("/auth/logout", headers=headers(alice), json={})
        ).status_code == 200
        assert (await alice.get(f"/workspaces/{workspace}")).status_code == 401


async def test_session_and_workspace_survive_connection_reopen(tmp_path: Path) -> None:
    path = tmp_path / "auth.sqlite"
    mail = RecordingEmail()
    with SqliteTransactionStore(path) as store:
        app = app_for(store, tmp_path, mail)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
            await sign_in(client, mail, "Return@example.test")
            response = await client.post(
                "/workspaces", headers=headers(client), json={"notes": "Persisted"}
            )
            assert response.status_code == 201
            workspace = response.json()["workspace_id"]
            cookies = dict(client.cookies)
    with SqliteTransactionStore(path) as reopened:
        app = app_for(reopened, tmp_path, mail)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=ORIGIN, cookies=cookies
        ) as client:
            response = await client.get(f"/workspaces/{workspace}")
            assert response.status_code == 200
            assert response.json()["notes"] == "Persisted"
            assert response.headers["cache-control"] == "no-store"


async def test_concurrent_confirmation_posts_consume_exactly_once(
    environment: tuple[FastAPI, SqliteTransactionStore, RecordingEmail],
) -> None:
    app, store, mail = environment
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        await client.post(
            "/auth/link",
            headers={"Origin": ORIGIN},
            json={"email": "Concurrent@example.test", "request_id": str(uuid.uuid4())},
        )
        await client.post("/internal/dispatch", headers=WORKER)
        token = mail.sent[-1].text.split("#token=", 1)[1].splitlines()[0]
        replies = await asyncio.gather(
            *(
                client.post("/auth/confirm", headers={"Origin": ORIGIN}, json={"token": token})
                for _ in range(2)
            )
        )
        assert sorted(response.status_code for response in replies) == [200, 401]
        assert store._require_open().execute(
            "SELECT count(*) FROM pb_identity_sessions"
        ).fetchone() == (1,)


async def test_request_id_bound_to_different_email_is_a_sanitized_conflict(
    environment: tuple[FastAPI, SqliteTransactionStore, RecordingEmail],
) -> None:
    app, store, mail = environment
    request_id = str(uuid.uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        first = await client.post(
            "/auth/link",
            headers={"Origin": ORIGIN},
            json={"email": "Original@example.test", "request_id": request_id},
        )
        assert first.status_code == 202
        conflict = await client.post(
            "/auth/link",
            headers={"Origin": ORIGIN},
            json={"email": "Changed@example.test", "request_id": request_id},
        )
        assert conflict.status_code == 409
        assert conflict.json() == {"detail": "invalid_request"}
        assert store._require_open().execute(
            "SELECT count(*) FROM pb_identity_links"
        ).fetchone() == (1,)
        assert not mail.sent


@pytest.mark.parametrize("mode", [ProviderMode.TEST, ProviderMode.LIVE])
def test_configured_environment_composes_real_cookie_auth_without_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: ProviderMode,
) -> None:
    import parkingbreaker.config as config

    settings = Settings(mode, tmp_path, "worker-secret-0123456789", AUTH_VALUES)
    seen: list[SqliteTransactionStore] = []

    def offline_build(_settings: Settings, *, store: SqliteTransactionStore) -> object:
        seen.append(store)
        return build_services(Settings(ProviderMode.FAKE, tmp_path, "unused", {}), store=store)

    monkeypatch.setattr(config, "load_settings", lambda: settings)
    monkeypatch.setattr(config, "build_services", offline_build)
    app = app_from_environment()
    assert any(route.path == "/auth/session" for route in app.routes if hasattr(route, "path"))
    assert len(seen) == 1
    seen[0].close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("PB_AUTH_LINK_KEY", "bad-secret"),
        ("PB_PUBLIC_ORIGIN", "http://portal.example.test"),
        ("PB_PUBLIC_ORIGIN", ORIGIN + "/extra"),
        ("RESEND_FROM_EMAIL", "bad\nsender"),
        ("RESEND_API_KEY", ""),
        ("RESEND_API_KEY", "bad\nkey"),
    ],
)
def test_invalid_auth_config_is_sanitized_before_database_or_provider_construction(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    values = dict(AUTH_VALUES, **{field: value})
    with pytest.raises(ConfigError, match=field) as error:
        load_auth_settings(Settings(ProviderMode.TEST, tmp_path, "unused", values))
    if value:
        assert value not in str(error.value)
    assert not list(tmp_path.iterdir())


def test_auth_composition_never_creates_an_independent_database(tmp_path: Path) -> None:
    settings = load_auth_settings(Settings(ProviderMode.FAKE, tmp_path, "unused", AUTH_VALUES))
    assert settings is not None
    with SqliteTransactionStore(tmp_path / "only.sqlite") as store:
        auth, delivery = build_customer_access(settings, store, clock=lambda: NOW)
        auth.requester.queue_link(email="Owner@example.test", request_id=str(uuid.uuid4()))
        assert store._require_open().execute(
            "SELECT count(*) FROM pb_identity_links"
        ).fetchone() == (1,)
        assert delivery.dispatch() == 1
    assert [path.name for path in tmp_path.glob("*.sqlite")] == ["only.sqlite"]


async def test_same_email_appeals_are_isolated_and_recovery_restores_only_one(
    environment: tuple[FastAPI, SqliteTransactionStore, RecordingEmail],
) -> None:
    app, store, mail = environment
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as a,
        AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as b,
        AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as recovery,
    ):
        workspaces, cases, tokens = [], [], []
        for client in (a, b):
            # No email/account required to start; bad origin cannot replace access.
            assert (await client.post("/auth/appeals", json={})).status_code == 403
            assert (
                await client.post("/auth/appeals", headers={"Origin": ORIGIN}, json={})
            ).status_code == 201
            response = await client.post("/workspaces", headers=headers(client), json={})
            assert response.status_code == 201
            workspace = response.json()["workspace_id"]
            workspaces.append(workspace)
            # A retry or second workspace request cannot broaden this credential.
            repeated = await client.post("/workspaces", headers=headers(client), json={})
            assert repeated.json()["workspace_id"] == workspace
            request_id = str(uuid.uuid4())
            for _ in range(2):
                assert (
                    await client.post(
                        "/auth/link",
                        headers=headers(client),
                        json={
                            "email": "Same@example.test",
                            "request_id": request_id,
                        },
                    )
                ).status_code == 202
            assert (await client.post("/internal/dispatch", headers=WORKER)).status_code == 200
            tokens.append(mail.sent[-1].text.split("#token=", 1)[1].splitlines()[0])
            intake = copy.deepcopy(INTAKE)
            intake["photo_refs"] = []
            intake["ticket"]["issuer_kind"] = "PRIVATE"
            result = await client.post(
                f"/workspaces/{workspace}/intake", headers=headers(client), json=intake
            )
            assert result.status_code == 200
            cases.append(result.json()["case_id"])
        assert workspaces[0] != workspaces[1]
        assert len(mail.sent) == 2
        assert mail.sent[0].to == mail.sent[1].to == "Same@example.test"
        for client, other in ((a, 1), (b, 0)):
            for path in (f"/workspaces/{workspaces[other]}", f"/cases/{cases[other]}/status"):
                assert (await client.get(path)).status_code == 404
            assert (
                await client.put(
                    f"/cases/{cases[other]}/tier",
                    headers=headers(client),
                    json={"tier": "CERTIFIED"},
                )
            ).status_code == 404
            assert (
                await client.post(
                    f"/workspaces/{workspaces[other]}/intake", headers=headers(client), json=INTAKE
                )
            ).status_code == 404
            assert (
                await client.post("/auth/appeals", headers={"Origin": ORIGIN}, json={})
            ).status_code == 403
        response = await recovery.post(
            "/auth/confirm", headers={"Origin": ORIGIN}, json={"token": tokens[0]}
        )
        assert response.status_code == 200
        assert (await recovery.get("/auth/appeal")).json()["workspace_id"] == workspaces[0]
        assert (await recovery.get(f"/cases/{cases[1]}/status")).status_code == 404
        assert (
            await recovery.post(
                "/auth/confirm", headers={"Origin": ORIGIN}, json={"token": tokens[0]}
            )
        ).status_code == 401
        assert store._require_open().execute(
            "SELECT count(*) FROM pb_identity_accounts WHERE email='Same@example.test'"
        ).fetchone() == (2,)


async def test_recovery_requires_matching_appeal_contact_without_revealing_existence(
    environment: tuple[FastAPI, SqliteTransactionStore, RecordingEmail],
) -> None:
    app, store, mail = environment
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as owner:
        await owner.post("/auth/appeals", headers={"Origin": ORIGIN}, json={})
        workspace = (await owner.post("/workspaces", headers=headers(owner), json={})).json()[
            "workspace_id"
        ]
        await owner.post(
            "/auth/link",
            headers=headers(owner),
            json={"email": "Contact@example.test", "request_id": str(uuid.uuid4())},
        )
        await owner.post("/internal/dispatch", headers=WORKER)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as visitor:
        for workspace_id, email in (
            (workspace, "Wrong@example.test"),
            ("missing", "Contact@example.test"),
        ):
            result = await visitor.post(
                "/auth/link",
                headers={"Origin": ORIGIN},
                json={
                    "workspace_id": workspace_id,
                    "email": email,
                    "request_id": str(uuid.uuid4()),
                },
            )
            assert result.status_code == 202
            assert result.json() == {"accepted": True}
            await visitor.post("/internal/dispatch", headers=WORKER)
            assert len(mail.sent) == 1
        assert (await visitor.get(f"/workspaces/{workspace}")).status_code == 401
        request_id = str(uuid.uuid4())
        for _ in range(2):
            result = await visitor.post(
                "/auth/link",
                headers={"Origin": ORIGIN},
                json={
                    "workspace_id": workspace,
                    "email": "Contact@example.test",
                    "request_id": request_id,
                },
            )
            assert result.status_code == 202
        await visitor.post("/internal/dispatch", headers=WORKER)
        assert len(mail.sent) == 2
        token = mail.sent[-1].text.split("#token=", 1)[1].splitlines()[0]
        assert (
            await visitor.post("/auth/confirm", headers={"Origin": ORIGIN}, json={"token": token})
        ).status_code == 200
        assert (await visitor.get("/auth/appeal")).json()["workspace_id"] == workspace
        assert store._require_open().execute(
            "SELECT count(*) FROM pb_intake_owners"
        ).fetchone() == (1,)


async def test_intake_contact_recovers_when_initial_access_email_was_not_queued(
    environment: tuple[FastAPI, SqliteTransactionStore, RecordingEmail],
) -> None:
    app, _store, mail = environment
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as owner:
        await owner.post("/auth/appeals", headers={"Origin": ORIGIN}, json={})
        workspace = (await owner.post("/workspaces", headers=headers(owner), json={})).json()[
            "workspace_id"
        ]
        intake = copy.deepcopy(INTAKE)
        intake["photo_refs"] = []
        intake["ticket"]["issuer_kind"] = "PRIVATE"
        assert (
            await owner.post(f"/workspaces/{workspace}/intake", headers=headers(owner), json=intake)
        ).status_code == 200
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as visitor:
        result = await visitor.post(
            "/auth/link",
            headers={"Origin": ORIGIN},
            json={
                "workspace_id": workspace,
                "email": intake["contact"]["email"],
                "request_id": str(uuid.uuid4()),
            },
        )
        assert result.status_code == 202
        await visitor.post("/internal/dispatch", headers=WORKER)
        assert len(mail.sent) == 1
        token = mail.sent[0].text.split("#token=", 1)[1].splitlines()[0]
        assert (
            await visitor.post("/auth/confirm", headers={"Origin": ORIGIN}, json={"token": token})
        ).status_code == 200
        assert (await visitor.get("/auth/appeal")).json()["workspace_id"] == workspace


async def test_email_throttle_is_sanitized_and_persisted(
    environment: tuple[FastAPI, SqliteTransactionStore, RecordingEmail],
) -> None:
    app, store, _mail = environment
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        for _ in range(5):
            response = await client.post(
                "/auth/link",
                headers={"Origin": ORIGIN},
                json={"email": "Throttle@example.test", "request_id": str(uuid.uuid4())},
            )
            assert response.status_code == 202
        response = await client.post(
            "/auth/link",
            headers={"Origin": ORIGIN},
            json={"email": "Throttle@example.test", "request_id": str(uuid.uuid4())},
        )
        assert response.status_code == 429
        assert response.json() == {"detail": "request_rate_limited"}
        assert response.headers["retry-after"] == "900"
        assert store._require_open().execute(
            "SELECT count(*) FROM pb_identity_accounts"
        ).fetchone() == (5,)


async def test_recovery_throttle_does_not_disclose_matching_contact(
    environment: tuple[FastAPI, SqliteTransactionStore, RecordingEmail],
) -> None:
    app, store, _mail = environment
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as owner:
        await owner.post("/auth/appeals", headers={"Origin": ORIGIN}, json={})
        workspace = (await owner.post("/workspaces", headers=headers(owner), json={})).json()[
            "workspace_id"
        ]
        for _ in range(5):
            response = await owner.post(
                "/auth/link",
                headers=headers(owner),
                json={"email": "Private@example.test", "request_id": str(uuid.uuid4())},
            )
            assert response.status_code == 202
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as visitor:
        for target, email in (
            (workspace, "Private@example.test"),
            (workspace, "Wrong@example.test"),
            ("missing", "Private@example.test"),
        ):
            response = await visitor.post(
                "/auth/link",
                headers={"Origin": ORIGIN},
                json={"workspace_id": target, "email": email, "request_id": str(uuid.uuid4())},
            )
            assert response.status_code == 202
            assert response.json() == {"accepted": True}
            assert "retry-after" not in response.headers
        assert store._require_open().execute(
            "SELECT count(*) FROM pb_notification_access_links"
        ).fetchone() == (5,)


@pytest.mark.parametrize("mode", ["test", "live"])
def test_direct_customer_app_refuses_default_worker_secret(
    tmp_path: Path,
    mode: str,
) -> None:
    from typing import Literal, cast

    settings = load_auth_settings(Settings(ProviderMode.FAKE, tmp_path, "unused", AUTH_VALUES))
    assert settings is not None
    with SqliteTransactionStore(tmp_path / "auth.sqlite") as store:
        auth, _delivery = build_customer_access(settings, store, clock=lambda: NOW)
        with pytest.raises(ConfigError, match="PB_WORKER_SECRET"):
            create_app(customer_auth=auth, provider_mode=cast(Literal["test", "live"], mode))
