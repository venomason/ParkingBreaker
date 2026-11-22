"""J05: consent-aware D+14/D+30 follow-ups, honest outcomes, clerical copy."""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from httpx2 import AsyncClient

from parkingbreaker.case_service import CaseService
from parkingbreaker.case_state import DeliveryProofStored
from parkingbreaker.email_client import EmailOutcome, FakeEmailClient
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.outcome_follow_up import (
    OutcomeFollowUpService,
    OutcomeLinkService,
    derive_follow_up_token,
    verify_follow_up_token,
)
from test_case_service import Clock, fake_resolution
from test_effect_dispatch import WORKER, approved, payment_fixture

assert fake_resolution
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fake_resolution")]
LINK_KEY = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode()
ORIGIN = "https://parkingbreaker.com"
DELIVERED_AT = datetime(2026, 11, 1, 12, tzinfo=UTC)


def _accepted() -> EmailOutcome:
    return EmailOutcome("accepted", provider_id=str(uuid4()))


async def _delivered_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[AsyncClient, CaseService, str, str, Clock]:
    lob = FakeLobClient(
        [
            LobResponse(
                200, "ltr_follow", tracking_number="940000009", evidence=LetterEvidence.PRODUCTION
            )
        ]
    )
    client, real, case_id = await approved(tmp_path, monkeypatch, lob)
    clock = Clock()
    object.__setattr__(real._deps, "clock", clock)
    payments, _transport = stripe_of(real)
    object.__setattr__(real._deps, "payments", payments)
    stepped = await client.post(
        f"/internal/cases/{case_id}/step", headers=WORKER, json={"step_id": "dispatch"}
    )
    assert stepped.json()["state"] == "IN_TRANSIT", stepped.text
    clock.now = DELIVERED_AT
    record = real._intake(case_id)
    assert record.case_token is not None
    token = record.case_token
    current = real._store.get_case(token)
    assert current is not None
    real._store.apply_letter_event(
        token,
        DeliveryProofStored("proof_follow"),
        current.revision,
        occurred_at=DELIVERED_AT,
    )
    assert real._store.get_case(token) is not None
    return client, real, case_id, token, clock


def stripe_of(real: CaseService) -> tuple[object, object]:
    from test_case_service import stripe

    return stripe(payment_fixture(), payment_fixture(), payment_fixture("succeeded"))


def _service(real: CaseService, client: FakeEmailClient, clock: Clock) -> OutcomeFollowUpService:
    return OutcomeFollowUpService(
        real._store,
        client,
        key=LINK_KEY,
        origin=ORIGIN,
        sender="appeals@parkingbreaker.com",
        clock=clock,
    )


async def test_sends_d14_once_then_d30_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, _case_id, _token, clock = await _delivered_case(tmp_path, monkeypatch)
    email = FakeEmailClient([_accepted(), _accepted()])
    try:
        clock.now = DELIVERED_AT + timedelta(days=15)
        first = _service(real, email, clock).run()
        assert first.sent == 1
        assert first.unknown_recorded == 0
        again = _service(real, email, clock).run()
        assert again.sent == 0  # at most one send per window
        clock.now = DELIVERED_AT + timedelta(days=31)
        later = _service(real, email, clock).run()
        assert later.sent == 1
        assert email.call_count == 2
    finally:
        await client.aclose()


async def test_opt_out_stops_sends_and_a_later_opt_in_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, _case_id, token, clock = await _delivered_case(tmp_path, monkeypatch)
    email = FakeEmailClient([_accepted(), _accepted()])
    try:
        address = "alex@example.test"
        real._store.set_follow_up_consent(
            address, False, "optout-1", "customer", "asked to stop", now=clock.now
        )
        clock.now = DELIVERED_AT + timedelta(days=15)
        skipped = _service(real, email, clock).run()
        assert skipped.sent == 0
        assert skipped.skipped_no_consent >= 1
        assert email.call_count == 0
        real._store.set_follow_up_consent(
            address, True, "optin-1", "customer", "changed mind", now=clock.now
        )
        resumed = _service(real, email, clock).run()
        assert resumed.sent == 1
        assert real._store.follow_up_sent(token, "D14") is True
    finally:
        await client.aclose()


async def test_unknown_is_recorded_after_the_final_window_and_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, _case_id, token, clock = await _delivered_case(tmp_path, monkeypatch)
    email = FakeEmailClient([_accepted(), _accepted()])
    try:
        clock.now = DELIVERED_AT + timedelta(days=30 + 30 + 1)
        report = _service(real, email, clock).run()
        assert report.unknown_recorded == 1
        outcome = real._store.outcome_for(token)
        assert outcome is not None
        assert outcome.kind == "OUTCOME_UNKNOWN"
        assert "dismiss" not in outcome.reason.lower()
        assert "defeat" not in outcome.reason.lower()
        later = _service(real, email, clock).run()
        assert later.unknown_recorded == 0  # exactly one outcome per case
    finally:
        await client.aclose()


async def test_a_rejected_email_is_deferred_and_not_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from parkingbreaker.email_client import EmailReason

    client, real, _case_id, token, clock = await _delivered_case(tmp_path, monkeypatch)
    email = FakeEmailClient(
        [EmailOutcome("rejected", reason=EmailReason.PROVIDER_REJECTED), _accepted()]
    )
    try:
        clock.now = DELIVERED_AT + timedelta(days=15)
        first = _service(real, email, clock).run()
        assert first.sent == 0
        assert first.deferred == 1
        assert real._store.follow_up_sent(token, "D14") is False
        retry = FakeEmailClient([_accepted()])
        second = _service(real, retry, clock).run()
        assert second.sent == 1
        assert real._store.follow_up_sent(token, "D14") is True
    finally:
        await client.aclose()


async def test_follow_up_copy_is_clerical_and_carries_a_verifiable_opt_out_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, _case_id, token, clock = await _delivered_case(tmp_path, monkeypatch)
    email = FakeEmailClient([_accepted()])
    try:
        clock.now = DELIVERED_AT + timedelta(days=15)
        service = _service(real, email, clock)
        service.run()
        message = next(iter(email._messages.values()))
        assert "do not give legal advice" in message.text
        assert "do not promise any outcome" in message.text
        assert "dismiss" not in message.text.lower()
        assert "guarantee" not in message.text.lower()
        assert "promise" not in message.text.lower().replace("do not promise", "")
        assert "we do not give legal advice" in message.text.lower()
        assert "/outcomes/opt-out#case=" in message.text
        assert "/outcomes/opt-out?" not in message.text
        match = re.search(r"[?&]token=([0-9a-f]{64})", message.text)
        assert match is not None
        assert verify_follow_up_token(LINK_KEY, token, match.group(1)) is True
        assert verify_follow_up_token(LINK_KEY, token, "0" * 64) is False
        assert derive_follow_up_token(LINK_KEY, token) == match.group(1)
        assert "day 14 and once at day 30" in message.text
    finally:
        await client.aclose()


async def test_private_link_opts_out_and_reports_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, _case_id, token, clock = await _delivered_case(tmp_path, monkeypatch)
    try:
        links = OutcomeLinkService(real._store, key=LINK_KEY, clock=clock)
        good = derive_follow_up_token(LINK_KEY, token)
        assert links.opt_out(token, "0" * 64) is False
        assert links.report(token, "0" * 64, "dismissed") is False
        assert links.opt_out(token, good) is True
        assert real._store.follow_up_allowed("alex@example.test") is False
        # A customer-reported decision is recorded as told, never as an agency fact.
        assert links.report(token, good, "pending") is True
        assert real._store.outcome_for(token) is None
        assert links.report(token, good, "dismissed") is True
        outcome = real._store.outcome_for(token)
        assert outcome is not None
        assert outcome.kind == "OUTCOME_CUSTOMER"
        assert outcome.detail == "customer_reported:dismissed"
        assert links.report(token, good, "not-a-decision") is False
        # The same decision again is a satisfied request, never a crash or a rewrite.
        assert links.report(token, good, "dismissed") is True
        assert links.report(token, good, "upheld") is False
        assert real._store.outcome_for(token) == outcome
    finally:
        await client.aclose()


async def test_opt_out_after_a_later_opt_in_is_a_new_decision_not_a_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, _case_id, token, clock = await _delivered_case(tmp_path, monkeypatch)
    try:
        links = OutcomeLinkService(real._store, key=LINK_KEY, clock=clock)
        good = derive_follow_up_token(LINK_KEY, token)
        address = "alex@example.test"
        assert links.opt_out(token, good) is True
        assert real._store.follow_up_allowed(address) is False
        assert links.opt_out(token, good) is True  # idempotent while already opted out
        real._store.set_follow_up_consent(
            address, True, "support-optin", "support", "customer asked to resume", now=clock.now
        )
        assert real._store.follow_up_allowed(address) is True
        # A third click is a fresh decision: the response must match the stored state.
        assert links.opt_out(token, good) is True
        assert real._store.follow_up_allowed(address) is False
        email = FakeEmailClient([_accepted(), _accepted()])
        clock.now = DELIVERED_AT + timedelta(days=31)
        report = _service(real, email, clock).run()
        assert report.sent == 0
        assert email.call_count == 0
    finally:
        await client.aclose()


async def test_recorded_outcome_stops_subsequent_follow_ups_and_avoids_starvation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, real, _case_id, token, clock = await _delivered_case(tmp_path, monkeypatch)
    email = FakeEmailClient([_accepted(), _accepted()])
    try:
        links = OutcomeLinkService(real._store, key=LINK_KEY, clock=clock)
        good = derive_follow_up_token(LINK_KEY, token)
        clock.now = DELIVERED_AT + timedelta(days=5)
        assert links.report(token, good, "dismissed") is True
        # Resolved cases are excluded from pending delivery scans so they cannot starve newer cases.
        assert real._store.delivered_cases(limit=1) == []
        clock.now = DELIVERED_AT + timedelta(days=31)
        report = _service(real, email, clock).run()
        assert report.sent == 0
        assert report.unknown_recorded == 0
        assert email.call_count == 0
    finally:
        await client.aclose()
