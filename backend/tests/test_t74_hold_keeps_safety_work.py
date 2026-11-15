"""T74 criteria 2 and 4: a hold landing after the letter request went out changes nothing.

Provider lookups, capture after a confirmed mailing, the stop and its hold release all keep
running on a held case, and retry never overrides a scheduled lookup. This holds with the
mail dispatcher's create-intent gate still pending; it does not test that gate.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.case_hold_store import CaseHold, CommandOutcome, HoldReason
from parkingbreaker.case_service import CaseService
from parkingbreaker.health_service import _holds
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from test_case_service import anyio_backend as anyio_backend
from test_case_service import fake_resolution as fake_resolution
from test_case_service import stripe
from test_effect_dispatch import approved, dispatch, payment_fixture
from test_payment_stress_failures import _stripe_money
from test_t59_outages import ACCEPTED, LETTER, _mailed_once_and_charged_once, _sweep_hours


def _token(real: CaseService, case: str) -> str:
    record = real._store.get_intake(case)
    assert record is not None
    assert record.case_token is not None
    return record.case_token


def _hold(real: CaseService, token: str) -> CaseHold:
    outcome, hold = real.hold_case(token, HoldReason.INCIDENT, "h1")
    assert outcome is CommandOutcome.APPLIED
    assert hold is not None
    assert hold.held
    return hold


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_lookup_and_capture_keep_running_on_a_held_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(None, error="timeout")])
    api, real, case = await approved(tmp_path, monkeypatch, lob)
    payments, transport = stripe(payment_fixture(), payment_fixture("succeeded"))
    object.__setattr__(real._deps, "payments", payments)
    try:
        await dispatch(api)
        _stripe_money(transport, captures=0, releases=0)
        token = _token(real, case)
        hold = _hold(real, token)
        # Lob made the letter, but its answer never arrived.
        (key, request), *_ = lob.calls
        lob.letters[request.submission_token] = LETTER
        lob.by_key[key] = LETTER
        lob.evidence_by_token[request.submission_token] = LetterEvidence.PRODUCTION
        await _sweep_hours(api, real, 6)
        _mailed_once_and_charged_once(real, case, lob)
        _stripe_money(transport, captures=1, releases=0)
        assert len({key for key, _ in lob.calls}) == 1  # any retry reused the one key
        # The hold is untouched: it neither lapsed nor was consumed by the mailing.
        assert real._store.case_hold(token) == hold
    finally:
        await api.aclose()


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_the_stop_and_its_release_keep_running_on_a_held_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(503), ACCEPTED])
    api, real, case = await approved(tmp_path, monkeypatch, lob)
    payments, transport = stripe(payment_fixture(), payment_fixture("canceled"))
    object.__setattr__(real._deps, "payments", payments)
    try:
        await dispatch(api)
        token = _token(real, case)
        hold = _hold(real, token)
        await _sweep_hours(api, real, 6)
        # Lob confirms no letter exists: the case stops and the hold is released once.
        status = real.status(case)
        assert status.state is cs.CaseState.STOPPED_NO_CHARGE
        assert status.payment_state is ps.PaymentState.RELEASED
        assert lob.by_key == {}
        _stripe_money(transport, captures=0, releases=1)
        assert real.resume_case(token, "r1", hold.generation)[0] is CommandOutcome.REJECTED
        assert _holds(real._store._require_open()) == ()
    finally:
        await api.aclose()


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_retry_on_a_held_case_runs_its_due_lookup_and_keeps_the_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(None, error="timeout")])
    api, real, case = await approved(tmp_path, monkeypatch, lob)
    payments, _transport = stripe(payment_fixture(), payment_fixture("succeeded"))
    object.__setattr__(real._deps, "payments", payments)
    try:
        await dispatch(api)
        token = _token(real, case)
        hold = _hold(real, token)
        (key, request), *_ = lob.calls
        lob.letters[request.submission_token] = LETTER
        lob.by_key[key] = LETTER
        lob.evidence_by_token[request.submission_token] = LetterEvidence.PRODUCTION
        cursor = real._store.submission_cursor(token, 1)
        assert cursor is not None
        # Not due yet: the retry is applied, and the backoff stands on a held case too.
        assert real.retry_case(token, "t1")[0] is CommandOutcome.APPLIED
        assert real._store.submission_cursor(token, 1) == cursor
        assert real.status(case).state is cs.CaseState.SUBMITTING
        # Due: no sweep needed. The lookup finds the one letter under the same key.
        due = cursor.next_at + timedelta(seconds=1)
        object.__setattr__(real._deps, "clock", lambda: due)
        assert real.retry_case(token, "t2")[0] is CommandOutcome.APPLIED
        assert real.status(case).state is cs.CaseState.IN_TRANSIT
        assert len({key for key, _ in lob.calls}) == 1
        assert real._store.case_hold(token) == hold
    finally:
        await api.aclose()


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_retry_before_next_at_keeps_the_scheduled_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lob = FakeLobClient([LobResponse(None, error="timeout")])
    api, real, case = await approved(tmp_path, monkeypatch, lob)
    payments, _transport = stripe(payment_fixture())
    object.__setattr__(real._deps, "payments", payments)
    try:
        await dispatch(api)
        token = _token(real, case)
        cursor = real._store.submission_cursor(token, 1)
        assert cursor is not None
        assert real._deps.clock() < cursor.next_at
        calls = len(lob.calls)
        assert real.retry_case(token, "t1")[0] is CommandOutcome.APPLIED
        assert real._store.submission_cursor(token, 1) == cursor
        assert len(lob.calls) == calls
    finally:
        await api.aclose()
