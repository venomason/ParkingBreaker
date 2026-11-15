from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Mapping
from typing import cast

import pytest

from parkingbreaker.email_client import (
    MAX_RESPONSE_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    RESEND_EMAILS_URL,
    EmailMessage,
    EmailOutcome,
    EmailReason,
    EmailTransport,
    FakeEmailClient,
    ResendEmailClient,
    _NoRedirectHandler,
    _urllib_transport,
)

_TOKEN = "sensitive recipient body and provider key"
_ID = "de305d54-75b4-431b-adb2-eb6b9e546014"


def _accepted() -> tuple[int, bytes]:
    return 200, json.dumps({"id": _ID}).encode()


def test_resend_transport_shape_and_accepted_uuid() -> None:
    calls: list[tuple[str, Mapping[str, str], bytes, float]] = []

    def transport(
        url: str, headers: Mapping[str, str], payload: bytes, timeout: float
    ) -> tuple[int, bytes]:
        calls.append((url, headers, payload, timeout))
        return _accepted()

    client = ResendEmailClient("secret-api-key", "ParkingBreaker <mail@example.test>", transport)
    message = EmailMessage("person@example.test", "Ticket update", "private status")

    result = client.send(message, "stable-key")

    assert result == EmailOutcome("accepted", provider_id=_ID)
    assert len(calls) == 1
    url, headers, payload, timeout = calls[0]
    assert url == RESEND_EMAILS_URL
    assert headers == {
        "Authorization": "Bearer secret-api-key",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Idempotency-Key": "stable-key",
    }
    assert json.loads(payload) == {
        "from": "ParkingBreaker <mail@example.test>",
        "to": ["person@example.test"],
        "subject": "Ticket update",
        "text": "private status",
    }
    assert timeout == REQUEST_TIMEOUT_SECONDS == 20


@pytest.mark.parametrize(
    ("status", "body", "expected", "reason"),
    [
        (400, b'{"message":"private detail"}', "rejected", EmailReason.PROVIDER_REJECTED),
        (408, b"", "uncertain", EmailReason.INVALID_RESPONSE),
        (409, b"", "uncertain", EmailReason.INVALID_RESPONSE),
        (429, b"", "uncertain", EmailReason.INVALID_RESPONSE),
        (500, b"", "uncertain", EmailReason.INVALID_RESPONSE),
        (302, b"", "uncertain", EmailReason.INVALID_RESPONSE),
        (200, b"not-json", "uncertain", EmailReason.INVALID_RESPONSE),
        (200, b'{"id":"not-a-uuid"}', "uncertain", EmailReason.INVALID_RESPONSE),
        (
            200,
            b'{"id":"' + _ID.encode() + b'","id":"' + _ID.encode() + b'"}',
            "uncertain",
            EmailReason.INVALID_RESPONSE,
        ),
        (200, b"x" * (MAX_RESPONSE_BYTES + 1), "uncertain", EmailReason.RESPONSE_TOO_LARGE),
    ],
)
def test_provider_response_classification(
    status: int, body: bytes, expected: str, reason: EmailReason
) -> None:
    client = ResendEmailClient("secret", "sender@example.test", lambda *_: (status, body))

    result = client.send(EmailMessage("to@example.test", "subject", "body"), "key")

    assert result.status == expected
    assert result.reason == reason
    assert result.provider_id is None
    assert "private detail" not in repr(result)


@pytest.mark.parametrize(
    ("message", "key"),
    [
        (EmailMessage("", "subject", "body"), "key"),
        (EmailMessage("a@example.test\r\nBcc:x", "subject", "body"), "key"),
        (EmailMessage("a@example.test", "bad\nsubject", "body"), "key"),
        (EmailMessage("a@example.test", "subject", "bad\x00body"), "key"),
        (EmailMessage("a@example.test", "subject", "body"), "bad\r\nkey"),
        (EmailMessage("a@example.test", "subject", "body"), "k" * 257),
    ],
)
def test_invalid_local_values_never_reach_transport(message: EmailMessage, key: str) -> None:
    calls = 0

    def transport(
        _url: str, _headers: Mapping[str, str], _payload: bytes, _timeout: float
    ) -> tuple[int, bytes]:
        nonlocal calls
        calls += 1
        return _accepted()

    client = ResendEmailClient("secret", "sender@example.test", cast(EmailTransport, transport))

    result = client.send(message, key)

    assert result.status == "rejected"
    assert result.reason in {EmailReason.INVALID_MESSAGE, EmailReason.INVALID_KEY}
    assert calls == 0


def test_transport_exception_is_sanitized_and_same_key_retry_is_caller_owned() -> None:
    calls: list[tuple[str, str]] = []

    def transport(
        _url: str, headers: Mapping[str, str], _payload: bytes, _timeout: float
    ) -> tuple[int, bytes]:
        calls.append((headers["Idempotency-Key"], headers["Authorization"]))
        if len(calls) == 1:
            raise RuntimeError("private@example.test secret-api-key message-body")
        return _accepted()

    client = ResendEmailClient(
        "secret-api-key", "sender@example.test", cast(EmailTransport, transport)
    )
    message = EmailMessage("private@example.test", "private subject", "message-body")
    first = client.send(message, "retry-key")
    second = client.send(message, "retry-key")

    assert first == EmailOutcome("uncertain", reason=EmailReason.TRANSPORT_ERROR)
    assert second == EmailOutcome("accepted", provider_id=_ID)
    assert calls == [("retry-key", "Bearer secret-api-key")] * 2
    for rendered in (repr(first), repr(client), repr(message)):
        assert "private@example.test" not in rendered
        assert "secret-api-key" not in rendered
        assert "message-body" not in rendered


def test_redirect_handler_refuses_redirects() -> None:
    redirect = cast(Callable[..., object], _NoRedirectHandler().redirect_request)
    result = redirect(None, None, 307, "redirect", {}, "https://elsewhere")
    assert result is None


def test_default_transport_reads_only_response_limit_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[int] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def getcode(self) -> int:
            return 200

        def read(self, size: int) -> bytes:
            observed.append(size)
            return b"{}"

    class Opener:
        def open(self, request: urllib.request.Request, timeout: float) -> Response:
            assert request.full_url == RESEND_EMAILS_URL
            assert timeout == REQUEST_TIMEOUT_SECONDS
            return Response()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_args: Opener())

    assert _urllib_transport(RESEND_EMAILS_URL, {}, b"{}", REQUEST_TIMEOUT_SECONDS) == (200, b"{}")
    assert observed == [MAX_RESPONSE_BYTES + 1]


def test_fake_binds_key_to_message_and_does_not_invent_success() -> None:
    fake = FakeEmailClient(
        [
            EmailOutcome("uncertain", reason=EmailReason.TRANSPORT_ERROR),
            EmailOutcome("accepted", provider_id=_ID),
        ]
    )
    message = EmailMessage("person@example.test", "subject", "private body")

    assert fake.send(message, "key").status == "uncertain"
    changed = EmailMessage("other@example.test", "subject", "private body")
    assert fake.send(changed, "key") == EmailOutcome(
        "rejected",
        reason=EmailReason.IDEMPOTENCY_CONFLICT,
    )
    assert fake.send(message, "key") == EmailOutcome("accepted", provider_id=_ID)
    assert fake.send(message, "key") == EmailOutcome("accepted", provider_id=_ID)
    assert fake.call_count == 2

    empty = FakeEmailClient()
    assert empty.send(message, "no-script").status == "uncertain"
    assert "private body" not in repr(empty)


def test_outcome_rejects_unproved_acceptance() -> None:
    with pytest.raises(ValueError, match="provider UUID"):
        EmailOutcome("accepted", provider_id="provider-says-private-error")
