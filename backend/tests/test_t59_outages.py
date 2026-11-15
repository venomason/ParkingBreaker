"""T59: an outage at each outside service fails safely and recovers on its own.

Each outside service is made unreachable at the moment it matters, then restored. The
checks are the same each time: no wrong or second charge, no wrong or second letter, no
lost record, and the case finishes once the service is back, driven only by the normal
scheduler sweep with time moving forward. Search and drafting-model outages stop the case
before any charge (covered in test_payment_stress_failures and the T40 drafting tests);
email outages retry the same message under the same key (test_t46_safety_gaps).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from httpx2 import AsyncClient

from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.case_service import CaseService
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution
from test_case_service import stripe
from test_effect_dispatch import approved, dispatch, payment_fixture
from test_payment_stress_failures import _stripe_money

LETTER = "ltr_once"
ACCEPTED = LobResponse(200, LETTER, tracking_number="9400", evidence=LetterEvidence.PRODUCTION)


async def _sweep_hours(api: AsyncClient, real: CaseService, hours: int) -> None:
    """The scheduler sweep once an hour, as time passes."""
    start = real._deps.clock()
    for hour in range(1, hours + 1):
        object.__setattr__(real._deps, "clock", lambda h=hour: start + timedelta(hours=h))
        await dispatch(api)


def _mailed_once_and_charged_once(real: CaseService, case: str, lob: FakeLobClient) -> None:
    status = real.status(case)
    assert status.state is cs.CaseState.IN_TRANSIT
    assert status.payment_state is ps.PaymentState.CAPTURED
    assert len(lob.by_key) == 1  # one letter exists, whatever was retried
    assert len(set(lob.letters.values())) == 1


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_stripe_outage_at_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lob = FakeLobClient([ACCEPTED])
    api, real, case = await approved(tmp_path, monkeypatch, lob)
    payments, transport = stripe(
        ConnectionError("stripe down"), payment_fixture(), payment_fixture("succeeded")
    )
    object.__setattr__(real._deps, "payments", payments)
    try:
        await dispatch(api)
        # Mailed, but the capture call never reached Stripe: pending, not charged, not lost.
        status = real.status(case)
        assert status.state is cs.CaseState.IN_TRANSIT
        assert status.payment_state is ps.PaymentState.CAPTURE_PENDING
        _stripe_money(transport, captures=0, releases=0)
        await _sweep_hours(api, real, 4)
        _mailed_once_and_charged_once(real, case, lob)
        _stripe_money(transport, captures=1, releases=0)
        assert len(lob.calls) == 1
    finally:
        await api.aclose()


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
@pytest.mark.parametrize("outage", ["timeout", "server_error"])
async def test_lob_outage_before_any_letter_exists_stops_without_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outage: str
) -> None:
    down = LobResponse(None, error="timeout") if outage == "timeout" else LobResponse(503)
    lob = FakeLobClient([down, ACCEPTED])
    api, real, case = await approved(tmp_path, monkeypatch, lob)
    payments, transport = stripe(payment_fixture(), payment_fixture("canceled"))
    object.__setattr__(real._deps, "payments", payments)
    try:
        await dispatch(api)
        assert len(lob.calls) == 1
        await _sweep_hours(api, real, 6)
        # Lob confirms no letter exists, so the case stops and the hold is released once.
        # It does not retry by itself: the customer is told nothing was sent or charged.
        status = real.status(case)
        assert status.state is cs.CaseState.STOPPED_NO_CHARGE
        assert status.payment_state is ps.PaymentState.RELEASED
        assert status.stop is not None
        assert status.stop.reason_key == "stop.transaction_failure.not_sent"
        assert lob.by_key == {}
        _stripe_money(transport, captures=0, releases=1)
    finally:
        await api.aclose()


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_lob_lost_response_after_the_letter_was_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(None, error="timeout")])
    api, real, case = await approved(tmp_path, monkeypatch, lob)
    payments, transport = stripe(payment_fixture(), payment_fixture("succeeded"))
    object.__setattr__(real._deps, "payments", payments)
    try:
        await dispatch(api)
        _stripe_money(transport, captures=0, releases=0)  # nothing charged while unsure
        # Lob made the letter, but its answer never arrived.
        (key, request), *_ = lob.calls
        lob.letters[request.submission_token] = LETTER
        lob.by_key[key] = LETTER
        lob.evidence_by_token[request.submission_token] = LetterEvidence.PRODUCTION
        await _sweep_hours(api, real, 6)
        _mailed_once_and_charged_once(real, case, lob)
        _stripe_money(transport, captures=1, releases=0)
        assert len({key for key, _ in lob.calls}) == 1  # any retry reused the one key
    finally:
        await api.aclose()
