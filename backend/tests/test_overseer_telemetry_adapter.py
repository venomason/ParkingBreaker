"""Offline hostile-upstream and read-only boundary probes; no network sockets."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from parkingbreaker.api.schemas import OverseerFeed
from parkingbreaker.overseer_telemetry_adapter import (
    FEED_PATH,
    AdapterSecretStore,
    OverseerTelemetryAdapter,
)
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
SECRET = "synthetic-overseer-credential-0123456789"
PRIVATE = "Alex-private-alex@example.test-123-Example-Street"
ORIGIN = "https://worker.private.invalid"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def identifier(number: int) -> str:
    return f"op1_{number:032x}"


def case(number: int) -> dict[str, Any]:
    return {
        "ops_id": identifier(number),
        "letter_state": "FROZEN",
        "payment_state": "AUTHORIZED",
        "attempt": 1,
        "age_hours": 2,
        "payment_due_at": None,
        "submission_due_at": None,
        "held": False,
        "hold_generation": None,
        "hold_reason": None,
        "review_due_at": None,
        "stuck": None,
        "error_class": None,
        "ai_settled_cents": 3,
        "ai_unsettled_reserved_cents": 5,
        "ai_unknown_cost_calls": 1,
    }


def feed(numbers: tuple[int, ...] = (1,), *, more: bool = False) -> dict[str, Any]:
    return {
        "generated_at": NOW.isoformat(),
        "freshness": {
            "state": "fresh",
            "stale": False,
            "last_started_at": (NOW - timedelta(seconds=10)).isoformat(),
            "last_finished_at": NOW.isoformat(),
            "last_successful_finished_at": NOW.isoformat(),
            "last_outcome": "ok",
            "unfinished_runs": 0,
            "error_class": None,
        },
        "alerts": [{"kind": "disk_low", "severity": "warning", "domain": "disk", "count": 1}],
        "cases": [case(n) for n in numbers],
        "next_after": identifier(numbers[-1]) if more else None,
    }


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, delay: float = 0) -> None:
        self.chunks = chunks
        self.delay = delay
        self.read = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.read += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def response(
    data: dict[str, Any] | None = None,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/json", **(headers or {})},
        stream=Stream([body if body is not None else json.dumps(data or feed()).encode()]),
    )


def adapter(
    handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any
) -> OverseerTelemetryAdapter:
    return OverseerTelemetryAdapter(
        ORIGIN,
        AdapterSecretStore(SECRET),
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
        **kwargs,
    )


@pytest.mark.anyio
async def test_health_projects_only_counts_and_freshness_without_credentials(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []
    # Ambient proxies and netrc must not affect a fixed private connection.
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid:8888")
    monkeypatch.setenv("NETRC", "/no-such-private-file")

    def serve(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response(headers={"x-untrusted-detail": PRIVATE})

    reader = adapter(serve)
    result = await reader.call_tool("get_service_health")
    assert result["status"] == "ok"
    assert result["last_successful_observation_at"] == NOW.isoformat()
    assert "cases" not in result
    assert result["alerts"] == feed()["alerts"]
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "GET"
    assert str(request.url) == ORIGIN + FEED_PATH + "?limit=1"
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert "cookie" not in request.headers
    assert request.headers["accept-encoding"] == "identity"
    assert request.content == b""
    assert set(request.extensions["timeout"].values()) == {10}
    public = json.dumps(result) + repr(reader) + repr(AdapterSecretStore(SECRET)) + caplog.text
    assert SECRET not in public
    assert PRIVATE not in public


@pytest.mark.parametrize(
    "origin",
    [
        "http://worker.private.invalid",
        "file:///etc/passwd",
        "https://user:password@host",
        "https://host/internal/dispatch",
        "https://host?token=secret",
        "https://host#fragment",
        "https://host\\@evil.invalid",
        "https://host\n",
        "https://host%2fevil",
        "https://",
        "https://host:0",
        "https://host:65536",
        "https://host:",
        "http://127.0.0.1",
        "https://[::1",
        "https://host/../internal/overseer/feed",
    ],
)
def test_rejects_untrusted_origins_without_echoing_input(origin: str) -> None:
    with pytest.raises(ValueError, match=r"^invalid_feed_origin$"):
        OverseerTelemetryAdapter(origin, AdapterSecretStore(SECRET))


@pytest.mark.parametrize("origin", ["http://127.0.0.1:9876", "http://[::1]:9876"])
@pytest.mark.anyio
async def test_loopback_requires_explicit_opt_in(origin: str) -> None:
    requests = []

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response()

    reader = OverseerTelemetryAdapter(
        origin,
        AdapterSecretStore(SECRET),
        allow_loopback_test_origin=True,
        transport=httpx.MockTransport(serve),
        clock=lambda: NOW,
    )
    assert (await reader.get_service_health())["status"] == "ok"
    assert str(requests[0].url).startswith(origin + FEED_PATH)
    with pytest.raises(ValueError, match="invalid_feed_origin"):
        OverseerTelemetryAdapter(
            "http://192.168.0.1", AdapterSecretStore(SECRET), allow_loopback_test_origin=True
        )


@pytest.mark.parametrize("where", ["root", "freshness", "alert", "case"])
@pytest.mark.parametrize(
    "field", ["email", "address", "token", "cookie", "authorization", "prompt", "__proto__"]
)
@pytest.mark.anyio
async def test_rejects_unknown_fields_at_every_depth(
    where: str, field: str, caplog: pytest.LogCaptureFixture
) -> None:
    body = feed()
    targets = {
        "root": body,
        "freshness": body["freshness"],
        "alert": body["alerts"][0],
        "case": body["cases"][0],
    }
    targets[where][field] = PRIVATE + SECRET
    result = await adapter(lambda _: response(body)).list_case_operations()
    assert result["status"] == "unavailable"
    assert result["error_code"] == "schema_violation"
    assert result["pagination_complete"] is False
    assert "cases" not in result
    assert PRIVATE not in json.dumps(result) + caplog.text
    assert SECRET not in json.dumps(result) + caplog.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ops_id", PRIVATE),
        ("ops_id", identifier(1) + "\n"),
        ("ops_id", "op99999_" + "a" * 32),
        ("letter_state", PRIVATE),
        ("payment_state", "Bearer " + SECRET),
        ("held", "false"),
        ("attempt", True),
        ("attempt", 1.0),
        ("age_hours", -1),
        ("age_hours", "123"),
        ("age_hours", 2**31),
        ("payment_due_at", "2026-11-08T12:00:00"),
        ("payment_due_at", 1794139200),
        ("hold_reason", PRIVATE),
        ("error_class", PRIVATE),
        ("stuck", PRIVATE),
        ("ai_settled_cents", float("nan")),
        ("ai_unsettled_reserved_cents", True),
        ("ai_unknown_cost_calls", 1.5),
    ],
)
@pytest.mark.anyio
async def test_rejects_pii_and_noncanonical_scalars_in_allowed_fields(
    field: str, value: Any
) -> None:
    body = feed()
    body["cases"][0][field] = value
    result = await adapter(lambda _: response(body)).get_service_health()
    assert result["error_code"] == "schema_violation"
    assert PRIVATE not in json.dumps(result)
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize(
    "body", [b'{"cases":[],"cases":[]}', b"[]", b"null", b"{bad json", b"\xff", b"[" * 2000]
)
@pytest.mark.anyio
async def test_malformed_duplicate_and_deep_json_are_safe(body: bytes) -> None:
    assert (await adapter(lambda _: response(body=body)).get_service_health()) == {
        "status": "unavailable",
        "error_code": "schema_violation",
        "last_successful_observation_at": None,
    }


@pytest.mark.anyio
async def test_even_schema_valid_reflection_of_credential_is_rejected() -> None:
    secret = identifier(1)  # a malicious server can reflect a secret in an allowed field
    reader = OverseerTelemetryAdapter(
        ORIGIN,
        AdapterSecretStore(secret),
        transport=httpx.MockTransport(lambda _: response()),
        clock=lambda: NOW,
    )
    assert (await reader.get_service_health())["error_code"] == "schema_violation"


@pytest.mark.anyio
async def test_upstream_reason_headers_and_exceptions_cannot_leak_via_debug_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    def serve(_: httpx.Request) -> httpx.Response:
        # Exercise actual httpx logging of the remote reason phrase, and the same
        # logger names used by httpcore's raw-header/exception trace diagnostics.
        for name in ("httpcore2.connection", "httpcore2.http11", "httpcore2.http2"):
            logging.getLogger(name).debug("untrusted header %s %s", PRIVATE, SECRET)
        result = response(status=500, headers={"set-cookie": PRIVATE})
        result.extensions["reason_phrase"] = (SECRET + PRIVATE).encode()
        return result

    reader = adapter(serve)
    assert (await reader.get_service_health())["error_code"] == "upstream_unavailable"
    assert SECRET not in caplog.text
    assert PRIVATE not in caplog.text
    logging.getLogger("httpx2").info("outside collection remains visible")
    assert "outside collection remains visible" in caplog.text


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("destination", ["https://evil.invalid/steal", "/internal/dispatch"])
@pytest.mark.anyio
async def test_redirects_never_send_a_second_request(status: int, destination: str) -> None:
    requests = []

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(status=status, headers={"location": destination, "set-cookie": PRIVATE})

    result = await adapter(serve).get_service_health()
    assert result["error_code"] == "redirect_rejected"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "headers",
    [
        {"set-cookie": PRIVATE},
        {"cookie": PRIVATE},
        {"authorization": SECRET},
        {"proxy-authorization": SECRET},
        {"content-type": "text/html"},
        {"content-encoding": "gzip"},
        {"content-length": "-1"},
        {"content-length": "1, 2"},
    ],
)
@pytest.mark.anyio
async def test_forbidden_headers_and_compression_fail_closed(headers: dict[str, str]) -> None:
    result = await adapter(lambda _: response(headers=headers)).get_service_health()
    assert result["error_code"] == "schema_violation"
    assert PRIVATE not in json.dumps(result)
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (401, "auth_expired"),
        (403, "auth_expired"),
        (500, "upstream_unavailable"),
        (204, "upstream_unavailable"),
        (429, "upstream_unavailable"),
        ("timeout", "upstream_timeout"),
        ("connection", "upstream_unavailable"),
        ("schema", "schema_violation"),
    ],
)
@pytest.mark.anyio
async def test_failures_retain_last_complete_observation_and_drop_partial_results(
    failure: int | str, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    fail = False

    def serve(request: httpx.Request) -> httpx.Response:
        if not fail:
            return response()
        if failure == "timeout":
            raise httpx.ReadTimeout(PRIVATE + SECRET, request=request)
        if failure == "connection":
            raise httpx.ConnectError(PRIVATE + SECRET, request=request)
        if failure == "schema":
            return response({"cookie": PRIVATE})
        assert isinstance(failure, int)
        return response(status=failure, body=(PRIVATE + SECRET).encode())

    reader = adapter(serve)
    assert (await reader.list_case_operations())["status"] == "ok"
    fail = True
    for result in (await reader.list_case_operations(), await reader.get_service_health()):
        assert result["status"] == "unavailable"
        assert result["error_code"] == code
        assert result["last_successful_observation_at"] == NOW.isoformat()
        assert "cases" not in result
        assert "alerts" not in result
        assert PRIVATE not in json.dumps(result) + caplog.text
        assert SECRET not in json.dumps(result) + caplog.text


@pytest.mark.anyio
async def test_total_timeout_bounds_slow_stream_and_closes_response() -> None:
    stream = Stream([b"{", b"}"], delay=0.04)
    reader = adapter(
        lambda _: httpx.Response(200, headers={"content-type": "application/json"}, stream=stream),
        timeout_seconds=0.01,
    )
    result = await reader.list_case_operations()
    assert result["error_code"] == "upstream_timeout"
    assert stream.closed
    assert result["pages_fetched"] == 0
    assert result["pagination_complete"] is False


@pytest.mark.anyio
async def test_total_timeout_is_not_reset_between_pages() -> None:
    requests = 0

    async def serve(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        await asyncio.sleep(0.04)
        return response(feed((requests,), more=True))

    reader = OverseerTelemetryAdapter(
        ORIGIN,
        AdapterSecretStore(SECRET),
        transport=httpx.MockTransport(serve),
        clock=lambda: NOW,
        page_size=1,
        timeout_seconds=0.065,
    )
    result = await reader.list_case_operations()
    assert result["error_code"] == "upstream_timeout"
    assert result["pages_fetched"] == 1
    assert result["pagination_complete"] is False
    assert "cases" not in result
    assert requests == 2


@pytest.mark.anyio
async def test_later_page_failure_cannot_advance_last_successful_observation() -> None:
    partial = False

    def serve(request: httpx.Request) -> httpx.Response:
        if not partial:
            return response()
        if request.url.params.get("after"):
            return response(status=401)
        body = feed(more=True)
        body["generated_at"] = (NOW + timedelta(seconds=10)).isoformat()
        return response(body)

    reader = adapter(serve, page_size=1)
    assert (await reader.list_case_operations())["status"] == "ok"
    partial = True
    result = await reader.list_case_operations()
    assert result["error_code"] == "auth_expired"
    assert result["pages_fetched"] == 1
    assert result["last_successful_observation_at"] == NOW.isoformat()
    assert result["pagination_complete"] is False
    assert "cases" not in result


@pytest.mark.parametrize("declared", [True, False])
@pytest.mark.anyio
async def test_byte_limit_before_buffering_and_early_close(declared: bool) -> None:
    stream = Stream([b"x" * 65, b"unread"])
    headers = {"content-type": "application/json"}
    if declared:
        headers["content-length"] = "65"
    result = await adapter(
        lambda _: httpx.Response(200, headers=headers, stream=stream), max_response_bytes=64
    ).get_service_health()
    assert result["error_code"] == "response_too_large"
    assert stream.closed
    assert stream.read == (0 if declared else 1)


@pytest.mark.anyio
async def test_pagination_queries_and_collection_evidence() -> None:
    requests = []

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(feed((len(requests),), more=len(requests) < 3))

    result = await adapter(serve, page_size=1, max_pages=3).list_case_operations()
    assert result["status"] == "ok"
    assert result["pages_fetched"] == 3
    assert result["pagination_complete"] is True
    assert result["collection_started_at"] == result["collection_finished_at"] == NOW.isoformat()
    assert result["page_observation_times"] == [NOW.isoformat()] * 3
    assert result["cases"] == [case(1), case(2), case(3)]
    assert [dict(r.url.params) for r in requests] == [
        {"limit": "1"},
        {"limit": "1", "after": identifier(1)},
        {"limit": "1", "after": identifier(2)},
    ]
    assert all(r.method == "GET" and r.url.path == FEED_PATH for r in requests)


@pytest.mark.parametrize("bound", ["pages", "collection_bytes"])
@pytest.mark.anyio
async def test_pagination_limits_discard_partial_feed(bound: str) -> None:
    requests = []

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(feed((len(requests),), more=True))

    options = (
        {"max_pages": 2}
        if bound == "pages"
        else {"max_collection_bytes": len(json.dumps(feed(more=True)).encode()) + 20}
    )
    result = await adapter(serve, page_size=1, **options).list_case_operations()
    assert result["error_code"] == (
        "pagination_limit" if bound == "pages" else "response_too_large"
    )
    assert result["pages_fetched"] == (2 if bound == "pages" else 1)
    assert result["pagination_complete"] is False
    assert result["last_successful_observation_at"] is None
    assert "cases" not in result
    assert len(requests) == 2


@pytest.mark.parametrize(
    "defect", ["repeat", "backwards", "cursor", "short", "oversized", "duplicate"]
)
@pytest.mark.anyio
async def test_pagination_rejects_duplicates_nonadvancing_and_arbitrary_cursors(
    defect: str,
) -> None:
    calls = 0

    def serve(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return response(feed((2,), more=True))
        body = feed((3,))
        if defect == "repeat":
            body = feed((2,), more=True)
        elif defect == "backwards":
            body = feed((1,))
        elif defect == "cursor":
            body["next_after"] = "https://evil.invalid/?token=" + SECRET
        elif defect == "short":
            body = feed(())
            body["next_after"] = identifier(3)
        elif defect == "oversized":
            body = feed((3, 4))
        elif defect == "duplicate":
            body = feed((3, 3))
        return response(body)

    result = await adapter(serve, page_size=1).list_case_operations()
    assert result["error_code"] == "schema_violation"
    assert result["pages_fetched"] == 1
    assert calls == 2
    assert "cases" not in result


@pytest.mark.parametrize("mode", ["stale_flag", "stale_state", "old_snapshot", "future_snapshot"])
@pytest.mark.anyio
async def test_freshness_never_converts_stale_or_future_snapshot_to_healthy(mode: str) -> None:
    body = feed()
    if mode == "stale_flag":
        body["freshness"]["stale"] = True
    elif mode == "stale_state":
        body["freshness"]["state"] = "failed"
    elif mode == "old_snapshot":
        body["generated_at"] = (NOW - timedelta(minutes=6)).isoformat()
    else:
        body["generated_at"] = (NOW + timedelta(minutes=6)).isoformat()
    result = await adapter(lambda _: response(body)).get_service_health()
    assert result["status"] == ("unavailable" if mode == "future_snapshot" else "stale")


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("hold_case", {}),
        ("resume_case", {}),
        ("retry_case", {}),
        ("dispatch", {}),
        ("POST", {}),
        ("get_service_health", {"url": "https://evil.invalid"}),
        ("get_service_health", {"method": "POST"}),
        ("list_case_operations", {"after": PRIVATE}),
        ("get_released_cohort_report", {"distinct_customers": 10, "qualified": True}),
    ],
)
@pytest.mark.anyio
async def test_denied_writes_and_overrides_never_read_secrets_or_contact_upstream(
    name: str, arguments: dict[str, Any]
) -> None:
    class UnusedSecrets:
        def read_feed_secret(self) -> str:
            pytest.fail("forbidden tool read a credential")

    def never(_: httpx.Request) -> httpx.Response:
        pytest.fail("forbidden tool made a request")

    reader = OverseerTelemetryAdapter(ORIGIN, UnusedSecrets(), transport=httpx.MockTransport(never))
    assert (await reader.call_tool(name, arguments))["error_code"] == "operation_not_allowed"


@pytest.mark.parametrize("count", [0, 5, 9, 10, 11, 100])
@pytest.mark.anyio
async def test_operations_never_qualify_a_released_customer_cohort(count: int) -> None:
    reader = adapter(lambda _: response(feed(tuple(range(1, count + 1)))))
    assert (await reader.list_case_operations())["status"] == "ok"
    assert reader.get_released_cohort_report() == {
        "status": "unavailable",
        "error_code": "cohort_export_unqualified",
        "minimum_distinct_customers": 10,
        "last_successful_observation_at": None,
    }
    assert (
        await reader.call_tool("get_released_cohort_report") == reader.get_released_cohort_report()
    )


@pytest.mark.anyio
async def test_secret_store_failure_and_expiry_are_fixed_safe_errors() -> None:
    class ExpiredStore:
        def read_feed_secret(self) -> str:
            raise RuntimeError(PRIVATE + SECRET)

    reader = OverseerTelemetryAdapter(ORIGIN, ExpiredStore())
    assert (await reader.get_service_health()) == {
        "status": "unavailable",
        "error_code": "credential_unavailable",
        "last_successful_observation_at": None,
    }
    with pytest.raises(ValueError, match=r"^credential_unavailable$"):
        AdapterSecretStore("short\n" + PRIVATE)


@pytest.mark.parametrize(
    "limits",
    [
        {"max_pages": 0},
        {"max_pages": 21},
        {"max_pages": True},
        {"page_size": 101},
        {"max_response_bytes": 1_048_577},
        {"max_collection_bytes": 5_242_881},
        {"timeout_seconds": 0},
        {"timeout_seconds": 31},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": True},
    ],
)
def test_limits_have_hard_configuration_ceilings(limits: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"^invalid_collection_limits$"):
        adapter(lambda _: response(), **limits)


@pytest.mark.anyio
async def test_real_store_feed_passes_wire_contract_without_mutating_database(
    tmp_path: Path,
) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        store.configure_ops_ids(bytes(range(32)))
        store.save_workspace("private-workspace", Workspace(notes=PRIVATE, position=PRIVATE), NOW)
        store.start_case("private-workspace", "private-authorization", NOW, NOW + timedelta(days=5))
        before = tuple(store._require_open().iterdump())

        def serve(request: httpx.Request) -> httpx.Response:
            page = store.overseer_page(NOW, request.url.params.get("after"), 100)
            wire = OverseerFeed.model_validate(page, from_attributes=True).model_dump(mode="json")
            return response(wire)

        result = await adapter(serve).list_case_operations()
        assert result["status"] == "stale"  # no sweep has run; no invented health
        assert result["pagination_complete"] is True
        assert result["pages_fetched"] == 1
        assert PRIVATE not in json.dumps(result)
        assert tuple(store._require_open().iterdump()) == before
