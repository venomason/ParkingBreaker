"""Private, read-only boundary over T75; no command, proxy or marketing export.

Construct once with an operator-selected origin and adapter-owned secret store. Only
the three argument-free tools are client-facing. Transport/clock injection is for local
tests, never a tool argument. The last successful observation is process-local: restart
reports None until another complete validated observation succeeds. Case pages are
separate snapshots, not an atomic database export. Partial collections are discarded.

No T32 release connector is qualified here. Even ten operations IDs cannot establish
ten distinct customers or the persisted anti-differencing state of a released export.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import re
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from urllib.parse import urlsplit

import httpx2 as httpx

from parkingbreaker.api.schemas import OverseerFeed
from parkingbreaker.json_types import JsonValue
from parkingbreaker.oplog.cohort_report import DEFAULT_MIN_COHORT
from parkingbreaker.overseer_feed import OPS_ID_PATTERN

FEED_PATH = "/internal/overseer/feed"
MIN_COHORT_CUSTOMERS = max(10, DEFAULT_MIN_COHORT)
_OPS_ID = re.compile(OPS_ID_PATTERN)
_TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)")
_SECRET = re.compile(r"[\x21-\x7e]{24,256}")
_MAX_COUNT = 2**31 - 1
_PRIVATE_COLLECTION: ContextVar[bool] = ContextVar("overseer_private_collection", default=False)


class _PrivateHTTPLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # httpx logs upstream reason phrases; httpcore DEBUG logs headers/exceptions.
        # Drop those records only inside this collection, including cleanup. Other
        # tasks retain their logs; changing process-wide log levels would not be safe.
        return not _PRIVATE_COLLECTION.get()


_HTTP_LOG_FILTER = _PrivateHTTPLogs()


def _guard_http_logs() -> None:
    for name in ("httpx2", "httpcore2.connection", "httpcore2.http11", "httpcore2.http2"):
        logging.getLogger(name).addFilter(_HTTP_LOG_FILTER)


class FeedSecretStore(Protocol):
    """Trusted private storage; callers of the tools never receive this interface."""

    def read_feed_secret(self) -> str: ...


class AdapterSecretStore:
    """Minimal in-memory store; a deployment can supply its own secret-store reader."""

    __slots__ = ("__secret",)

    def __init__(self, secret: str) -> None:
        if not isinstance(secret, str) or _SECRET.fullmatch(secret) is None:
            raise ValueError("credential_unavailable")
        self.__secret = secret

    def read_feed_secret(self) -> str:
        return self.__secret

    def __repr__(self) -> str:
        return "AdapterSecretStore(<redacted>)"


class _Refused(Exception):
    """Only locally selected constant codes may be constructed here."""


def _fixed_url(origin: str, allow_loopback_test_origin: bool) -> str:
    try:
        if not isinstance(origin, str) or re.search(r"[\s\\%?#]", origin):
            raise ValueError
        parts = urlsplit(origin)
        host = parts.hostname
        if (
            not host
            or parts.username is not None
            or parts.password is not None
            or parts.path not in ("", "/")
            or (parts.port is not None and not 1 <= parts.port <= 65535)
            or parts.netloc.endswith(":")
        ):
            raise ValueError
        loopback = False
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            if re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?", host) is None:
                raise ValueError from None
        if parts.scheme != "https" and not (
            parts.scheme == "http" and allow_loopback_test_origin and loopback
        ):
            raise ValueError
        return f"{parts.scheme}://{parts.netloc}{FEED_PATH}"
    except (ValueError, TypeError):
        raise ValueError("invalid_feed_origin") from None


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise _Refused("schema_violation")
        result[key] = value
    return result


def _check_scalars(value: JsonValue, secret: str, key: str = "", depth: int = 0) -> None:
    # The wire schema has no free-text fields. Enforce bounds before normalization,
    # so type coercion, duplicate JSON keys and timestamp normalization cannot hide data.
    if depth > 5:
        raise _Refused("schema_violation")
    if isinstance(value, dict):
        for name, item in value.items():
            if secret in name:
                raise _Refused("schema_violation")
            _check_scalars(item, secret, name, depth + 1)
    elif isinstance(value, list):
        if len(value) > 100:
            raise _Refused("schema_violation")
        for item in value:
            _check_scalars(item, secret, key, depth + 1)
    elif isinstance(value, bool):
        if key not in {"held", "stale"}:
            raise _Refused("schema_violation")
    elif isinstance(value, int):
        if not 0 <= value <= _MAX_COUNT or key.endswith("_at"):
            raise _Refused("schema_violation")
    elif isinstance(value, float):
        raise _Refused("schema_violation")
    elif isinstance(value, str):
        if len(value) > 64 or secret in value:
            raise _Refused("schema_violation")
        if key.endswith("_at") and _TIMESTAMP.fullmatch(value) is None:
            raise _Refused("schema_violation")
        if key in {"ops_id", "next_after"} and (
            _OPS_ID.fullmatch(value) is None or int(value.split("_", 1)[0][2:]) > 65535
        ):
            raise _Refused("schema_violation")


def _page(body: bytes, secret: str, limit: int, after: str | None) -> OverseerFeed:
    try:
        raw: JsonValue = json.loads(body, object_pairs_hook=_unique_object)
        _check_scalars(raw, secret)
        page = OverseerFeed.model_validate_json(body, strict=True)
        ids = [case.ops_id for case in page.cases]
        if len(ids) > limit or ids != sorted(set(ids)):
            raise ValueError
        if after is not None and any(value <= after for value in ids):
            raise ValueError
        if page.next_after is not None and (
            len(ids) != limit or not ids or page.next_after != ids[-1]
        ):
            raise ValueError
        return page
    except Exception:
        # Pydantic/JSON errors contain untrusted values. Never return or log them.
        raise _Refused("schema_violation") from None


class OverseerTelemetryAdapter:
    """Bounded GET-only reader. Limits are trusted configuration, not tool input."""

    def __init__(
        self,
        origin: str,
        secret_store: FeedSecretStore,
        *,
        allow_loopback_test_origin: bool = False,
        page_size: int = 100,
        max_pages: int = 10,
        max_response_bytes: int = 262_144,
        max_collection_bytes: int = 1_048_576,
        timeout_seconds: float = 10,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._url = _fixed_url(origin, allow_loopback_test_origin)
        for value, maximum in (
            (page_size, 100),
            (max_pages, 20),
            (max_response_bytes, 1_048_576),
            (max_collection_bytes, 5_242_880),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("invalid_collection_limits")
        if (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 30
        ):
            raise ValueError("invalid_collection_limits")
        self._secrets = secret_store
        self._page_size = page_size
        self._max_pages = max_pages
        self._max_response_bytes = max_response_bytes
        self._max_collection_bytes = max_collection_bytes
        self._timeout = timeout_seconds
        self._transport = transport
        self._clock = clock
        self._last_successful_observation_at: str | None = None

    def _unavailable(self, code: str) -> dict[str, JsonValue]:
        return {
            "status": "unavailable",
            "error_code": code,
            "last_successful_observation_at": self._last_successful_observation_at,
        }

    async def call_tool(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> dict[str, JsonValue]:
        """Closed bridge entry point: no write names, URL, method or query overrides."""
        if arguments:
            return self._unavailable("operation_not_allowed")
        if name == "get_service_health":
            return await self.get_service_health()
        if name == "list_case_operations":
            return await self.list_case_operations()
        if name == "get_released_cohort_report":
            return self.get_released_cohort_report()
        return self._unavailable("operation_not_allowed")

    def get_released_cohort_report(self) -> dict[str, JsonValue]:
        # Fail closed until a trusted released export with >=10 DISTINCT customers,
        # cell suppression and durable repeated-export protections is integrated.
        # Never accept caller-supplied counts/qualification flags or derive from cases.
        return {
            "status": "unavailable",
            "error_code": "cohort_export_unqualified",
            "minimum_distinct_customers": MIN_COHORT_CUSTOMERS,
            "last_successful_observation_at": None,
        }

    async def get_service_health(self) -> dict[str, JsonValue]:
        return await self._collect(health_only=True)

    async def list_case_operations(self) -> dict[str, JsonValue]:
        return await self._collect(health_only=False)

    async def _collect(self, *, health_only: bool) -> dict[str, JsonValue]:
        _guard_http_logs()
        context = _PRIVATE_COLLECTION.set(True)
        try:
            return await self._observe(health_only=health_only)
        finally:
            _PRIVATE_COLLECTION.reset(context)

    async def _observe(self, *, health_only: bool) -> dict[str, JsonValue]:
        started = self._clock()
        pages: list[OverseerFeed] = []
        complete = False
        result: dict[str, JsonValue]
        try:
            async with asyncio.timeout(self._timeout):
                try:
                    secret = self._secrets.read_feed_secret()
                    if not isinstance(secret, str) or _SECRET.fullmatch(secret) is None:
                        raise ValueError
                except Exception:
                    raise _Refused("credential_unavailable") from None
                async with httpx.AsyncClient(
                    transport=self._transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=self._timeout,
                ) as client:
                    after = None
                    remaining = self._max_collection_bytes
                    limit = 1 if health_only else self._page_size
                    for _ in range(1 if health_only else self._max_pages):
                        page, size = await self._fetch(client, secret, limit, after, remaining)
                        remaining -= size
                        pages.append(page)
                        if health_only or page.next_after is None:
                            complete = True
                            break
                        after = page.next_after
                    if not complete:
                        raise _Refused("pagination_limit")
            finished = self._clock()
            # Old feed snapshots stay visibly stale, even when the upstream claims fresh.
            stale = any(
                page.freshness.stale
                or page.freshness.state.value != "fresh"
                or finished - page.generated_at > timedelta(minutes=5)
                for page in pages
            )
            if any(page.generated_at > finished + timedelta(seconds=30) for page in pages):
                raise _Refused("schema_violation")
            observed = min(page.generated_at for page in pages).isoformat()
            self._last_successful_observation_at = observed
            result = {
                "status": "stale" if stale else "ok",
                "last_successful_observation_at": observed,
            }
            if health_only:
                data = cast(dict[str, JsonValue], pages[0].model_dump(mode="json"))
                result.update(
                    observed_at=data["generated_at"],
                    freshness=data["freshness"],
                    alerts=data["alerts"],
                )
            else:
                result["cases"] = [
                    cast(JsonValue, case.model_dump(mode="json"))
                    for page in pages
                    for case in page.cases
                ]
                result["page_observation_times"] = [page.generated_at.isoformat() for page in pages]
        except _Refused as exc:
            result = self._unavailable(str(exc))
        except (TimeoutError, httpx.TimeoutException):
            result = self._unavailable("upstream_timeout")
        except Exception:
            result = self._unavailable("upstream_unavailable")
        if not health_only:
            result.update(
                collection_started_at=started.isoformat(),
                collection_finished_at=self._clock().isoformat(),
                pages_fetched=len(pages),
                pagination_complete=complete and result["status"] != "unavailable",
            )
        return result

    async def _fetch(
        self,
        client: httpx.AsyncClient,
        secret: str,
        limit: int,
        after: str | None,
        remaining: int,
    ) -> tuple[OverseerFeed, int]:
        params = {"limit": str(limit)}
        if after is not None:
            params["after"] = after
        # An explicit Request does not merge a cookie jar, netrc or default auth.
        request = httpx.Request(
            "GET",
            self._url,
            params=params,
            headers={
                "Authorization": f"Bearer {secret}",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            extensions={
                "timeout": dict.fromkeys(("connect", "read", "write", "pool"), self._timeout)
            },
        )
        response = await client.send(request, stream=True, auth=None, follow_redirects=False)
        try:
            if 300 <= response.status_code < 400:
                raise _Refused("redirect_rejected")
            if response.status_code in {401, 403}:
                raise _Refused("auth_expired")
            if response.status_code != 200:
                raise _Refused("upstream_unavailable")
            if (
                response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
                or response.headers.get("content-encoding", "identity").lower() != "identity"
                or any(
                    name in response.headers
                    for name in ("set-cookie", "authorization", "proxy-authorization", "cookie")
                )
            ):
                raise _Refused("schema_violation")
            maximum = min(self._max_response_bytes, remaining)
            length = response.headers.get("content-length")
            if length is not None and (not length.isascii() or not length.isdecimal()):
                raise _Refused("schema_violation")
            if length is not None and int(length) > maximum:
                raise _Refused("response_too_large")
            body = bytearray()
            # Raw streaming + identity encoding prevents decompression bombs. Check
            # each received chunk before retaining it; Content-Length is not trusted.
            async for chunk in response.aiter_raw():
                if len(chunk) > maximum - len(body):
                    raise _Refused("response_too_large")
                body.extend(chunk)
            return _page(bytes(body), secret, limit, after), len(body)
        finally:
            await response.aclose()
