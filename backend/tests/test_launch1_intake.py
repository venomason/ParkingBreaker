"""LAUNCH-1: pre-research and pre-hold no-charge intake gates, offline."""

import copy
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker import case_state as cs
from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.service_policy import IntakeStop, intake_customer_stop
from parkingbreaker.stop_reasons import stop_message
from test_case_service import ALICE, INTAKE, NOW, Clock, service, submit
from test_case_service import fake_resolution as fake_resolution
from test_stripe_payment import REF


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize(
    ("issued", "today", "expected"),
    [
        (date(2026, 10, 17), date(2026, 10, 27), None),
        (date(2026, 10, 16), date(2026, 10, 27), IntakeStop.TOO_OLD),
        (date(2026, 10, 27), date(2026, 10, 27), None),
        (date(2025, 12, 27), date(2026, 1, 6), None),
        (date(2025, 12, 26), date(2026, 1, 6), IntakeStop.TOO_OLD),
        (date(2024, 2, 23), date(2024, 3, 4), None),
    ],
)
def test_ten_day_cutoff_uses_passed_dates(
    issued: date, today: date, expected: IntakeStop | None
) -> None:
    assert intake_customer_stop(issued, today, "NOT_RENTAL") is expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (True, IntakeStop.RENTAL_PAID),
        (False, None),
        ("NOT_RENTAL", None),
        (None, IntakeStop.RENTAL_PAYMENT_UNKNOWN),
        ("unknown", IntakeStop.RENTAL_PAYMENT_UNKNOWN),
    ],
)
def test_rental_answers_fail_closed(answer: bool | str | None, expected: IntakeStop | None) -> None:
    assert intake_customer_stop(NOW.date(), NOW.date(), answer) is expected


def test_too_old_copy_states_service_age_limit_not_issuer_deadline() -> None:
    message = stop_message(
        cs.StopReason.BUSINESS_EXCLUSION,
        policy_reason="age limit",
        intake_stop=IntakeStop.TOO_OLD,
    )

    assert "ParkingBreaker accepts tickets up to 10 days old." in message.text
    assert "outside our service's age limit" in message.text
    assert "in time" not in message.text
    assert "deadline" not in message.text.lower()
    assert "official instructions" in message.text
    assert "Nothing was charged." in message.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("age", "rental", "stop_key"),
    [
        (11, "NOT_RENTAL", "too_old"),
        (3, True, "rental_paid"),
        (3, None, "rental_payment_unknown"),
        (3, "MISSING", "rental_payment_unknown"),
        (10, False, None),
        (10, "NOT_RENTAL", None),
    ],
)
async def test_intake_stops_before_research_and_authorization(
    tmp_path: Path, fake_resolution: list[str], age: int, rental: Any, stop_key: str | None
) -> None:
    path = tmp_path / "intake.sqlite"
    payments = FakePaymentClient(REF)
    real = service(path, payments)
    body = copy.deepcopy(INTAKE)
    body["ticket"]["issue_date"] = (NOW.date() - timedelta(days=age)).isoformat()
    # A later notice date never hides the original ticket age.
    body["ticket"]["notice_date"] = NOW.date().isoformat()
    if rental == "MISSING":
        del body["rental_company_paid"]
    else:
        body["rental_company_paid"] = rental
    async with AsyncClient(
        transport=ASGITransport(app=create_app(real)), base_url="http://offline.test"
    ) as api:
        workspace, receipt = await submit(api, body)
        case_id = receipt["case_id"]
        prehold = (await api.get(f"/cases/{case_id}/pre-hold", headers=ALICE)).json()
        if stop_key is not None:
            assert receipt["state"] == "STOPPED_NO_CHARGE"
            assert prehold["kind"] == "stop"
            assert prehold["reason_key"].endswith(stop_key)
            assert prehold["no_charge"] is True
            assert "Nothing was charged." in prehold["message"]
            expected = {
                "too_old": "outside our service's age limit.",
                "rental_paid": "A paid ticket usually cannot be appealed.",
                "rental_payment_unknown": "before we can take your payment.",
            }
            assert expected[stop_key] in prehold["message"]
            assert "official_url" not in prehold  # No issuer research ran; no guessed link.
            for suffix, payload in [
                ("tier", {"tier": "CERTIFIED"}),
                ("authorization", {"terms_accepted": True, "payment_method_id": "pm_card_visa"}),
            ]:
                response = await (api.put if suffix == "tier" else api.post)(
                    f"/cases/{case_id}/{suffix}", headers=ALICE, json=payload
                )
                assert response.status_code == 409
            assert fake_resolution == []
            assert payments.calls == []
            record = real._store.get_intake(case_id)
            assert record is not None
            assert record.case_token is None
            assert real._store.get_research(case_id) is None
            status = (await api.get(f"/cases/{case_id}/status", headers=ALICE)).json()
            assert status["state"] == "STOPPED_NO_CHARGE"
            assert status["payment_state"] is None
            assert status["stop"] == prehold
            restored = service(path, payments)
            assert restored.pre_hold(case_id).model_dump(mode="json", exclude_none=True) == {
                k: v for k, v in prehold.items() if v is not None
            }
            assert restored.workspace_view(workspace).intake is not None
        else:
            assert receipt["state"] == "INTAKE"
            assert prehold["kind"] == "offer"
            assert prehold["tiers"][0]["amount_cents"] == 2999
            await api.put(f"/cases/{case_id}/tier", headers=ALICE, json={"tier": "CERTIFIED"})
            auth = await api.post(
                f"/cases/{case_id}/authorization",
                headers=ALICE,
                json={"terms_accepted": True, "payment_method_id": "pm_card_visa"},
            )
            assert auth.status_code == 200, auth.text
            assert auth.json()["amount_cents"] == 2999
            assert payments.calls[0][:3] == ("authorize", "pm_card_visa", 2999)
            assert payments.captures == 0


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["known_unverified", "unknown", "excluded"])
async def test_unverified_intake_shows_plain_copy_and_only_recorded_site(
    tmp_path: Path, fake_resolution: list[str], kind: str
) -> None:
    payments = FakePaymentClient(REF)
    real = service(
        tmp_path / "unverified.sqlite",
        payments,
        launch_allowlist=frozenset() if kind == "excluded" else None,
    )
    body = copy.deepcopy(INTAKE)
    if kind == "known_unverified":
        body["ticket"]["printed_deadline"] = (NOW.date() - timedelta(days=1)).isoformat()
    elif kind == "unknown":
        body["ticket"]["issuer_hint"] = "Unreachable County"
    async with AsyncClient(
        transport=ASGITransport(app=create_app(real)), base_url="http://offline.test"
    ) as api:
        _, receipt = await submit(api, body)
        result = (await api.get(f"/cases/{receipt['case_id']}/pre-hold", headers=ALICE)).json()
    if kind == "excluded":
        assert result["family"] == "BUSINESS_EXCLUSION"
        assert "Nothing was charged." in result["message"]
    else:
        assert result["family"] == "NOT_YET_VERIFIED"
        issuer = (
            "Unreachable County"
            if kind == "unknown"
            else "San Francisco Municipal Transportation Agency (SFMTA)"
        )
        assert (
            f"We can't confirm how {issuer} handles mailed appeals, so we won't take your payment."
            in result["message"]
        )
    assert result.get("official_url") == (None if kind == "unknown" else "https://sfmta.com/")
    assert payments.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("legacy_change", ["missing_rental", "old_ticket"])
async def test_saved_unstarted_offers_cannot_bypass_new_intake_gates(
    tmp_path: Path, fake_resolution: list[str], legacy_change: str
) -> None:
    import json

    real = service(tmp_path / "legacy-intake.sqlite", FakePaymentClient(REF))
    async with AsyncClient(
        transport=ASGITransport(app=create_app(real)), base_url="http://offline.test"
    ) as api:
        _, receipt = await submit(api, copy.deepcopy(INTAKE))
    case_id = receipt["case_id"]
    record = real._store.get_intake(case_id)
    assert record is not None
    intake = json.loads(record.intake_json)
    offer = json.loads(record.prehold_json)
    if legacy_change == "missing_rental":
        del intake["rental_company_paid"]
    elif legacy_change == "old_ticket":
        intake["ticket"]["issue_date"] = (NOW.date() - timedelta(days=11)).isoformat()
    real._store._require_open().execute(
        "UPDATE pb_intake_records SET intake_json=?, prehold_json=? WHERE intake_id=?",
        (json.dumps(intake), json.dumps(offer), case_id),
    )
    result = real.pre_hold(case_id)
    assert isinstance(result, s.Stop)
    assert result.official_url == "https://sfmta.com/"
    assert real.status(case_id).state is cs.CaseState.STOPPED_NO_CHARGE
    with pytest.raises(ServiceConflict, match="case_stopped"):
        real.authorize(
            case_id, s.StartAuthorization(terms_accepted=True, payment_method_id="pm_card_visa")
        )


@pytest.mark.anyio
@pytest.mark.parametrize("started", [False, True])
async def test_day_ten_offer_expires_before_new_hold_but_preserves_existing_authorization(
    tmp_path: Path, fake_resolution: list[str], started: bool
) -> None:
    clock = Clock()
    payments = FakePaymentClient(REF)
    real = service(tmp_path / "aging-intake.sqlite", payments, clock)
    body = copy.deepcopy(INTAKE)
    body["ticket"]["issue_date"] = (NOW.date() - timedelta(days=10)).isoformat()
    async with AsyncClient(
        transport=ASGITransport(app=create_app(real)), base_url="http://offline.test"
    ) as api:
        _, receipt = await submit(api, body)
    case_id = receipt["case_id"]
    real.choose(case_id, s.ChooseTier(tier=s.MailTier.CERTIFIED))
    authorization = s.StartAuthorization(terms_accepted=True, payment_method_id="pm_card_visa")
    original = real.authorize(case_id, authorization) if started else None
    calls = list(payments.calls)
    resolutions = list(fake_resolution)
    clock.now += timedelta(hours=4)  # Day 10 becomes day 11, within the existing hold window.
    if started:
        assert real.authorize(case_id, authorization) == original
        assert isinstance(real.pre_hold(case_id), s.Offer)
    else:
        stop = real.pre_hold(case_id)
        assert isinstance(stop, s.Stop)
        assert stop.reason_key.endswith("too_old")
        assert stop.official_url == "https://sfmta.com/"
        with pytest.raises(ServiceConflict, match="case_stopped"):
            real.authorize(case_id, authorization)
        assert real._store.has_active_authorization_attempt(case_id) is False
    assert payments.calls == calls
    assert fake_resolution == resolutions
    assert payments.captures == payments.releases == 0
