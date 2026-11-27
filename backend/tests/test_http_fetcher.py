from __future__ import annotations

import socket
import ssl
import threading
import time
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from parkingbreaker.http_fetcher import (
    MIN_HOST_INTERVAL_S,
    USER_AGENT,
    HttpFetcher,
    _Connection,
    _TlsConnection,
    urllib_transport,
)
from parkingbreaker.research.sources import MAX_BODY_BYTES, FetchError, FetchResponse

PUBLIC = ("93.184.215.14",)


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str], float, int]] = []

    def __call__(
        self, url: str, headers: Mapping[str, str], timeout: float, max_bytes: int
    ) -> FetchResponse:
        self.calls.append((url, dict(headers), timeout, max_bytes))
        return FetchResponse(url, 200, {"content-type": "text/html"}, b"<p>ok</p>")


def fetcher(addresses: tuple[str, ...] = PUBLIC) -> tuple[HttpFetcher, RecordingTransport]:
    transport = RecordingTransport()
    return HttpFetcher(resolver=lambda _host: addresses, transport=transport), transport


def test_public_host_is_fetched_once_with_size_cap_and_user_agent() -> None:
    f, transport = fetcher()
    response = f.get("https://www.sfmta.com/contest")
    assert response.status == 200
    [(url, headers, timeout, max_bytes)] = transport.calls
    assert url == "https://www.sfmta.com/contest"
    assert headers["User-Agent"] == USER_AGENT
    assert timeout == 15.0
    assert max_bytes == MAX_BODY_BYTES + 1


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "100.64.0.1",
        "224.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "not-an-ip",
    ],
)
def test_non_public_addresses_are_refused(address: str) -> None:
    f, transport = fetcher((address,))
    with pytest.raises(FetchError, match="public"):
        f.get("https://city.example.gov/page")
    assert transport.calls == []


def test_any_private_answer_among_public_ones_is_refused() -> None:
    f, transport = fetcher(("93.184.215.14", "10.0.0.1"))
    with pytest.raises(FetchError):
        f.get("https://city.example.gov/page")
    assert transport.calls == []


def test_no_addresses_is_refused() -> None:
    f, _ = fetcher(())
    with pytest.raises(FetchError):
        f.get("https://city.example.gov/page")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://city.example.gov/page",
        "file:///etc/passwd",
        "https://city.example.gov:8443/page",
        "http://city.example.gov:443/page",
        "https://user:pw@city.example.gov/page",
        "https:///nohost",
        "https://city.example.gov:notaport/page",
    ],
)
def test_unsafe_urls_are_refused(url: str) -> None:
    f, transport = fetcher()
    with pytest.raises(FetchError):
        f.get(url)
    assert transport.calls == []


def test_default_ports_are_allowed() -> None:
    f, transport = fetcher()
    f.get("https://city.example.gov:443/a")
    f.get("http://other.example.gov:80/b")
    assert len(transport.calls) == 2


def test_same_host_is_paced_other_hosts_are_not() -> None:
    now = [100.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    f = HttpFetcher(
        resolver=lambda _h: PUBLIC,
        transport=RecordingTransport(),
        monotonic=lambda: now[0],
        sleep=sleep,
    )
    f.get("https://a.example.gov/1")
    now[0] += 0.25
    f.get("https://a.example.gov/2")
    f.get("https://b.example.gov/1")
    assert sleeps == [pytest.approx(MIN_HOST_INTERVAL_S - 0.25)]


# --- The urllib transport against a local server ----------------------------------


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/trickle":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == "/trickle-headers":
            try:
                for byte in b"HTTP/1.0 200 OK\r\nContent-Length: 0\r\n\r\n":
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://elsewhere.example.com/x")
            self.end_headers()
        elif self.path == "/missing":
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not here")
        elif self.path == "/big":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"x" * 5000)
        elif self.path == "/slow":
            time.sleep(1.0)
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("X-Echo-UA", self.headers.get("User-Agent", ""))
            self.end_headers()
            self.wfile.write(b"<p>hello</p>")


@pytest.fixture
def base_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_transport_returns_page_with_lowercase_headers(base_url: str) -> None:
    response = urllib_transport(base_url + "/", {"User-Agent": "UA-test"}, 5.0, 1000)
    assert response.status == 200
    assert response.body == b"<p>hello</p>"
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["x-echo-ua"] == "UA-test"


def test_transport_returns_redirect_without_following(base_url: str) -> None:
    response = urllib_transport(base_url + "/redirect", {}, 5.0, 1000)
    assert response.status == 302
    assert response.headers["location"] == "https://elsewhere.example.com/x"


def test_transport_returns_error_status_as_response(base_url: str) -> None:
    response = urllib_transport(base_url + "/missing", {}, 5.0, 1000)
    assert response.status == 404
    assert response.body == b"not here"


def test_transport_reads_at_most_max_bytes(base_url: str) -> None:
    response = urllib_transport(base_url + "/big", {}, 5.0, 100)
    assert response.body == b"x" * 100


def test_transport_timeout_is_fetch_error(base_url: str) -> None:
    with pytest.raises(FetchError, match="request failed"):
        urllib_transport(base_url + "/slow", {}, 0.2, 100)


def test_transport_connection_refused_is_fetch_error() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    server.server_close()
    with pytest.raises(FetchError, match="request failed"):
        urllib_transport(f"http://127.0.0.1:{port}/", {}, 2.0, 100)


@pytest.mark.parametrize("path", ["/trickle", "/trickle-headers"])
def test_elapsed_deadline_interrupts_continuously_active_socket(base_url: str, path: str) -> None:
    started = time.monotonic()
    with pytest.raises(FetchError, match="request failed"):
        urllib_transport(base_url + path, {}, 0.15, 1000)
    assert time.monotonic() - started < 0.7


def test_default_fetcher_pins_validated_addresses_without_dns_or_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def dns(*args: object, **kwargs: object) -> None:
        raise AssertionError("validated hostname must not be resolved again")

    def connect(sock: socket.socket, address: object) -> None:
        calls.append(address)
        raise OSError("synthetic connection refusal before network")

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    f = HttpFetcher(resolver=lambda _host: PUBLIC)
    with pytest.raises(FetchError, match="request failed"):
        f.get("https://city.example.gov/contest")
    assert calls == [(PUBLIC[0], 443)]


def test_https_default_port_sends_the_bare_hostname() -> None:
    connection = _TlsConnection("city.example.gov", 443, 2.0, PUBLIC)
    client, server = socket.socketpair()
    with client, server:
        connection.sock = client
        connection.request("GET", "/parking")
        client.shutdown(socket.SHUT_WR)
        with server.makefile("rb") as wire:
            headers = wire.readlines()
    assert b"Host: city.example.gov\r\n" in headers


def test_pinned_tls_keeps_original_hostname_and_certificate_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[object] = []

    def connect(connection: _Connection) -> None:
        connection.sock = socket.socket()
        connection.active_socket = connection.sock

    def wrap(context: ssl.SSLContext, sock: socket.socket, **kwargs: object) -> None:
        observed.append((kwargs["server_hostname"], context.check_hostname, context.verify_mode))
        raise ssl.SSLError("synthetic refusal before any network")

    monkeypatch.setattr(_Connection, "connect", connect)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", wrap)
    with pytest.raises(FetchError, match="request failed"):
        HttpFetcher(resolver=lambda _host: PUBLIC).get("https://city.example.gov/contest")
    assert observed == [("city.example.gov", True, ssl.CERT_REQUIRED)]
