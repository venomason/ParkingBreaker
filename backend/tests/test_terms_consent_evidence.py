"""The paid card hold records the accepted terms as durable dispute evidence."""

import copy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient

import parkingbreaker.case_service as case_service_module
from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.case_service import TERMS_VERSION
from parkingbreaker.payment_fakes import FakePaymentClient
from test_case_service import INTAKE, NOW, service, submit
from test_case_service import fake_resolution as fake_resolution
from test_stripe_payment import REF


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_paid_authorization_embeds_terms_evidence(
    tmp_path: Path, fake_resolution: list[str]
) -> None:
    payments = FakePaymentClient(REF)
    real = service(tmp_path / "consent.sqlite", payments)
    async with AsyncClient(
        transport=ASGITransport(app=create_app(real)), base_url="http://offline.test"
    ) as api:
        _, receipt = await submit(api, copy.deepcopy(INTAKE))
    case_id = receipt["case_id"]
    real.choose(case_id, s.ChooseTier(tier=s.MailTier.CERTIFIED))
    body = s.StartAuthorization(terms_accepted=True, payment_method_id="pm_card_visa")

    authorization = real.authorize(case_id, body)
    assert authorization.status == "authorized"

    # Exactly one hold was created and its metadata carries the consent evidence.
    assert [call[0] for call in payments.calls] == ["authorize"]
    assert len(payments.authorizations) == 1
    _amount, _pm, metadata, _outcome = next(iter(payments.authorizations.values()))
    assert metadata["terms_accepted"] == "true"
    assert metadata["terms_version"] == TERMS_VERSION
    accepted_at = datetime.fromisoformat(metadata["terms_accepted_at"])
    assert accepted_at.tzinfo is not None
    assert accepted_at == NOW
    # The pre-existing hold identity metadata is preserved unchanged.
    assert metadata["case"] == case_id
    assert metadata["tier"] == "CERTIFIED"
    assert metadata["service_amount_cents"] == str(authorization.amount_cents)

    # Replaying the same authorization returns the same hold and never creates another.
    assert real.authorize(case_id, body) == authorization
    assert [call[0] for call in payments.calls] == ["authorize"]


@pytest.mark.anyio
@pytest.mark.parametrize("retry_via", ["recover", "authorize"])
async def test_interrupted_authorization_retry_across_terms_version_change(
    tmp_path: Path,
    fake_resolution: list[str],
    monkeypatch: pytest.MonkeyPatch,
    retry_via: str,
) -> None:
    """An interrupted hold keeps the original accepted terms version across a bump."""
    payments = FakePaymentClient(REF)
    original_authorize = payments.authorize
    dropped = {"done": False}

    def flaky_authorize(
        amount_cents: int,
        payment_method_id: str,
        idempotency_key: str,
        metadata: dict[str, str],
    ) -> object:
        result = original_authorize(amount_cents, payment_method_id, idempotency_key, metadata)
        if not dropped["done"]:
            dropped["done"] = True
            raise TimeoutError("simulated network drop after provider created hold")
        return result

    monkeypatch.setattr(payments, "authorize", flaky_authorize)

    clock_state = {"now": NOW}
    real = service(tmp_path / f"consent-{retry_via}.sqlite", payments)
    monkeypatch.setattr(real, "_deps", replace(real._deps, clock=lambda: clock_state["now"]))

    async with AsyncClient(
        transport=ASGITransport(app=create_app(real)), base_url="http://offline.test"
    ) as api:
        _, receipt = await submit(api, copy.deepcopy(INTAKE))
    case_id = receipt["case_id"]
    real.choose(case_id, s.ChooseTier(tier=s.MailTier.CERTIFIED))
    body = s.StartAuthorization(terms_accepted=True, payment_method_id="pm_card_visa")

    # 1. Provider creates the hold with the original TERMS_VERSION, then the response drops.
    with pytest.raises(ServiceConflict, match="card_not_authorized"):
        real.authorize(case_id, body)
    assert len(payments.authorizations) == 1

    # 2. Both TERMS_VERSION and the wall clock advance before recovery/retry.
    monkeypatch.setattr(case_service_module, "TERMS_VERSION", "terms-2099-01-01")
    clock_state["now"] = NOW + timedelta(minutes=5)

    # 3. Recovery or direct authorize retry must succeed, reuse the exact original
    #    payment request under the same idempotency key, and create no second hold.
    if retry_via == "recover":
        status = real.recover_authorization(case_id)
        assert status.state != "STOPPED_NO_CHARGE"
        assert not status.authorization_pending
        assert status.payment_state == "AUTHORIZED"
    else:
        auth = real.authorize(case_id, body)
        assert auth.status == "authorized"

    assert len(payments.authorizations) == 1
    _amount, _pm, metadata, _outcome = next(iter(payments.authorizations.values()))
    assert metadata["terms_accepted"] == "true"
    assert metadata["terms_version"] == TERMS_VERSION
    assert datetime.fromisoformat(metadata["terms_accepted_at"]) == NOW
