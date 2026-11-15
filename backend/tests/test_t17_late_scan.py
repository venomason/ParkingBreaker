"""Late webhook receipt must preserve verified mail evidence without late capture."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from pathlib import Path

import pytest

from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.effect_dispatch import dispatch_once
from parkingbreaker.lob_submission import Accepted, LetterEvidence
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_case_service import service
from test_submission_coordinator import NOW, prepared
from test_t17_mail_evidence import (
    OfflineLob,
    current_case,
    cursor_for,
    new_id,
    resource,
    submit,
)


@pytest.mark.parametrize("event_after_expiry", [False, True])
def test_signed_scan_received_after_expiry_accepts_original_letter_but_never_captures_late(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event_after_expiry: bool
) -> None:
    path = tmp_path / "cases.sqlite"
    wire = OfflineLob(monkeypatch, lambda *_: (200, resource()))
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        submit(store, wire, case, NOW)
    payments = FakePaymentClient(case.authorization_ref)

    received_at = case.authorization_expires_at + timedelta(minutes=1)
    event_at = case.authorization_expires_at + timedelta(
        seconds=30 if event_after_expiry else -5 * 60
    )
    original_ref = case.authorization_ref
    event_id = "evt_late_scan_after_expiry" if event_after_expiry else "evt_late_scan"
    body = resource()
    payload = json.dumps(
        {
            "id": event_id,
            "event_type": {"id": "letter.in_transit"},
            "body": body,
            "date_created": event_at.isoformat(),
        }
    ).encode()
    stamp = str(int(received_at.timestamp()))
    signature = hmac.new(b"secret", stamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    headers = {
        "lob-signature": signature,
        "lob-signature-timestamp": stamp,
    }

    app = service(path, payments)
    object.__setattr__(app._deps, "clock", lambda: received_at)
    object.__setattr__(app._deps, "lob_webhook_secret", "secret")
    object.__setattr__(app._deps, "new_id", new_id)
    assert app.webhook("lob", payload, headers) is True
    app._store.close()

    # Read the committed acceptance after a process-style reopen, then replay the
    # identical signed event through a fresh service instance.
    app = service(path, payments)
    object.__setattr__(app._deps, "clock", lambda: received_at)
    object.__setattr__(app._deps, "lob_webhook_secret", "secret")
    object.__setattr__(app._deps, "new_id", new_id)
    try:
        saved = current_case(app._store, case)
        cursor = cursor_for(app._store, case)
        assert saved.authorization_ref == original_ref
        assert saved.letter_state is cs.CaseState.IN_TRANSIT
        assert saved.payment_state is ps.PaymentState.CAPTURE_PENDING
        assert isinstance(cursor.outcome, Accepted)
        assert cursor.outcome.letter_id == "ltr_Original"
        assert cursor.outcome.evidence is LetterEvidence.PRODUCTION
        effects = app._store.effects(case.case_token)
        assert sum(row.effect is ps.PaymentEffect.CAPTURE_PAYMENT for row in effects) == 1

        assert app.webhook("lob", payload, headers) is False
        assert app._store.effects(case.case_token) == effects
        assert cursor_for(app._store, case) == cursor
    finally:
        app._store.close()

    # Dispatch can reconcile the existing authorization after its deadline, but
    # must not capture, release, or authorize another object.
    with SqliteTransactionStore(path) as store:
        dispatch_once(store, wire.client, payments, lambda: received_at, new_id)
        saved = current_case(store, case)
        assert saved.authorization_ref == original_ref
        assert saved.letter_state is cs.CaseState.IN_TRANSIT
        assert saved.payment_state is ps.PaymentState.CAPTURE_PENDING
        assert not any(call[0] in {"capture", "release", "authorize"} for call in payments.calls)
