"""Raw signature and caller-clock replay checks; no clocks or provider network in policy."""

import hashlib
import hmac
from datetime import UTC, datetime

import pytest

from parkingbreaker.lob_webhook import verify_webhook

NOW = datetime(2026, 10, 15, 21, tzinfo=UTC)


@pytest.mark.parametrize("delta", [-301, 301])
def test_stale_or_future_lob_signature_refused(delta: int) -> None:
    timestamp = str(int(NOW.timestamp()) + delta)
    payload = b'{"id":"evt_original"}'
    digest = hmac.new(b"secret", timestamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    with pytest.raises(ValueError, match="invalid_signature"):
        verify_webhook(payload, digest, timestamp, "secret", NOW)


def test_lob_signature_uses_exact_raw_bytes() -> None:
    timestamp = str(int(NOW.timestamp()))
    payload = b'{ "id": "evt_original" }'
    digest = hmac.new(b"secret", timestamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    assert verify_webhook(payload, digest, timestamp, "secret", NOW) == {"id": "evt_original"}
    with pytest.raises(ValueError, match="invalid_signature"):
        verify_webhook(b'{"id":"evt_original"}', digest, timestamp, "secret", NOW)
