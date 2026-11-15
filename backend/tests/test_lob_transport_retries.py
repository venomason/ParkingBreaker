"""Exercise the installed SDK transport against loopback faults, including warmed pools."""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal

import pytest
from lob_python.api_client import ApiClient  # type: ignore[import-untyped]
from lob_python.configuration import Configuration  # type: ignore[import-untyped]
from urllib3.connection import HTTPConnection
from urllib3.util import Retry

from mail_samples import make_request
from parkingbreaker import lob_client
from parkingbreaker.lob_submission import LobResponse


@pytest.mark.parametrize(
    "construction", ["default", "injected", "warmed", "retry_all", "warmed_retry_all", "proxy"]
)
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("fault", ["timeout", "disconnect", "unavailable", "refused"])
def test_sdk_fault_makes_one_attempt(
    construction: str,
    method: str,
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, bytes, str | None]] = []
    connections: list[int] = []
    release = threading.Event()
    fault_active = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            self.respond()

        def do_POST(self) -> None:
            self.respond()

        def respond(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append((self.command, body, self.headers.get("Idempotency-Key")))
            if fault_active and fault == "timeout":
                release.wait(2)
                return
            if fault_active and fault == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            payload = b'{"object":"list","data":[],"count":0,"next_url":null}'
            self.send_response(503 if fault_active else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    configuration = Configuration(host=base + "/v1", username="test_synthetic")
    if "retry_all" in construction:
        configuration.retries = Retry(total=3, allowed_methods=None, status_forcelist=[503])
    if construction == "proxy":
        configuration.host = "http://synthetic.invalid/v1"
        configuration.proxy = base

    original_connect = HTTPConnection.connect

    def connect(connection: HTTPConnection) -> None:
        assert connection.host == "127.0.0.1"
        assert connection.port == server.server_port
        connections.append(connection.port)
        original_connect(connection)

    monkeypatch.setattr(HTTPConnection, "connect", connect)
    api = None
    stopped = False
    try:
        if construction == "default":

            def local_configuration(**kwargs: object) -> Configuration:
                return Configuration(host=base + "/v1", **kwargs)

            monkeypatch.setattr(lob_client, "Configuration", local_configuration)
            client = lob_client.LobSdkClient("test_synthetic", "2024-01-01", 0.05)
            api = client._api_client
        else:
            api = ApiClient(configuration)
            manager = api.rest_client.pool_manager
            if construction.startswith("warmed"):
                assert manager.request("GET", base + "/warm", timeout=1).status == 200
            client = lob_client.LobSdkClient("test_synthetic", "2024-01-01", 0.05, api_client=api)
            assert api.rest_client.pool_manager is manager
        requests.clear()
        connections.clear()
        if fault == "refused":
            server.shutdown()
            server.server_close()
            stopped = True
        fault_active = True
        if method == "GET":
            with pytest.raises(lob_client.MetadataLookupFailure):
                client.find_by_metadata("synthetic-token")
        else:
            response = client.create_letter(make_request(), "synthetic-stable-key")
        assert len(connections) == 1
        assert len(requests) == (0 if fault == "refused" else 1)
        if method == "POST":
            error = (
                "provider response uncertain"
                if fault == "unavailable"
                else "provider request uncertain"
            )
            assert response == LobResponse(None, error=error)
        if method == "POST" and requests:
            assert requests[0][2] == "synthetic-stable-key"
            assert b"Content-Disposition: form-data;" in requests[0][1]
    finally:
        release.set()
        if not stopped:
            server.shutdown()
            server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()
        if api is not None:
            api.close()


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_warmed_proxy_retains_tls_and_transport_settings(
    scheme: Literal["http", "https"],
) -> None:
    """Inspect pool selection without making a network request, including HTTPS proxies."""
    configuration = Configuration(host=f"{scheme}://synthetic.invalid/v1")
    configuration.proxy = "http://127.0.0.1:1"
    configuration.ssl_ca_cert = "synthetic-ca.pem"
    configuration.retries = Retry(total=3, allowed_methods=None)
    api = ApiClient(configuration)
    try:
        manager = api.rest_client.pool_manager
        old_pool = manager.connection_from_url(configuration.host)
        lob_client.LobSdkClient("test_synthetic", "2024-01-01", 1, api_client=api)
        new_pool = manager.connection_from_url(configuration.host)
        assert api.rest_client.pool_manager is manager
        assert old_pool.retries.total == 3
        assert Retry.from_int(new_pool.retries).total == 0
        assert new_pool is not old_pool
        assert manager.connection_pool_kw["ca_certs"] == "synthetic-ca.pem"
        assert manager.proxy.host == "127.0.0.1"
    finally:
        api.close()
