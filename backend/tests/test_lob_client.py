from __future__ import annotations

import json
import logging
import threading
import traceback
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from lob_python.api_client import ApiClient  # type: ignore[import-untyped]
from lob_python.configuration import Configuration  # type: ignore[import-untyped]

from mail_samples import make_request
from parkingbreaker.agencies import LobVerificationStatus
from parkingbreaker.lob_client import LobSdkClient, MetadataLookupFailure
from parkingbreaker.lob_submission import (
    LetterEvidence,
    LetterOptions,
    LetterRequest,
    LobResponse,
    MetadataLookup,
)
from parkingbreaker.mail_config import MailClass

_API_VERSION = "2024-01-01"
_TOKEN = "opaque-submission-token"
_LETTER_ID = "ltr_Abc123"


def _address(name: str) -> dict[str, object]:
    return {
        "id": "adr_Abc123",
        "name": name,
        "company": None,
        "address_line1": "1 Main Street",
        "address_line2": None,
        "address_city": "San Francisco",
        "address_state": "CA",
        "address_zip": "94103",
        "address_country": "UNITED STATES",
        "object": "address",
    }


def _letter(letter_id: str = _LETTER_ID, token: str = _TOKEN) -> dict[str, object]:
    return {
        "id": letter_id,
        "object": "letter",
        "to": _address("recipient"),
        "from": _address("sender"),
        "date_created": "2026-10-12T12:00:00Z",
        "date_modified": "2026-10-12T12:00:00Z",
        "return_envelope": False,
        "use_type": "operational",
        "status": "rendered",
        "deleted": False,
        "failure_reason": None,
        "metadata": {"submission_token": token},
    }


class _WireServer:
    def __init__(
        self,
        responder: Callable[[str, str, dict[str, str], bytes], tuple[int, object]],
    ) -> None:
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(inner_self: BaseHTTPRequestHandler) -> None:
                self._handle(inner_self, responder)

            def do_GET(inner_self: BaseHTTPRequestHandler) -> None:
                self._handle(inner_self, responder)

            def log_message(
                inner_self: BaseHTTPRequestHandler, _format: str, *args: object
            ) -> None:
                del inner_self, args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _handle(
        self,
        handler: BaseHTTPRequestHandler,
        responder: Callable[[str, str, dict[str, str], bytes], tuple[int, object]],
    ) -> None:
        body = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
        headers = {key.lower(): value for key, value in handler.headers.items()}
        request = (handler.command, handler.path, headers, body)
        self.requests.append(request)
        status, payload = responder(*request)
        encoded = json.dumps(payload).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(encoded)))
        handler.end_headers()
        handler.wfile.write(encoded)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        host_text = host.decode() if isinstance(host, bytes) else host
        return f"http://{host_text}:{port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _client(server: _WireServer) -> LobSdkClient:
    configuration = Configuration(
        host=server.base_url,
        username="test-key-never-sent-to-network",
        password="",
    )
    configuration.retries = 0
    return LobSdkClient(
        "test-key-never-sent-to-network",
        _API_VERSION,
        2.5,
        api_client=ApiClient(configuration),
    )


def _parse_multipart(content_type: str, body: bytes) -> dict[str, bytes]:
    from email.parser import BytesParser
    from email.policy import default

    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    )
    result: dict[str, bytes] = {}
    for part in message.iter_parts():
        field = part.get_param("name", header="content-disposition")
        assert isinstance(field, str)
        payload = part.get_payload(decode=True)
        assert isinstance(payload, bytes)
        result[field] = payload
    return result


def _test_api_client(server: _WireServer) -> ApiClient:
    configuration = Configuration(host=server.base_url, username="test-key")
    configuration.retries = 0
    return ApiClient(configuration)


def test_create_uses_real_sdk_multipart_and_preserves_frozen_fields() -> None:
    def respond(
        method: str, path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        assert method == "POST"
        assert path == "/v1/letters"
        return 200, _letter()

    server = _WireServer(respond)
    try:
        client = _client(server)
        original = make_request(b"%PDF-bytes-are-frozen\x00\xff", _TOKEN)
        request = LetterRequest(
            original.pdf,
            original.submission_token,
            original.sender,
            original.recipient,
            LetterOptions(MailClass.CERTIFIED, color=True, double_sided=True, return_receipt=True),
        )
        response = client.create_letter(request, "same-key-on-every-retry")
    finally:
        server.close()

    assert response == LobResponse(200, _LETTER_ID, evidence=LetterEvidence.RENDERED)
    method, path, headers, body = server.requests[0]
    assert (method, path) == ("POST", "/v1/letters")
    assert headers["idempotency-key"] == "same-key-on-every-retry"
    assert headers["lob-version"] == _API_VERSION
    assert headers["authorization"].startswith("Basic ")
    parts = _parse_multipart(headers["content-type"], body)
    assert parts["file"] == request.pdf
    assert b"frozen-letter.pdf" not in body
    assert json.loads(parts["metadata"]) == {"submission_token": _TOKEN}
    to = json.loads(parts["to"])
    sender = json.loads(parts["from"])
    assert to == {
        "name": request.recipient.recipient,
        "company": request.recipient.attention,
        "address_line1": request.recipient.line1,
        "address_line2": request.recipient.line2,
        "address_city": request.recipient.city,
        "address_state": request.recipient.state,
        "address_zip": request.recipient.zip_code,
        "address_country": "US",
    }
    assert sender["name"] == request.sender.recipient
    assert sender["company"] == request.sender.attention
    assert sender["address_line1"] == request.sender.line1
    assert sender["address_line2"] == request.sender.line2
    assert parts["color"] == str(request.options.color).encode()
    assert parts["double_sided"] == str(request.options.double_sided).encode()
    assert parts["address_placement"] == b"insert_blank_page"
    assert parts["mail_type"] == b"usps_first_class"
    assert parts["extra_service"] == b"certified_return_receipt"
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    ("mail_class", "return_receipt", "extra_service"),
    [
        ("first_class", False, None),
        ("certified", False, "certified"),
        ("certified", True, "certified_return_receipt"),
    ],
)
def test_mail_class_mapping_has_no_fallback(
    mail_class: str, return_receipt: bool, extra_service: str | None
) -> None:
    def respond(
        _method: str, _path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        return 200, _letter()

    server = _WireServer(respond)
    try:
        original = make_request(b"%PDF-data", _TOKEN)
        request = LetterRequest(
            original.pdf,
            original.submission_token,
            original.sender,
            original.recipient,
            LetterOptions(MailClass(mail_class), return_receipt=return_receipt),
        )
        LobSdkClient(
            "test-key",
            _API_VERSION,
            1,
            api_client=_test_api_client(server),
        ).create_letter(request, "stable-key")
    finally:
        server.close()
    parts = _parse_multipart(server.requests[0][2]["content-type"], server.requests[0][3])
    assert parts["mail_type"] == b"usps_first_class"
    if extra_service is None:
        assert "extra_service" not in parts
    else:
        assert parts["extra_service"] == extra_service.encode()


def test_repeated_calls_send_identical_payload_and_same_key_without_internal_retry() -> None:
    def respond(
        _method: str, _path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        return 200, _letter()

    server = _WireServer(respond)
    try:
        client = _client(server)
        request = make_request(b"%PDF-frozen-payload", _TOKEN)
        first = client.create_letter(request, "immutable-idempotency-key")
        second = client.create_letter(request, "immutable-idempotency-key")
    finally:
        server.close()
    assert first == second == LobResponse(200, _LETTER_ID, evidence=LetterEvidence.RENDERED)
    assert len(server.requests) == 2
    payloads = [
        _parse_multipart(headers["content-type"], body) for _, _, headers, body in server.requests
    ]
    assert payloads[0] == payloads[1]
    assert all(
        headers["idempotency-key"] == "immutable-idempotency-key"
        for _, _, headers, _ in server.requests
    )


@pytest.mark.parametrize("status", [401, 403, 404, 408, 409, 425, 429, 500, 503])
def test_ambiguous_provider_errors_stay_uncertain(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    def respond(
        _method: str, _path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        return status, {
            "error": {"status_code": status, "code": "unauthorized", "message": "PII secret"}
        }

    server = _WireServer(respond)
    try:
        with caplog.at_level(logging.DEBUG, logger="lob_python.rest"):
            result = _client(server).create_letter(make_request(b"%PDF-data", _TOKEN), "key")
    finally:
        server.close()
    assert result.status is None
    assert result.error == "provider response uncertain"
    assert "PII secret" not in caplog.text
    assert "test-key-never-sent-to-network" not in caplog.text


def test_definitive_validation_failure_is_sanitized() -> None:
    def respond(
        _method: str, _path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        return 422, {
            "error": {"status_code": 422, "code": "invalid_file", "message": "private text"}
        }

    server = _WireServer(respond)
    try:
        result = _client(server).create_letter(make_request(b"%PDF-data", _TOKEN), "key")
    finally:
        server.close()
    assert result == LobResponse(422, error="provider validation rejected")
    assert "private text" not in result.error


@pytest.mark.parametrize(
    ("matches", "expected"),
    [
        (
            [_letter()],
            MetadataLookup(_LETTER_ID, authoritative=False, evidence=LetterEvidence.RENDERED),
        ),
        ([], MetadataLookup(None, authoritative=False)),
        ([_letter(_LETTER_ID, "other-token")], MetadataLookup(None, authoritative=False)),
    ],
)
def test_metadata_result_exact_match_and_empty_is_not_authoritative(
    matches: list[dict[str, object]], expected: MetadataLookup
) -> None:
    def respond(
        method: str, path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        if method == "GET" and path == f"/v1/letters/{_LETTER_ID}":
            assert path == f"/v1/letters/{_LETTER_ID}"
            return 200, _letter()
        return 200, {"object": "list", "data": matches, "count": len(matches), "next_url": None}

    server = _WireServer(respond)
    try:
        result = _client(server).find_by_metadata(_TOKEN)
    finally:
        server.close()
    assert result == expected
    request = server.requests[0]
    query = parse_qs(urlsplit(request[1]).query)
    assert query["metadata[submission_token]"] == [_TOKEN]
    assert "metadata" not in query
    assert request[2]["lob-version"] == _API_VERSION
    assert len([r for r in server.requests if r[0] == "GET" and "?" not in r[1]]) == (
        1 if expected.letter_id is not None else 0
    )


def test_metadata_paginates_with_validated_cursor_and_deduplicates() -> None:
    servers: list[_WireServer] = []

    def respond(
        method: str, path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        if method == "GET" and path == f"/v1/letters/{_LETTER_ID}":
            assert path == f"/v1/letters/{_LETTER_ID}"
            return 200, _letter()
        query = parse_qs(urlsplit(path).query)
        if "after" not in query:
            return 200, {
                "object": "list",
                "data": [_letter()],
                "count": 1,
                "next_url": (
                    f"{servers[0].base_url}/letters?after=opaque%3Acursor%2Fpage%2B2"
                    "&limit=1&metadata=%7B%22submission_token%22%3A%22wrong%22%7D"
                ),
            }
        return 200, {"object": "list", "data": [_letter()], "count": 1, "next_url": None}

    server = _WireServer(respond)
    servers.append(server)
    try:
        result = _client(server).find_by_metadata(_TOKEN)
    finally:
        server.close()
    assert result == MetadataLookup(
        _LETTER_ID, authoritative=False, evidence=LetterEvidence.RENDERED
    )
    second_query = parse_qs(urlsplit(server.requests[1][1]).query)
    assert second_query["after"] == ["opaque:cursor/page+2"]
    assert second_query["limit"] == ["100"]
    assert second_query["metadata[submission_token]"] == [_TOKEN]
    assert "metadata" not in second_query
    assert len([r for r in server.requests if r[0] == "GET" and "?" not in r[1]]) == 1


@pytest.mark.parametrize(
    "next_url_kind",
    [
        "wrong_host",
        "userinfo",
        "fragment",
        "wrong_path",
        "duplicate_after",
        "missing_after",
        "invalid_host",
    ],
)
def test_metadata_rejects_untrusted_or_incomplete_next_url(next_url_kind: str) -> None:
    servers: list[_WireServer] = []

    def respond(
        _method: str, _path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        base = servers[0].base_url
        bad_urls = {
            "wrong_host": base.replace("127.0.0.1", "example.com") + "/letters?after=cursor",
            "userinfo": base.replace("http://", "http://user@") + "/letters?after=cursor",
            "fragment": base + "/letters?after=cursor#fragment",
            "wrong_path": base + "/other?after=cursor",
            "duplicate_after": base + "/letters?after=one&after=two",
            "missing_after": base + "/letters?before=cursor",
            "invalid_host": "http://[broken/v1/letters?after=cursor",
        }
        return 200, {
            "object": "list",
            "data": [_letter()],
            "count": 1,
            "next_url": bad_urls[next_url_kind],
        }

    server = _WireServer(respond)
    servers.append(server)
    try:
        with pytest.raises(MetadataLookupFailure):
            _client(server).find_by_metadata(_TOKEN)
    finally:
        server.close()


def test_metadata_rejects_repeated_cursor_with_no_page_progress() -> None:
    servers: list[_WireServer] = []

    def respond(
        _method: str, path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        query = parse_qs(urlsplit(path).query)
        after = "opaque:first" if "after" not in query else "opaque:second"
        return 200, {
            "object": "list",
            "data": [_letter()],
            "count": 1,
            "next_url": f"{servers[0].base_url}/letters?after={after.replace(':', '%3A')}",
        }

    server = _WireServer(respond)
    servers.append(server)
    try:
        with pytest.raises(MetadataLookupFailure):
            _client(server).find_by_metadata(_TOKEN)
    finally:
        server.close()


def test_metadata_ambiguous_ids_and_failed_page_raise_typed_failure() -> None:
    def ambiguous(
        _method: str, _path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        return 200, {
            "object": "list",
            "data": [_letter(), _letter("ltr_Second")],
            "count": 2,
            "next_url": None,
        }

    server = _WireServer(ambiguous)
    try:
        with pytest.raises(MetadataLookupFailure):
            _client(server).find_by_metadata(_TOKEN)
    finally:
        server.close()

    def failed_page(
        _method: str, _path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        return 500, {"error": {"status_code": 500, "code": "internal_server_error"}}

    server = _WireServer(failed_page)
    try:
        with pytest.raises(MetadataLookupFailure):
            _client(server).find_by_metadata(_TOKEN)
    finally:
        server.close()


@pytest.mark.parametrize(
    "payload",
    [
        {**_letter(), "status": "failed"},
        {**_letter(), "status": "render_error"},
        {**_letter(), "status": "queued"},
        {**_letter(), "deleted": True},
        {**_letter(), "deleted": "false"},
        {**_letter(), "failure_reason": "private provider detail"},
        {**_letter(), "failure_reason": ""},
        {**_letter(), "failure_reason": {}},
        {**_letter(token="wrong-token")},
    ],
    ids=[
        "failed",
        "render_error",
        "unrecognized",
        "deleted",
        "malformed-deleted",
        "failure-reason",
        "empty-failure-reason",
        "malformed-failure-reason",
        "wrong-token",
    ],
)
def test_create_requires_explicit_rendered_same_object(payload: dict[str, object]) -> None:
    def respond(
        _method: str, _path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        return 200, payload

    server = _WireServer(respond)
    try:
        response = _client(server).create_letter(make_request(b"%PDF-1", _TOKEN), "key")
    finally:
        server.close()
    # Terminal/mismatched create responses do not trigger another lookup.
    assert response.status is None
    assert len(server.requests) == 1


@pytest.mark.parametrize("initial_status", ["processed", None])
def test_create_fetches_same_id_once_until_rendered(initial_status: str | None) -> None:
    def respond(
        method: str, path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        if method == "POST":
            payload = _letter()
            if initial_status is None:
                payload.pop("status")
            else:
                payload["status"] = initial_status
            return 200, payload
        assert (method, path) == ("GET", f"/v1/letters/{_LETTER_ID}")
        return 200, _letter()

    server = _WireServer(respond)
    try:
        response = _client(server).create_letter(make_request(b"%PDF-1", _TOKEN), "key")
    finally:
        server.close()
    assert response == LobResponse(200, _LETTER_ID, evidence=LetterEvidence.RENDERED)
    assert [(method, path) for method, path, _, _ in server.requests] == [
        ("POST", "/v1/letters"),
        ("GET", f"/v1/letters/{_LETTER_ID}"),
    ]


def test_create_accepts_rendered_letter_when_deleted_field_is_absent() -> None:
    def respond(
        _method: str, _path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        payload = _letter()
        payload.pop("deleted")
        return 200, payload

    server = _WireServer(respond)
    try:
        response = _client(server).create_letter(make_request(b"%PDF-1", _TOKEN), "key")
    finally:
        server.close()
    assert response == LobResponse(200, _LETTER_ID, evidence=LetterEvidence.RENDERED)
    assert len(server.requests) == 1


def test_create_get_failure_is_uncertain_and_does_not_leak_provider_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def respond(
        method: str, _path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        if method == "POST":
            return 200, {**_letter(), "status": "processed"}
        return 500, {"error": {"message": "private customer address"}}

    server = _WireServer(respond)
    try:
        with caplog.at_level(logging.DEBUG, logger="lob_python.rest"):
            response = _client(server).create_letter(make_request(b"%PDF-1", _TOKEN), "key")
    finally:
        server.close()
    assert response == LobResponse(200, _LETTER_ID, evidence=LetterEvidence.CREATED)
    assert [(method, path) for method, path, _, _ in server.requests] == [
        ("POST", "/v1/letters"),
        ("GET", f"/v1/letters/{_LETTER_ID}"),
    ]
    assert "private customer address" not in caplog.text


@pytest.mark.parametrize("mismatch", ["id", "token", "deleted", "failure_reason"])
def test_create_followup_get_must_match_live_rendered_identity(mismatch: str) -> None:
    def respond(
        method: str, path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        if method == "POST":
            return 200, {**_letter(), "status": "processed"}
        assert path == f"/v1/letters/{_LETTER_ID}"
        payload = _letter()
        if mismatch == "id":
            payload["id"] = "ltr_Other"
        elif mismatch == "token":
            payload["metadata"] = {"submission_token": "another-token"}
        elif mismatch == "deleted":
            payload["deleted"] = True
        else:
            payload["failure_reason"] = "private render failure"
        return 200, payload

    server = _WireServer(respond)
    try:
        response = _client(server).create_letter(make_request(b"%PDF-1", _TOKEN), "key")
    finally:
        server.close()
    assert response == LobResponse(200, _LETTER_ID, evidence=LetterEvidence.CREATED)
    assert [(method, path) for method, path, _, _ in server.requests] == [
        ("POST", "/v1/letters"),
        ("GET", f"/v1/letters/{_LETTER_ID}"),
    ]


@pytest.mark.parametrize("contradiction", ["token", "deleted", "failure_reason"])
def test_create_does_not_let_get_override_contradictory_initial_object(
    contradiction: str,
) -> None:
    def respond(
        method: str, path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        if method == "POST":
            initial = {**_letter(), "status": "processed"}
            if contradiction == "token":
                initial["metadata"] = {"submission_token": "another-token"}
            elif contradiction == "deleted":
                initial["deleted"] = True
            else:
                initial["failure_reason"] = "render failed"
            return 200, initial
        assert path == f"/v1/letters/{_LETTER_ID}"
        return 200, _letter()

    server = _WireServer(respond)
    try:
        response = _client(server).create_letter(make_request(b"%PDF-1", _TOKEN), "key")
    finally:
        server.close()
    assert response == LobResponse(None, error="provider response uncertain")
    assert len(server.requests) == 1


def test_metadata_match_requires_current_rendered_same_object() -> None:
    def respond(
        method: str, path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        if method == "GET" and path == f"/v1/letters/{_LETTER_ID}":
            assert path == f"/v1/letters/{_LETTER_ID}"
            return 200, {**_letter(), "status": "failed"}
        return 200, {
            "object": "list",
            "data": [_letter()],
            "count": 1,
            "next_url": None,
        }

    server = _WireServer(respond)
    try:
        with pytest.raises(MetadataLookupFailure):
            _client(server).find_by_metadata(_TOKEN)
    finally:
        server.close()
    assert [method for method, _, _, _ in server.requests] == ["GET", "GET"]
    assert server.requests[0][1].startswith("/v1/letters?")
    assert server.requests[1][1] == f"/v1/letters/{_LETTER_ID}"


@pytest.mark.parametrize(
    "contradiction",
    ["failed", "deleted", "wrongtyped_deleted", "failure_reason", "unknown_status"],
)
def test_metadata_list_contradiction_fails_without_followup_get(
    contradiction: str,
) -> None:
    def respond(
        method: str, path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        if method == "GET" and path == f"/v1/letters/{_LETTER_ID}":
            # A fresh rendered response must not override a contradictory list row.
            return 200, _letter()
        row = _letter()
        if contradiction == "failed":
            row["status"] = "failed"
        elif contradiction == "deleted":
            row["deleted"] = True
        elif contradiction == "wrongtyped_deleted":
            row["deleted"] = "false"
        elif contradiction == "failure_reason":
            row["failure_reason"] = "render failed"
        else:
            row["status"] = "render_error"
        return 200, {
            "object": "list",
            "data": [row],
            "count": 1,
            "next_url": None,
        }

    server = _WireServer(respond)
    try:
        with pytest.raises(MetadataLookupFailure):
            _client(server).find_by_metadata(_TOKEN)
    finally:
        server.close()
    assert len(server.requests) == 1
    assert "?" in server.requests[0][1]


@pytest.mark.parametrize("list_status", ["processed", None])
def test_metadata_processed_or_missing_status_can_resolve_by_one_get(
    list_status: str | None,
) -> None:
    def respond(
        method: str, path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        if method == "GET" and path == f"/v1/letters/{_LETTER_ID}":
            return 200, _letter()
        row = _letter()
        if list_status is None:
            row.pop("status")
        else:
            row["status"] = list_status
        return 200, {"object": "list", "data": [row], "count": 1, "next_url": None}

    server = _WireServer(respond)
    try:
        result = _client(server).find_by_metadata(_TOKEN)
    finally:
        server.close()
    assert result == MetadataLookup(
        _LETTER_ID, authoritative=False, evidence=LetterEvidence.RENDERED
    )
    assert len([r for r in server.requests if "?" not in r[1]]) == 1


@pytest.mark.parametrize("mismatch", ["id", "token", "deleted", "failure_reason"])
def test_metadata_followup_get_requires_same_rendered_identity(mismatch: str) -> None:
    def respond(
        method: str, path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        if method == "GET" and path == f"/v1/letters/{_LETTER_ID}":
            payload = _letter()
            if mismatch == "id":
                payload["id"] = "ltr_Other"
            elif mismatch == "token":
                payload["metadata"] = {"submission_token": "another-token"}
            elif mismatch == "deleted":
                payload["deleted"] = True
            else:
                payload["failure_reason"] = "private render failure"
            return 200, payload
        return 200, {
            "object": "list",
            "data": [_letter()],
            "count": 1,
            "next_url": None,
        }

    server = _WireServer(respond)
    try:
        with pytest.raises(MetadataLookupFailure):
            _client(server).find_by_metadata(_TOKEN)
    finally:
        server.close()
    assert len([r for r in server.requests if r[0] == "GET" and "?" not in r[1]]) == 1


@pytest.mark.parametrize(
    ("deliverability", "expected"),
    [
        ("deliverable", LobVerificationStatus.DELIVERABLE),
        ("deliverable_unnecessary_unit", LobVerificationStatus.DELIVERABLE_WITH_CORRECTIONS),
        ("deliverable_missing_unit", LobVerificationStatus.UNVERIFIED),
        ("unknown", LobVerificationStatus.UNVERIFIED),
    ],
)
def test_verify_address_maps_only_supported_results(
    deliverability: str, expected: LobVerificationStatus
) -> None:
    def respond(
        method: str, path: str, _headers: dict[str, str], body: bytes
    ) -> tuple[int, object]:
        assert method == "POST"
        assert path == "/v1/us_verifications"
        payload = json.loads(body)
        assert payload["primary_line"] == "1 Main Street"
        assert payload["recipient"] == "recipient"
        assert "address_line1" not in payload
        return 200, {
            "id": "us_ver_Abc123",
            "object": "us_verification",
            "deliverability": deliverability,
            "valid_address": True,
        }

    server = _WireServer(respond)
    try:
        from parkingbreaker.agencies import MailingAddress

        address = MailingAddress(
            "recipient", "attention", "1 Main Street", "Suite 1", "City", "CA", "94103"
        )
        result = _client(server).verify_address(address)
    finally:
        server.close()
    assert result.status is expected
    assert result.checked_on is None


def test_verify_address_provider_error_is_unverified_without_details() -> None:
    def respond(
        _method: str, _path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        return 500, {"error": {"message": "customer private address"}}

    server = _WireServer(respond)
    try:
        from parkingbreaker.agencies import MailingAddress

        address = MailingAddress(
            "recipient", "attention", "1 Main Street", "Suite 1", "City", "CA", "94103"
        )
        result = _client(server).verify_address(address)
    finally:
        server.close()
    assert result.status is LobVerificationStatus.UNVERIFIED
    assert result.note == "Lob verification unavailable"
    assert "private" not in result.note


def test_metadata_failure_traceback_excludes_provider_details() -> None:
    private_detail = "private customer address and provider response"

    def respond(
        _method: str, _path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        return 500, {"error": {"message": private_detail}}

    server = _WireServer(respond)
    try:
        with pytest.raises(MetadataLookupFailure) as caught:
            _client(server).find_by_metadata(_TOKEN)
        formatted = "".join(traceback.format_exception(caught.value))
        assert private_detail not in formatted
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__
    finally:
        server.close()


@pytest.mark.parametrize(
    ("tracking", "expected"),
    [
        ("9407111899223197428490", "9407111899223197428490"),
        ("not a tracking\n", None),
        (None, None),
    ],
)
def test_create_returns_only_a_well_formed_tracking_number(
    tracking: str | None, expected: str | None
) -> None:
    def respond(
        _method: str, _path: str, _headers: dict[str, str], _body: bytes
    ) -> tuple[int, object]:
        return 200, {**_letter(), "tracking_number": tracking}

    server = _WireServer(respond)
    try:
        response = _client(server).create_letter(make_request(b"%PDF-1", _TOKEN), "key")
    finally:
        server.close()

    assert response == LobResponse(
        200, _LETTER_ID, tracking_number=expected, evidence=LetterEvidence.RENDERED
    )
