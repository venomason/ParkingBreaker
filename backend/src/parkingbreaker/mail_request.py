"""Pure codec and validation for frozen mail requests (v6 section 6.3)."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict
from typing import cast

from parkingbreaker.agencies import MailingAddress
from parkingbreaker.lob_submission import LetterOptions, LetterRequest
from parkingbreaker.mail_config import MailClass

_STATE = re.compile(r"[A-Z]{2}", flags=re.ASCII)
_ZIP_CODE = re.compile(r"[0-9]{5}(?:-[0-9]{4})?", flags=re.ASCII)
_ENVELOPE_KEYS = {"sender", "recipient", "options"}
_ADDRESS_KEYS = {
    "recipient",
    "attention",
    "line1",
    "line2",
    "city",
    "state",
    "zip_code",
    "country",
}
_OPTION_KEYS = {"mail_class", "color", "double_sided", "return_receipt"}


def _has_control(value: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in value)


def _required_text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-blank string")
    if _has_control(value):
        raise ValueError(f"{field} must not contain control characters")


def _validate_address(address: object, field: str) -> None:
    if not isinstance(address, MailingAddress):
        raise ValueError(f"{field} must be a MailingAddress")

    for name in ("recipient", "line1", "city", "state", "zip_code"):
        value = getattr(address, name)
        _required_text(value, f"{field}.{name}")
        if any(char in value for char in "\r\n\u2028\u2029"):
            raise ValueError(f"{field}.{name} must not contain newlines")
    for name in ("attention", "line2"):
        value = getattr(address, name)
        if value is not None:
            _required_text(value, f"{field}.{name}")
            if any(char in value for char in "\r\n\u2028\u2029"):
                raise ValueError(f"{field}.{name} must not contain newlines")

    if address.country != "US":
        raise ValueError(f"{field}.country must be 'US'")
    if _STATE.fullmatch(address.state) is None:
        raise ValueError(f"{field}.state must be two uppercase ASCII letters")
    if _ZIP_CODE.fullmatch(address.zip_code) is None:
        raise ValueError(f"{field}.zip_code must be a five-digit or ZIP+4 code")


def validate_request(request: LetterRequest) -> None:
    """Validate all material fields without normalizing or mutating the request."""
    if not isinstance(request, LetterRequest):
        raise ValueError("request must be a LetterRequest")
    if not isinstance(request.pdf, bytes) or not request.pdf:
        raise ValueError("pdf must be non-empty bytes")
    token = request.submission_token
    if not isinstance(token, str) or not token.strip():
        raise ValueError("submission_token must be a non-blank string")
    if len(token) > 500:
        raise ValueError("submission_token must be at most 500 characters")
    if any(char in token for char in "'\"\\") or _has_control(token):
        raise ValueError("submission_token must not contain quotes, backslashes or controls")

    _validate_address(request.sender, "sender")
    _validate_address(request.recipient, "recipient")

    options = request.options
    if not isinstance(options, LetterOptions):
        raise ValueError("options must be LetterOptions")
    if type(options.mail_class) is not MailClass:
        raise ValueError("options.mail_class must be a MailClass")
    for name in ("color", "double_sided", "return_receipt"):
        if type(getattr(options, name)) is not bool:
            raise ValueError(f"options.{name} must be a bool")
    if options.return_receipt and options.mail_class is not MailClass.CERTIFIED:
        raise ValueError("return_receipt requires certified mail")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def envelope_json(request: LetterRequest) -> str:
    """Return the canonical envelope and options JSON for this validated request."""
    validate_request(request)
    envelope = {
        "sender": asdict(request.sender),
        "recipient": asdict(request.recipient),
        "options": {
            "mail_class": request.options.mail_class.value,
            "color": request.options.color,
            "double_sided": request.options.double_sided,
            "return_receipt": request.options.return_receipt,
        },
    }
    return _canonical_json(envelope)


def envelope_sha256(request: LetterRequest) -> str:
    """Bind addresses and mail options independently of the submission identity."""
    return hashlib.sha256(envelope_json(request).encode("utf-8")).hexdigest()


def request_fingerprint(request: LetterRequest) -> str:
    """Hash the PDF and every frozen request field using canonical JSON (v6 section 6.3)."""
    validate_request(request)
    payload = {
        "pdf_sha256": hashlib.sha256(request.pdf).hexdigest(),
        "submission_token": request.submission_token,
        "envelope": json.loads(envelope_json(request)),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _object_with_keys(value: object, keys: set[str], field: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{field} must contain exactly these keys: {', '.join(sorted(keys))}")
    return cast(dict[str, object], value)


def _decode_address(value: object, field: str) -> MailingAddress:
    parts = _object_with_keys(value, _ADDRESS_KEYS, field)
    try:
        return MailingAddress(**parts)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {exc}") from exc


def decode_request(pdf: bytes, submission_token: str, envelope_text: str) -> LetterRequest:
    """Decode and validate persisted envelope JSON into an immutable request."""
    if not isinstance(envelope_text, str):
        raise ValueError("envelope_text must be a string")
    try:
        raw = json.loads(
            envelope_text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid envelope JSON: {exc}") from exc

    envelope = _object_with_keys(raw, _ENVELOPE_KEYS, "envelope")
    option_values = _object_with_keys(envelope["options"], _OPTION_KEYS, "options")
    mail_class_value = option_values["mail_class"]
    if not isinstance(mail_class_value, str):
        raise ValueError("options.mail_class must be a string")
    try:
        mail_class = MailClass(mail_class_value)
    except ValueError as exc:
        raise ValueError("options.mail_class is unknown") from exc
    for name in ("color", "double_sided", "return_receipt"):
        if type(option_values[name]) is not bool:
            raise ValueError(f"options.{name} must be a bool")

    request = LetterRequest(
        pdf=pdf,
        submission_token=submission_token,
        sender=_decode_address(envelope["sender"], "sender"),
        recipient=_decode_address(envelope["recipient"], "recipient"),
        options=LetterOptions(
            mail_class=mail_class,
            color=cast(bool, option_values["color"]),
            double_sided=cast(bool, option_values["double_sided"]),
            return_receipt=cast(bool, option_values["return_receipt"]),
        ),
    )
    validate_request(request)
    return request
