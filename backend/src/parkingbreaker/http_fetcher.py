"""The real research Fetcher: one plain HTTP GET per call (v8 §2.1, bench task T1).

The only module that makes research HTTP requests. `research.sources.fetch_source` owns the
allowlist, redirects and parsing; this adapter only performs a single request and refuses
to reach private networks:

- never follows redirects (a 3xx comes back as a response for fetch_source to check);
- refuses hosts that resolve to private, loopback, link-local, multicast or reserved
  addresses, and any URL that is not plain http(s) on the default port;
- reads at most MAX_BODY_BYTES + 1 bytes, so fetch_source can reject oversize bodies;
- waits so that one host gets at most one request per MIN_HOST_INTERVAL_S;
- turns network errors and timeouts into FetchError without echoing response content.

Production connections use the validated numeric addresses directly, while keeping
the original HTTP Host and TLS hostname verification. Environment proxies are not
used. One elapsed deadline covers connection, TLS, headers and body; a deadline
interrupts the socket rather than waiting for the next idle-read timeout.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from parkingbreaker.research.sources import MAX_BODY_BYTES, FetchError, FetchResponse

USER_AGENT = "ParkingBreakerResearch/1.0 (official-page research; +https://parkingbreaker.com)"
TIMEOUT_S = 15.0
MIN_HOST_INTERVAL_S = 1.0

Resolver = Callable[[str], Sequence[str]]


class Transport(Protocol):
    def __call__(
        self, url: str, headers: Mapping[str, str], timeout: float, max_bytes: int
    ) -> FetchResponse: ...


def system_resolver(host: str) -> tuple[str, ...]:
    """Every address the system resolver gives for `host`."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        raise FetchError(f"cannot resolve {host}") from exc
    return tuple(sorted({str(info[4][0]) for info in infos}))


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class _Connection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, timeout: float, addresses: Sequence[str] | None):
        super().__init__(host, port, timeout=timeout)
        self.addresses = addresses
        self.deadline = time.monotonic() + timeout
        self.active_socket: socket.socket | None = None

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("elapsed deadline")
        return remaining

    def connect(self) -> None:
        if self.addresses is None:
            # Direct transport calls are also used by local socket tests. The
            # production HttpFetcher always supplies the already vetted addresses.
            super().connect()
            self.active_socket = self.sock
            return
        last: OSError | None = None
        for address in self.addresses:
            remaining = self.remaining()
            sock = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET)
            self.sock = sock
            self.active_socket = sock
            try:
                sock.settimeout(remaining)
                sock.connect((address, self.port))
                return
            except OSError as exc:
                sock.close()
                last = exc
        raise last if last is not None else OSError("no connection addresses")


class _TlsConnection(_Connection):
    # HTTPConnection uses this port when formatting the Host header.
    default_port = http.client.HTTPS_PORT

    def connect(self) -> None:
        super().connect()
        assert self.sock is not None
        secure = ssl.create_default_context().wrap_socket(
            self.sock, server_hostname=self.host, do_handshake_on_connect=False
        )
        self.sock = secure
        self.active_socket = secure
        secure.settimeout(self.remaining())
        secure.do_handshake()


def urllib_transport(
    url: str,
    headers: Mapping[str, str],
    timeout: float,
    max_bytes: int,
    *,
    addresses: Sequence[str] | None = None,
) -> FetchResponse:
    """One direct GET; retain the historical transport seam, never follow redirects."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or timeout <= 0 or max_bytes < 1:
        raise FetchError("request failed: invalid transport arguments")
    connection_type = _TlsConnection if parts.scheme == "https" else _Connection
    connection = connection_type(
        parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), timeout, addresses
    )
    expired = threading.Event()

    def interrupt() -> None:
        expired.set()
        sock = connection.active_socket or connection.sock
        if sock is not None:
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()

    deadline = time.monotonic() + timeout
    timer = threading.Timer(timeout, interrupt)
    timer.daemon = True
    timer.start()
    try:
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        connection.request("GET", target, headers=dict(headers))
        with connection.getresponse() as response:
            body = response.read(max_bytes)
            status = int(response.status)
            raw_headers = response.getheaders()
        if expired.is_set() or time.monotonic() >= deadline:
            raise TimeoutError("elapsed deadline")
    except (http.client.HTTPException, OSError, ValueError) as exc:
        raise FetchError(f"request failed: {type(exc).__name__}") from None
    finally:
        timer.cancel()
        connection.close()
    return FetchResponse(
        url=url,
        status=status,
        headers={key.lower(): value for key, value in raw_headers},
        body=body,
    )


@dataclass
class HttpFetcher:
    """`research.sources.Fetcher` over real HTTP, with private-network refusal."""

    resolver: Resolver = system_resolver
    transport: Transport = urllib_transport
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    timeout_s: float = TIMEOUT_S
    _last_request: dict[str, float] = field(default_factory=dict, init=False, repr=False)

    def get(self, url: str) -> FetchResponse:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise FetchError("malformed URL") from exc
        if parts.scheme not in {"http", "https"}:
            raise FetchError(f"unsupported scheme: {parts.scheme}")
        host = (parts.hostname or "").lower()
        if not host or parts.username is not None or parts.password is not None:
            raise FetchError("URL must name a host and carry no credentials")
        if port is not None and port != (443 if parts.scheme == "https" else 80):
            raise FetchError("non-default port refused")
        addresses = self.resolver(host)
        if not addresses or not all(_is_public(a) for a in addresses):
            raise FetchError(f"{host} does not resolve only to public addresses")
        self._pace(host)
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html, application/pdf, text/plain"}
        if self.transport is urllib_transport:
            return urllib_transport(
                url, headers, self.timeout_s, MAX_BODY_BYTES + 1, addresses=addresses
            )
        return self.transport(url, headers, self.timeout_s, MAX_BODY_BYTES + 1)

    def _pace(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = MIN_HOST_INTERVAL_S - (self.monotonic() - last)
            if wait > 0:
                self.sleep(wait)
        self._last_request[host] = self.monotonic()
