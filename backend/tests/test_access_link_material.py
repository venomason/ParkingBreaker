from __future__ import annotations

import base64
from urllib.parse import urlsplit

import pytest

from parkingbreaker.access_link_material import (
    build_access_url,
    derive_link_token,
    verify_link_token,
)
from parkingbreaker.customer_access import token_digest

KEY = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE"
NONCE = "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI"
EXPECTED_TOKEN = "X37xJzeNzVWTPJR2MITxtD4tCNFpPxRSptKcfKI9Jw8"
EXPECTED_DIGEST = "fa5178672ed2f319439bfcc077e8ff955cb6ed109992a2cf82bef9aad69415eb"


def credential(value: int) -> str:
    return base64.urlsafe_b64encode(bytes([value]) * 32).decode("ascii").rstrip("=")


def test_derivation_matches_fixed_known_vector_and_is_stable() -> None:
    assert derive_link_token(KEY, NONCE) == EXPECTED_TOKEN
    assert token_digest(EXPECTED_TOKEN) == EXPECTED_DIGEST
    assert derive_link_token(KEY, NONCE) == EXPECTED_TOKEN


def test_key_and_nonce_changes_produce_different_tokens() -> None:
    assert derive_link_token(credential(3), NONCE) != EXPECTED_TOKEN
    assert derive_link_token(KEY, credential(4)) != EXPECTED_TOKEN


def test_verification_binds_expected_digest_and_fails_after_key_rotation() -> None:
    assert verify_link_token(KEY, NONCE, EXPECTED_DIGEST)
    assert not verify_link_token(credential(5), NONCE, EXPECTED_DIGEST)
    assert not verify_link_token(KEY, credential(6), EXPECTED_DIGEST)
    assert not verify_link_token(KEY, NONCE, "f" * 64)


@pytest.mark.parametrize(
    ("key", "nonce"),
    [
        ("short", NONCE),
        (KEY + "=", NONCE),
        ("é" * 43, NONCE),
        (KEY, "short"),
        (KEY, NONCE + "="),
        (KEY, "é" * 43),
    ],
)
def test_invalid_derivation_material_fails_with_sanitized_error(key: str, nonce: str) -> None:
    with pytest.raises(ValueError, match=r"^invalid credential$"):
        derive_link_token(key, nonce)


@pytest.mark.parametrize("digest", ["", "A" * 64, "a" * 63, "a" * 65, "g" * 64, None])
def test_invalid_digest_verification_returns_false(digest: str | None) -> None:
    assert not verify_link_token(KEY, NONCE, digest)  # type: ignore[arg-type]


def test_url_puts_only_canonical_bearer_in_fragment() -> None:
    token = derive_link_token(KEY, NONCE)
    url = build_access_url("HTTPS://Portal.Example.TEST:443/", token)
    parsed = urlsplit(url)

    assert url == f"https://portal.example.test/access#token={token}"
    assert parsed.scheme == "https"
    assert parsed.netloc == "portal.example.test"
    assert parsed.path == "/access"
    assert parsed.query == ""
    assert parsed.fragment == f"token={token}"
    assert NONCE not in url
    assert EXPECTED_DIGEST not in url


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "http://portal.example.test",
        "https://",
        "https://user:pass@portal.example.test",
        "https://portal.example.test?x=1",
        "https://portal.example.test?",
        "https://portal.example.test#frag",
        "https://portal.example.test#",
        "https://portal.example.test/path",
        "https://portal.example.test//",
        "https://portal.example.test:444",
        "https://portal.example.test\\evil.test",
        "https://portal.example.test\n.evil.test",
        "https://portal.example.test .evil.test",
        "https://portál.example.test",
        "https://bad_label.example.test",
        "https://-bad.example.test",
        "https://bad-.example.test",
        "https://127.0.0.1",
    ],
)
def test_invalid_origins_fail_with_sanitized_error(origin: str) -> None:
    with pytest.raises(ValueError, match=r"^invalid access URL input$"):
        build_access_url(origin, EXPECTED_TOKEN)


@pytest.mark.parametrize("token", ["short", EXPECTED_TOKEN + "=", "é" * 43])
def test_invalid_url_token_fails_without_reflecting_input(token: str) -> None:
    with pytest.raises(ValueError, match=r"^invalid access URL input$") as caught:
        build_access_url("https://portal.example.test", token)
    assert token not in str(caught.value)
