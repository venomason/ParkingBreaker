"""The paid card hold records the accepted terms as durable dispute evidence."""

import copy
from datetime import datetime
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
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
