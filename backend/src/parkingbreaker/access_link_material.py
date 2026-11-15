"""Pure deterministic material for restart-safe one-use access-link delivery.

Changing the derivation key makes pending material fail verification; it does not
revoke a link token that was already sent. Link expiry and atomic one-use consumption
must still be enforced by the identity store.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
from urllib.parse import urlsplit

from parkingbreaker.customer_access import token_digest

_DOMAIN = b"parkingbreaker:email-access:v1\0"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def derive_link_token(key: str, nonce: str) -> str:
    """Derive a canonical bearer token from a dedicated key and stored nonce."""
    key_bytes = _decode_credential(key)
    _decode_credential(nonce)
    digest = hmac.new(key_bytes, _DOMAIN + nonce.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_link_token(key: str, nonce: str, expected_digest: str) -> bool:
    """Check derived-token binding; invalid or rotated material never makes a new token."""
    if not isinstance(expected_digest, str) or _DIGEST.fullmatch(expected_digest) is None:
        return False
    try:
        actual_digest = token_digest(derive_link_token(key, nonce))
    except ValueError:
        return False
    return hmac.compare_digest(actual_digest, expected_digest)


def build_access_url(origin: str, raw_token: str) -> str:
    """Build a canonical HTTPS URL with the bearer token only in its fragment."""
    try:
        token_digest(raw_token)
        host = _canonical_host(origin)
    except (TypeError, ValueError):
        raise ValueError("invalid access URL input") from None
    return f"https://{host}/access#token={raw_token}"


def _decode_credential(value: str) -> bytes:
    token_digest(value)
    return base64.urlsafe_b64decode(value + "=")


def _canonical_host(origin: str) -> str:
    if not isinstance(origin, str) or not origin:
        raise ValueError("invalid origin")
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in origin
    ):
        raise ValueError("invalid origin")
    if "\\" in origin or "?" in origin or "#" in origin:
        raise ValueError("invalid origin")

    parsed = urlsplit(origin)
    if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.path not in ("", "/"):
        raise ValueError("invalid origin")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("invalid origin")
    if parsed.query or parsed.fragment:
        raise ValueError("invalid origin")

    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("invalid origin") from None
    if hostname is None or (port is not None and port != 443):
        raise ValueError("invalid origin")
    try:
        hostname.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("invalid origin") from None

    canonical = hostname.lower()
    if len(canonical) > 253 or canonical.endswith("."):
        raise ValueError("invalid origin")
    try:
        ipaddress.ip_address(canonical)
    except ValueError:
        pass
    else:
        raise ValueError("invalid origin")
    labels = canonical.split(".")
    if any(
        not 1 <= len(label) <= 63
        or label[0] not in "abcdefghijklmnopqrstuvwxyz0123456789"
        or label[-1] not in "abcdefghijklmnopqrstuvwxyz0123456789"
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in label)
        for label in labels
    ):
        raise ValueError("invalid origin")
    return canonical
