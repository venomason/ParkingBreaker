"""T56: each customer keeps one of three prices, and that price is what is held and recorded."""

from collections import Counter
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.api.app import create_app
from parkingbreaker.config import build_services, load_settings
from parkingbreaker.prices import ALLOWED_PRICE_CENTS, PRICE_ARMS, assigned_price, price_arm
from test_config import ALICE, SECRET, held_case

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_assignment_is_sticky_even_and_only_the_three_prices() -> None:
    assert {1999, 2499, 2999} == ALLOWED_PRICE_CENTS
    picks = Counter(assigned_price(f"customer-{n}") for n in range(3000))
    assert set(picks) == ALLOWED_PRICE_CENTS
    assert all(900 <= count <= 1100 for count in picks.values()), picks
    # Pinned: changing the salt or the split would move returning customers to a new price.
    assert [assigned_price(c) for c in ("alice", "bob", "customer-1")] == [1999, 2499, 2999]
    assert {price_arm(cents) for cents in ALLOWED_PRICE_CENTS} == set(PRICE_ARMS)
    with pytest.raises(ValueError, match="price-test amount"):
        price_arm(2900)
    with pytest.raises(ValueError, match="customer id"):
        assigned_price(" ")


async def test_the_offered_price_is_what_is_held_and_recorded(tmp_path: Path) -> None:
    settings = load_settings(
        {"PB_PROVIDER_MODE": "fake", "PB_WORKER_SECRET": SECRET, "PB_DATA_DIR": str(tmp_path)},
        tmp_path / "no.env",
    )
    services = build_services(settings)
    app = create_app(services, worker_secret=settings.worker_secret)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fake.test") as c:
        case = await held_case(c)
        offer = (await c.get(f"/cases/{case}/pre-hold", headers=ALICE)).json()
        status = (await c.get(f"/cases/{case}/status", headers=ALICE)).json()
    expected = assigned_price("alice")
    assert [t["amount_cents"] for t in offer["tiers"]] == [expected]
    assert status["amount_cents"] == expected


@pytest.mark.parametrize("requested", [1999, 2499])
def test_a_hold_for_another_price_is_not_this_authorization(requested: int) -> None:
    from test_stripe_payment import REF, Transport, client, fixture

    held = client(Transport(fixture())).authorize(requested, "pm_card_visa", "key", {})
    assert (held.status, held.authorization_ref) == ("uncertain", REF)
    assert not held.full_object
