"""T20 U1: verification uses raw bytes and the caller's replay clock."""

import hashlib
import hmac
import json
from datetime import timedelta
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.api.app import create_app
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.stripe_payment import verify_webhook
from test_case_service import service
from test_submission_coordinator import NOW

BODY = b'{"id":"evt_contract","type":"payment_intent.succeeded","data":{"object":{}}}'


def signature(body: bytes = BODY, offset: int = 0) -> str:
    timestamp = str(int((NOW + timedelta(seconds=offset)).timestamp()))
    digest = hmac.new(b"secret", timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


@pytest.mark.parametrize("offset", [-301, 301])
def test_stripe_rejects_stale_and_future_signatures(offset: int) -> None:
    with pytest.raises(ValueError, match=r"^invalid_signature$"):
        verify_webhook(BODY, signature(offset=offset), "secret", NOW)


@pytest.mark.parametrize("offset", [-300, 0, 300])
def test_stripe_accepts_exact_tolerance_boundary(offset: int) -> None:
    assert verify_webhook(BODY, signature(offset=offset), "secret", NOW)["id"] == "evt_contract"


def test_stripe_rejects_tampered_raw_bytes() -> None:
    with pytest.raises(ValueError, match=r"^invalid_signature$"):
        verify_webhook(BODY + b" ", signature(), "secret", NOW)


@pytest.mark.parametrize("header", ["", "t=garbage,v1=bad", "v1=bad", "t=1", "t=1,v0=bad"])
def test_stripe_rejects_malformed_headers(header: str) -> None:
    with pytest.raises(ValueError, match=r"^invalid_signature$"):
        verify_webhook(BODY, header, "secret", NOW)


def test_stripe_unconfigured_secret_fails_closed() -> None:
    with pytest.raises(ValueError, match=r"^webhook_not_configured$"):
        verify_webhook(BODY, signature(), "", NOW)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["stripe", "lob"])
async def test_unconfigured_webhook_route_returns_400(tmp_path: Path, provider: str) -> None:
    real = service(tmp_path / "cases.sqlite", FakePaymentClient())
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(real)), base_url="http://test"
        ) as client:
            response = await client.post(f"/webhooks/{provider}", content=b"not even JSON")
        assert response.status_code == 400
        assert response.json() == {"detail": "invalid_signature"}
        assert real._store._require_open().execute(
            "SELECT count(*) FROM pb_webhooks"
        ).fetchone() == (0,)
    finally:
        real._store.close()


@pytest.mark.anyio
@pytest.mark.parametrize("secret", [b"", b"secret"])
async def test_lob_event_signed_while_unconfigured_is_refused_and_never_stored(
    tmp_path: Path, secret: bytes
) -> None:
    """An empty-key HMAC is a forgeable signature: it must never reach the backlog (M1)."""
    real = service(tmp_path / "cases.sqlite", FakePaymentClient(), clock=lambda: NOW)
    object.__setattr__(real._deps, "lob_webhook_secret", "")
    body = json.dumps(
        {
            "id": "evt_empty_secret",
            "event_type": {"id": "letter.in_transit"},
            "body": {"id": "ltr_empty_secret", "metadata": {"submission_token": "tok"}},
            "date_created": NOW.isoformat(),
        }
    ).encode()
    stamp = str(int(NOW.timestamp()))
    headers = {
        "lob-signature-timestamp": stamp,
        "lob-signature": hmac.new(secret, stamp.encode() + b"." + body, hashlib.sha256).hexdigest(),
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(real)), base_url="http://test"
        ) as client:
            response = await client.post("/webhooks/lob", content=body, headers=headers)
        assert response.status_code == 400
        assert response.json() == {"detail": "invalid_signature"}
        assert real._store.pending_webhooks() == ()
        database = real._store._require_open()
        assert database.execute("SELECT count(*) FROM pb_webhooks").fetchone() == (0,)
    finally:
        real._store.close()
