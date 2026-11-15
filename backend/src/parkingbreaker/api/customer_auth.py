"""Customer sign-in routes kept separate from the preview bearer-token API."""

from __future__ import annotations

import hmac
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import RequestResponseEndpoint

from parkingbreaker.access_link_material import build_access_url
from parkingbreaker.api.auth import Customer
from parkingbreaker.customer_access import AccessCredentials, AccessService
from parkingbreaker.identity_store import IdentityConflict, IdentitySession, _canonical_email
from parkingbreaker.notification_store import NotificationConflict, NotificationRateLimited
from parkingbreaker.prices import PRICE_EXPERIMENT_ID
from parkingbreaker.service_phase import ServicePhase

SESSION_COOKIE = "__Host-pb-session"
CSRF_COOKIE = "__Host-pb-csrf"
PRICING_COOKIE = "__Host-pb-price"
# Renewable browser recognition, not the lifetime of an assignment or private access.
PRICING_COOKIE_MAX_AGE_SECONDS = 400 * 24 * 60 * 60
MAX_BODY_BYTES = 4096
MAX_SESSION_AGE_SECONDS = 30 * 24 * 60 * 60
_ORIGIN_VALIDATION_TOKEN = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE"


class LinkRequester(Protocol):
    def queue_link(
        self,
        *,
        email: str,
        request_id: str,
        scope_id: str | None = None,
        workspace_id: str | None = None,
    ) -> None: ...


class AuthHttp:
    """HTTP policy for customer access; delivery and application wiring are injected later."""

    def __init__(
        self,
        access: AccessService,
        requester: LinkRequester,
        *,
        origin: str,
        clock: Callable[[], datetime],
    ) -> None:
        canonical_url = build_access_url(origin, _ORIGIN_VALIDATION_TOKEN)
        parsed_origin = urlsplit(canonical_url)
        self.origin = f"{parsed_origin.scheme}://{parsed_origin.netloc}"
        self.access = access
        self.requester = requester
        self.clock = clock

    async def customer(self, request: Request) -> Customer:
        # The owning event loop also owns the SQLite connection. A synchronous
        # FastAPI dependency would move this access to its worker thread pool.
        if "authorization" in request.headers:
            raise HTTPException(401, "authentication_required")
        session = self._session_from_cookie(request)
        # Every customer mutation shares the same origin and session-bound CSRF gate.
        # Checking only logout would leave the actual case routes unprotected.
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            self.require_csrf(request, session)
        request.state.identity_session = session
        return Customer(session.customer_id)

    def _session_from_cookie(self, request: Request) -> IdentitySession:
        if "authorization" in request.headers:
            raise HTTPException(401, "authentication_required")
        raw_session = request.cookies.get(SESSION_COOKIE, "")
        session = self.access.authenticate(raw_session)
        if session is None:
            raise HTTPException(401, "authentication_required")
        return session

    def require_origin(self, request: Request) -> None:
        supplied = request.headers.get("origin", "")
        try:
            matches = hmac.compare_digest(supplied, self.origin)
        except (TypeError, UnicodeEncodeError):
            matches = False
        if not matches:
            raise HTTPException(403, "request_forbidden")

    def require_csrf(self, request: Request, session: IdentitySession) -> None:
        self.require_origin(request)
        cookie = request.cookies.get(CSRF_COOKIE, "")
        header = request.headers.get("x-csrf-token", "")
        try:
            matches = hmac.compare_digest(cookie, header)
        except (TypeError, UnicodeEncodeError):
            matches = False
        if not matches or not self.access.csrf_valid(session, header):
            raise HTTPException(403, "request_forbidden")

    async def json_body(self, request: Request, *, fields: frozenset[str]) -> dict[str, object]:
        if "authorization" in request.headers:
            raise HTTPException(401, "authentication_required")
        self.require_origin(request)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise HTTPException(400, "invalid_request")
        chunks = bytearray()
        async for chunk in request.stream():
            if len(chunks) + len(chunk) > MAX_BODY_BYTES:
                raise HTTPException(413, "invalid_request")
            chunks.extend(chunk)
        try:
            parsed = json.loads(bytes(chunks).decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise HTTPException(400, "invalid_request") from None
        if not isinstance(parsed, dict) or any(
            not isinstance(key, str) or key not in fields for key in parsed
        ):
            raise HTTPException(400, "invalid_request")
        return parsed


def install_auth_routes(
    app: FastAPI,
    auth: AuthHttp,
    *,
    service_phase: ServicePhase = ServicePhase.PAID,
    service_phase_provider: Callable[[], ServicePhase] | None = None,
) -> None:
    """Install a small isolated router without changing global app exception handlers.

    ``service_phase_provider`` is the live phase source and wins when provided; it is
    called at request time so a private runtime change is reported without rebuilding the
    app. ``service_phase`` stays as the explicit enum fallback for existing callers and
    tests that do not pass a provider.
    """
    customer_dependency = Depends(auth.customer)

    @app.middleware("http")
    async def auth_privacy_headers(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        if request.url.path.startswith(("/auth/", "/workspaces", "/cases/")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/auth/confirm")
    async def confirm_landing() -> dict[str, bool]:
        return {"confirmation_required": True}

    @app.post("/auth/link", status_code=202)
    async def request_link(request: Request) -> Response:
        body = await auth.json_body(
            request, fields=frozenset({"email", "request_id", "workspace_id"})
        )
        email = body.get("email")
        request_id = body.get("request_id")
        workspace_id = body.get("workspace_id")
        if workspace_id is not None and (
            not isinstance(workspace_id, str) or not workspace_id or len(workspace_id) > 128
        ):
            raise HTTPException(400, "invalid_request")
        if not isinstance(email, str) or not isinstance(request_id, str):
            raise HTTPException(400, "invalid_request")
        try:
            canonical_email = _canonical_email(email)
            parsed_id = uuid.UUID(request_id)
            if parsed_id.version != 4 or str(parsed_id) != request_id:
                raise ValueError("invalid request id")
        except (IdentityConflict, ValueError):
            raise HTTPException(400, "invalid_request") from None
        try:
            # Browser retries retain this identity so a lost response cannot mint new links.
            session = auth.access.authenticate(request.cookies.get(SESSION_COOKIE, ""))
            if session is not None:
                auth.require_csrf(request, session)
            if workspace_id is not None:
                auth.requester.queue_link(
                    email=canonical_email, request_id=request_id, workspace_id=workspace_id
                )
            elif session is not None:
                auth.requester.queue_link(
                    email=canonical_email, request_id=request_id, scope_id=session.customer_id
                )
            else:
                auth.requester.queue_link(email=canonical_email, request_id=request_id)
        except HTTPException:
            raise
        except NotificationRateLimited:
            if workspace_id is not None:
                # Recovery must not reveal whether this workspace/contact matches,
                # even when matching requests have exhausted the recipient budget.
                return JSONResponse(status_code=202, content={"accepted": True})
            raise HTTPException(
                429, "request_rate_limited", headers={"Retry-After": "900"}
            ) from None
        except NotificationConflict:
            # A conflicting retry cannot succeed unchanged; do not advertise an outage.
            raise HTTPException(409, "invalid_request") from None
        except Exception:
            raise HTTPException(503, "request_unavailable") from None
        return JSONResponse(status_code=202, content={"accepted": True})

    @app.post("/auth/confirm")
    async def confirm_link(request: Request) -> Response:
        body = await auth.json_body(request, fields=frozenset({"token"}))
        token = body.get("token")
        if not isinstance(token, str):
            raise HTTPException(400, "invalid_request")
        try:
            credentials = auth.access.confirm(token)
        except Exception:
            raise HTTPException(503, "request_unavailable") from None
        if credentials is None:
            raise HTTPException(401, "authentication_required")
        return credential_response(credentials)

    def price_response_cookie(response: Response, raw_token: str | None) -> None:
        if raw_token is not None:
            response.set_cookie(
                PRICING_COOKIE,
                raw_token,
                max_age=PRICING_COOKIE_MAX_AGE_SECONDS,
                path="/",
                secure=True,
                httponly=True,
                samesite="lax",
            )

    @app.post("/auth/pricing")
    async def browser_pricing(request: Request) -> Response:
        await auth.json_body(request, fields=frozenset())
        session = auth.access.authenticate(request.cookies.get(SESSION_COOKIE, ""))
        if session is not None:
            auth.require_csrf(request, session)
        try:
            assignment, new_token = auth.access.browser_pricing(
                request.cookies.get(PRICING_COOKIE),
                scope_id=session.customer_id if session is not None else None,
            )
            # Legacy scopes have their own frozen offer amount. A new visitor assignment
            # is for the next appeal; it must not relabel that existing appeal's offer.
            legacy = session is not None and not auth.access.has_scope_pricing(session.customer_id)
            reported_phase = (
                service_phase if service_phase_provider is None else service_phase_provider()
            )
            if not isinstance(reported_phase, ServicePhase):
                raise ValueError("invalid service phase provider")
            response = JSONResponse(
                content={
                    "experiment_id": PRICE_EXPERIMENT_ID,
                    "arm": None if legacy else assignment.arm,
                    "amount_cents": None if legacy else assignment.amount_cents,
                    "service_phase": reported_phase.value,
                }
            )
            price_response_cookie(response, new_token or request.cookies.get(PRICING_COOKIE))
            return response
        except Exception:
            raise HTTPException(503, "request_unavailable") from None

    @app.post("/auth/appeals", status_code=201)
    async def start_appeal(request: Request) -> Response:
        await auth.json_body(request, fields=frozenset())
        # Replacing an existing session requires its CSRF proof; a cross-site caller
        # cannot switch an active browser into a different appeal.
        if SESSION_COOKIE in request.cookies:
            previous = auth.access.authenticate(request.cookies.get(SESSION_COOKIE, ""))
            if previous is not None:
                auth.require_csrf(request, previous)
        try:
            _assignment, new_token = auth.access.browser_pricing(
                request.cookies.get(PRICING_COOKIE)
            )
            pricing_token = new_token or request.cookies.get(PRICING_COOKIE)
            credentials = auth.access.start_appeal(pricing_token=pricing_token)
        except Exception:
            raise HTTPException(503, "request_unavailable") from None
        response = credential_response(credentials)
        response.status_code = 201
        price_response_cookie(response, pricing_token)
        return response

    def credential_response(credentials: AccessCredentials) -> Response:
        try:
            now = _utc(auth.clock())
        except (TypeError, ValueError, OverflowError):
            auth.access.revoke(credentials.session_token)
            raise HTTPException(503, "request_unavailable") from None
        remaining = int((credentials.expires_at - now).total_seconds())
        if remaining <= 0:
            auth.access.revoke(credentials.session_token)
            raise HTTPException(401, "authentication_required")
        max_age = min(remaining, MAX_SESSION_AGE_SECONDS)
        response = JSONResponse(content={"authenticated": True})
        response.set_cookie(
            SESSION_COOKIE,
            credentials.session_token,
            max_age=max_age,
            expires=credentials.expires_at,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        response.set_cookie(
            CSRF_COOKIE,
            credentials.csrf_token,
            max_age=max_age,
            expires=credentials.expires_at,
            path="/",
            secure=True,
            httponly=False,
            samesite="lax",
        )
        return response

    @app.get("/auth/session")
    async def session(_customer: Customer = customer_dependency) -> dict[str, bool]:
        return {"authenticated": True}

    @app.post("/auth/logout")
    async def logout(request: Request, _customer: Customer = customer_dependency) -> Response:
        session = getattr(request.state, "identity_session", None)
        if not isinstance(session, IdentitySession):
            raise HTTPException(401, "authentication_required")
        auth.require_csrf(request, session)
        body = await auth.json_body(request, fields=frozenset())
        if body:
            raise HTTPException(400, "invalid_request")
        raw_session = request.cookies.get(SESSION_COOKIE, "")
        auth.access.revoke(raw_session)
        response = JSONResponse(content={"authenticated": False})
        response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        response.delete_cookie(CSRF_COOKIE, path="/", secure=True, httponly=False, samesite="lax")
        return response


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid auth time")
    return value.astimezone(UTC)
