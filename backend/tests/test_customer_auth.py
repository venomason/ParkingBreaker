from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.api.customer_auth import (
    CSRF_COOKIE,
    MAX_BODY_BYTES,
    SESSION_COOKIE,
    AuthHttp,
    install_auth_routes,
)
from parkingbreaker.customer_access import AccessService, token_digest
from parkingbreaker.transaction_store import SqliteTransactionStore

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
ORIGIN = "https://portal.example.test"


def credential(value: int) -> str:
    return base64.urlsafe_b64encode(bytes([value]) * 32).decode("ascii").rstrip("=")


class FakeRequester:
    def __init__(self) -> None:
        self.queued: list[tuple[str, str]] = []

    def queue_link(
        self,
        *,
        email: str,
        request_id: str,
        scope_id: str | None = None,
        workspace_id: str | None = None,
    ) -> None:
        self.queued.append((email, request_id))


@pytest.fixture
def auth_env(
    tmp_path: Path,
) -> Iterator[tuple[FastAPI, SqliteTransactionStore, list[datetime], FakeRequester, str]]:
    store = SqliteTransactionStore(tmp_path / "auth.sqlite")
    raw_link = credential(1)
    clock = [NOW]
    store.issue_identity_link(
        customer_id="customer-1",
        email="Customer@example.test",
        token_digest=token_digest(raw_link),
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    generated = iter(credential(value) for value in range(2, 40))
    access = AccessService(store, clock=lambda: clock[0], token_factory=lambda: next(generated))
    requester = FakeRequester()
    app = FastAPI()
    install_auth_routes(
        app,
        AuthHttp(access, requester, origin=ORIGIN, clock=lambda: clock[0]),
    )
    yield app, store, clock, requester, raw_link
    store.close()


async def open_client(app: FastAPI) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN)
    await client.__aenter__()
    return client


async def confirm(client: AsyncClient, raw_link: str) -> tuple[str, str]:
    response = await client.post(
        "/auth/confirm",
        headers={"Origin": ORIGIN},
        json={"token": raw_link},
    )
    assert response.status_code == 200, response.text
    return client.cookies[SESSION_COOKIE], client.cookies[CSRF_COOKIE]


async def test_get_confirmation_is_inert_and_never_echoes_query_token(
    auth_env: tuple[FastAPI, SqliteTransactionStore, list[datetime], FakeRequester, str],
) -> None:
    app, store, _clock, _requester, raw_link = auth_env
    client = await open_client(app)
    try:
        response = await client.get(f"/auth/confirm?token={raw_link}")
        assert response.status_code == 200
        assert response.json() == {"confirmation_required": True}
        assert raw_link not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        row = (
            store._require_open()
            .execute(
                "SELECT consumed_at FROM pb_identity_links WHERE token_digest=?",
                (token_digest(raw_link),),
            )
            .fetchone()
        )
        assert row[0] is None
    finally:
        await client.aclose()


async def test_link_request_uses_canonical_email_and_generic_accepted_response(
    auth_env: tuple[FastAPI, SqliteTransactionStore, list[datetime], FakeRequester, str],
) -> None:
    app, _store, _clock, requester, _raw_link = auth_env
    request_id = "d30fb0c7-03a2-48b3-bcd7-1a55cce4ec99"
    client = await open_client(app)
    try:
        response = await client.post(
            "/auth/link",
            headers={"Origin": ORIGIN},
            json={"email": "  Customer@EXAMPLE.test ", "request_id": request_id},
        )
        assert response.status_code == 202
        assert response.json() == {"accepted": True}
        assert requester.queued[0][0] == "Customer@example.test"
        assert str(UUID(requester.queued[0][1], version=4)) == request_id
        replay = await client.post(
            "/auth/link",
            headers={"Origin": ORIGIN},
            json={"email": "Customer@example.test", "request_id": request_id},
        )
        assert replay.status_code == 202
        assert requester.queued == [("Customer@example.test", request_id)] * 2
        assert "Customer" not in response.text
        assert response.headers["cache-control"] == "no-store"
    finally:
        await client.aclose()


async def test_confirm_sets_secure_host_cookies_and_logout_revokes_session(
    auth_env: tuple[FastAPI, SqliteTransactionStore, list[datetime], FakeRequester, str],
) -> None:
    app, _store, _clock, _requester, raw_link = auth_env
    client = await open_client(app)
    try:
        response = await client.post(
            "/auth/confirm", headers={"Origin": ORIGIN}, json={"token": raw_link}
        )
        assert response.status_code == 200
        assert response.json() == {"authenticated": True}
        assert raw_link not in response.text
        assert "customer-1" not in response.text
        session = client.cookies[SESSION_COOKIE]
        csrf = client.cookies[CSRF_COOKIE]
        assert session not in response.text
        assert csrf not in response.text
        set_cookies = response.headers.get_list("set-cookie")
        session_cookie = next(value for value in set_cookies if value.startswith(SESSION_COOKIE))
        csrf_cookie = next(value for value in set_cookies if value.startswith(CSRF_COOKIE))
        assert "Secure" in session_cookie
        assert "HttpOnly" in session_cookie
        assert "Path=/" in session_cookie
        assert "SameSite=lax" in session_cookie
        assert "Max-Age=2592000" in session_cookie
        assert "Secure" in csrf_cookie
        assert "HttpOnly" not in csrf_cookie
        assert (await client.get("/auth/session")).json() == {"authenticated": True}

        logged_out = await client.post(
            "/auth/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
            json={},
        )
        assert logged_out.status_code == 200
        assert logged_out.json() == {"authenticated": False}
        assert all("Max-Age=0" in value for value in logged_out.headers.get_list("set-cookie"))
        assert (await client.get("/auth/session")).status_code == 401
    finally:
        await client.aclose()


async def test_session_requires_real_cookie_and_rejects_preview_bearers(
    auth_env: tuple[FastAPI, SqliteTransactionStore, list[datetime], FakeRequester, str],
) -> None:
    app, _store, _clock, _requester, _raw_link = auth_env
    client = await open_client(app)
    try:
        assert (await client.get("/auth/session")).status_code == 401
        assert (
            await client.get("/auth/session", headers={"Authorization": "Bearer fake-alice"})
        ).status_code == 401
        client.cookies.set(SESSION_COOKIE, "fake-alice")
        assert (await client.get("/auth/session")).status_code == 401
        assert (await client.get("/auth/session", headers={"Authorization": ""})).status_code == 401
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({"Origin": "https://attacker.example.test"}, 403),
        ({"Origin": ORIGIN}, 403),
        ({"Origin": ORIGIN, "X-CSRF-Token": "wrong"}, 403),
    ],
)
async def test_logout_requires_exact_origin_and_matching_csrf(
    auth_env: tuple[FastAPI, SqliteTransactionStore, list[datetime], FakeRequester, str],
    headers: dict[str, str],
    expected_status: int,
) -> None:
    app, _store, _clock, _requester, raw_link = auth_env
    client = await open_client(app)
    try:
        await confirm(client, raw_link)
        response = await client.post("/auth/logout", headers=headers, json={})
        assert response.status_code == expected_status
        assert response.json() == {"detail": "request_forbidden"}
        assert response.headers["cache-control"] == "no-store"
        assert (await client.get("/auth/session")).status_code == 200
    finally:
        await client.aclose()


async def test_session_expires_and_confirmation_is_single_use(
    auth_env: tuple[FastAPI, SqliteTransactionStore, list[datetime], FakeRequester, str],
) -> None:
    app, _store, clock, _requester, raw_link = auth_env
    client = await open_client(app)
    try:
        await confirm(client, raw_link)
        replay = await client.post(
            "/auth/confirm", headers={"Origin": ORIGIN}, json={"token": raw_link}
        )
        assert replay.status_code == 401
        clock[0] += timedelta(days=30)
        assert (await client.get("/auth/session")).status_code == 401
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("body", "content_type", "expected_status"),
    [
        (b'{"email":"a@example.test","email":"b@example.test"}', "application/json", 400),
        (b'{"email":"a@example.test","extra":true}', "application/json", 400),
        (b"{}", "text/plain", 400),
        (b"x" * (MAX_BODY_BYTES + 1), "application/json", 413),
    ],
)
async def test_link_body_limits_and_schema_errors_are_sanitized(
    auth_env: tuple[FastAPI, SqliteTransactionStore, list[datetime], FakeRequester, str],
    body: bytes,
    content_type: str,
    expected_status: int,
) -> None:
    app, _store, _clock, requester, _raw_link = auth_env
    client = await open_client(app)
    try:
        response = await client.post(
            "/auth/link",
            headers={"Origin": ORIGIN, "Content-Type": content_type},
            content=body,
        )
        assert response.status_code == expected_status
        assert "a@example.test" not in response.text
        assert "b@example.test" not in response.text
        assert requester.queued == []
    finally:
        await client.aclose()


async def test_public_auth_posts_require_exact_origin_and_reject_authorization(
    auth_env: tuple[FastAPI, SqliteTransactionStore, list[datetime], FakeRequester, str],
) -> None:
    app, _store, _clock, requester, raw_link = auth_env
    client = await open_client(app)
    try:
        wrong_origin = await client.post(
            "/auth/link",
            headers={"Origin": "https://other.example.test"},
            json={"email": "a@example.test"},
        )
        assert wrong_origin.status_code == 403
        auth_header = await client.post(
            "/auth/link",
            headers={"Origin": ORIGIN, "Authorization": "Bearer fake-bob"},
            json={"email": "a@example.test"},
        )
        assert auth_header.status_code == 401
        confirm_header = await client.post(
            "/auth/confirm",
            headers={"Origin": ORIGIN, "Authorization": "Bearer fake-alice"},
            json={"token": raw_link},
        )
        assert confirm_header.status_code == 401
        assert requester.queued == []
    finally:
        await client.aclose()
