"""Small Resend adapter and deterministic fake for outbound service email.

Email content and provider errors stay inside this boundary. Callers own durable retry
state and must reuse the same idempotency key after an uncertain outcome.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

RESEND_EMAILS_URL = "https://api.resend.com/emails"
MAX_RESPONSE_BYTES = 64 * 1024
REQUEST_TIMEOUT_SECONDS = 20.0
_HEADER_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_BODY_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

EmailStatus = Literal["accepted", "uncertain", "rejected"]


class EmailReason(StrEnum):
    INVALID_MESSAGE = "invalid_message"
    INVALID_KEY = "invalid_key"
    TRANSPORT_ERROR = "transport_error"
    INVALID_RESPONSE = "invalid_response"
    RESPONSE_TOO_LARGE = "response_too_large"
    PROVIDER_REJECTED = "provider_rejected"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    SCRIPT_EXHAUSTED = "script_exhausted"


@dataclass(frozen=True, repr=False)
class EmailMessage:
    """Immutable email request whose recipient, subject and body never appear in repr."""

    to: str
    subject: str
    text: str

    def __repr__(self) -> str:
        return "EmailMessage(<redacted>)"


@dataclass(frozen=True)
class EmailOutcome:
    status: EmailStatus
    provider_id: str | None = None
    reason: EmailReason | None = None

    def __post_init__(self) -> None:
        if self.status not in {"accepted", "uncertain", "rejected"}:
            raise ValueError("invalid email outcome status")
        if self.status == "accepted":
            if self.reason is not None or _canonical_uuid(self.provider_id) is None:
                raise ValueError("accepted email outcome requires a provider UUID")
        elif self.provider_id is not None or not isinstance(self.reason, EmailReason):
            raise ValueError("non-accepted email outcome requires a fixed reason")

    def __repr__(self) -> str:
        return (
            f"EmailOutcome(status={self.status!r}, provider_id={self.provider_id!r}, "
            f"reason={None if self.reason is None else self.reason.value!r})"
        )


class EmailClient(Protocol):
    def send(self, message: EmailMessage, key: str) -> EmailOutcome: ...


class EmailTransport(Protocol):
    def __call__(
        self, url: str, headers: Mapping[str, str], payload: bytes, timeout: float
    ) -> tuple[int, bytes]: ...


def _canonical_uuid(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        return None
    canonical = str(parsed)
    return canonical if canonical == value.lower() else None


def _valid_headers(message: EmailMessage) -> bool:
    return (
        isinstance(message, EmailMessage)
        and isinstance(message.to, str)
        and bool(message.to.strip())
        and not _HEADER_CONTROL.search(message.to)
        and isinstance(message.subject, str)
        and bool(message.subject.strip())
        and not _HEADER_CONTROL.search(message.subject)
        and isinstance(message.text, str)
        and not _BODY_CONTROL.search(message.text)
    )


def _valid_key(key: object) -> bool:
    return (
        isinstance(key, str)
        and bool(key.strip())
        and len(key) <= 256
        and not _HEADER_CONTROL.search(key)
    )


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate response field")
        result[key] = value
    return result


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: Any,
        msg: Any,
        headers: Any,
        newurl: Any,
    ) -> None:
        return None


def _urllib_transport(
    url: str, headers: Mapping[str, str], payload: bytes, timeout: float
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=payload, headers=dict(headers), method="POST")
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.getcode(), response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read(MAX_RESPONSE_BYTES + 1)
        finally:
            exc.close()


class ResendEmailClient:
    """One fixed-endpoint Resend POST; never retries or follows redirects."""

    def __init__(
        self,
        api_key: str,
        sender: str,
        transport: EmailTransport | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip() or _HEADER_CONTROL.search(api_key):
            raise ValueError("api_key must be a non-blank header value")
        if not isinstance(sender, str) or not sender.strip() or _HEADER_CONTROL.search(sender):
            raise ValueError("sender must be a non-blank header value")
        self._api_key = api_key
        self._sender = sender
        self._transport = _urllib_transport if transport is None else transport

    def __repr__(self) -> str:
        return "ResendEmailClient(<redacted>)"

    def send(self, message: EmailMessage, key: str) -> EmailOutcome:
        if not _valid_headers(message):
            return EmailOutcome("rejected", reason=EmailReason.INVALID_MESSAGE)
        if not _valid_key(key):
            return EmailOutcome("rejected", reason=EmailReason.INVALID_KEY)

        try:
            payload = json.dumps(
                {
                    "from": self._sender,
                    "to": [message.to],
                    "subject": message.subject,
                    "text": message.text,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError):
            return EmailOutcome("rejected", reason=EmailReason.INVALID_MESSAGE)

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": key,
        }
        try:
            response = self._transport(RESEND_EMAILS_URL, headers, payload, REQUEST_TIMEOUT_SECONDS)
        except Exception:
            return EmailOutcome("uncertain", reason=EmailReason.TRANSPORT_ERROR)

        if (
            not isinstance(response, tuple)
            or len(response) != 2
            or type(response[0]) is not int
            or not isinstance(response[1], bytes)
        ):
            return EmailOutcome("uncertain", reason=EmailReason.INVALID_RESPONSE)
        status, body = response
        if len(body) > MAX_RESPONSE_BYTES:
            return EmailOutcome("uncertain", reason=EmailReason.RESPONSE_TOO_LARGE)
        if 400 <= status < 500 and status not in {408, 409, 429}:
            return EmailOutcome("rejected", reason=EmailReason.PROVIDER_REJECTED)
        if not 200 <= status < 300:
            return EmailOutcome("uncertain", reason=EmailReason.INVALID_RESPONSE)
        try:
            decoded = json.loads(body.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
        except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
            return EmailOutcome("uncertain", reason=EmailReason.INVALID_RESPONSE)
        if not isinstance(decoded, dict):
            return EmailOutcome("uncertain", reason=EmailReason.INVALID_RESPONSE)
        provider_id = _canonical_uuid(decoded.get("id"))
        if provider_id is None:
            return EmailOutcome("uncertain", reason=EmailReason.INVALID_RESPONSE)
        return EmailOutcome("accepted", provider_id=provider_id)


class FakeEmailClient:
    """Scripted email boundary with immutable-key binding and no implicit success."""

    def __init__(self, responses: list[EmailOutcome] | None = None) -> None:
        self._responses = list(responses or ())
        self._messages: dict[str, EmailMessage] = {}
        self._settled: dict[str, EmailOutcome] = {}
        self.call_count = 0

    def __repr__(self) -> str:
        return "FakeEmailClient(<redacted>)"

    def send(self, message: EmailMessage, key: str) -> EmailOutcome:
        if not _valid_headers(message):
            return EmailOutcome("rejected", reason=EmailReason.INVALID_MESSAGE)
        if not _valid_key(key):
            return EmailOutcome("rejected", reason=EmailReason.INVALID_KEY)
        previous = self._messages.get(key)
        if previous is not None and previous != message:
            return EmailOutcome("rejected", reason=EmailReason.IDEMPOTENCY_CONFLICT)
        self._messages[key] = message
        settled = self._settled.get(key)
        if settled is not None:
            return settled
        self.call_count += 1
        outcome = (
            self._responses.pop(0)
            if self._responses
            else EmailOutcome("uncertain", reason=EmailReason.SCRIPT_EXHAUSTED)
        )
        if outcome.status != "uncertain":
            self._settled[key] = outcome
        return outcome
