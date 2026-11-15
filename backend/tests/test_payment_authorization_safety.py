"""Offline negative controls for pre-case authorization recovery."""

from dataclasses import replace
from typing import cast

import pytest

from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.payment_records import PaymentOutcome
from test_case_service import fake_resolution as fake_resolution
from test_stripe_payment import REF, Transport, client, fixture
from test_t14_authorization_recovery import BODY, RecoveryCase
from test_t14_authorization_recovery import recovery as recovery


def _card_error(intent: object, kind: str = "card_error") -> dict[str, object]:
    return {"error": {"type": kind, "payment_intent": intent, "message": "PRIVATE ERROR"}}


@pytest.mark.parametrize(
    ("status", "body", "expected_ref"),
    [
        (402, _card_error(fixture("succeeded")), REF),
        (402, _card_error(fixture("canceled")), REF),
        (402, _card_error({"id": REF}), REF),
        (400, _card_error({"id": REF}), ""),
        (500, _card_error({"id": REF}), ""),
        (402, _card_error({"id": REF}, "api_error"), ""),
        (402, _card_error({"id": "ch_Original123"}), ""),
        (402, _card_error({"id": "pi_"}), ""),
        (402, _card_error({"id": "pi_bad/path"}), ""),
        (402, _card_error({"id": "pi_bad?query"}), ""),
        (402, _card_error({"id": "pi_bad\n"}), ""),
        (402, _card_error({"id": 123}), ""),
        (402, _card_error({}), ""),
        (402, _card_error(None), ""),
        (402, _card_error(REF), ""),
        (402, {"error": {"type": "card_error"}}, ""),
        (402, {"error": None}, ""),
        (402, "{", ""),
        (402, "null", ""),
        (
            402,
            '{"error":{"type":"card_error","payment_intent":'
            '{"id":"pi_First123","id":"pi_Second123"}}}',
            "",
        ),
    ],
)
def test_error_body_supplies_only_a_strict_identity_hint(
    status: int, body: object, expected_ref: str, caplog: pytest.LogCaptureFixture
) -> None:
    transport = Transport(body, status=status)
    result = client(transport).authorize(2999, BODY.payment_method_id, "original-key", {})
    assert result == PaymentOutcome(
        "uncertain",
        expected_ref,
        evidence_version=1,
        classification="server" if status >= 500 else "ambiguous_error",
    )
    assert len(transport.calls) == 1
    assert transport.calls[0][:2] == ("post", "/v1/payment_intents")
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize("evidence_required", [False, True])
@pytest.mark.parametrize(
    ("classification", "received", "capturable"),
    [
        (None, 0, 0),
        ("ambiguous_error", 0, 0),
        ("verified_object", None, 0),
        ("verified_object", 1, 0),
        ("verified_object", 2999, 0),
        ("verified_object", False, 0),
        ("verified_object", 0.0, 0),
        ("verified_object", 0, None),
        ("verified_object", 0, 2999),
    ],
)
def test_unverified_or_contradictory_cancellation_keeps_new_card_blocked(
    recovery: RecoveryCase,
    evidence_required: bool,
    classification: str | None,
    received: object,
    capturable: object,
) -> None:
    recovery.payments.authorization_evidence_required = evidence_required
    recovery.payments.next_response = PaymentOutcome(
        "uncertain", recovery.payments.object.authorization_ref
    )
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    saved = recovery.attempt
    recovery.payments.object = replace(
        recovery.payments.object,
        status="canceled",
        classification=classification,
        amount_received=cast(int | None, received),
        amount_capturable=cast(int | None, capturable),
    )
    with pytest.raises(ServiceConflict, match=r"^card_not_authorized$"):
        recovery.real.authorize(recovery.case_id, BODY)
    assert recovery.attempt.state == "uncertain"
    assert recovery.attempt.authorization_ref == saved.authorization_ref
    assert recovery.attempt.idempotency_key == saved.idempotency_key
    assert recovery.attempt.request_json == saved.request_json
    calls = list(recovery.payments.calls)
    assert [call[0] for call in calls] == ["authorize", "retrieve"]
    with pytest.raises(ServiceConflict, match=r"^authorization_outcome_unresolved$"):
        recovery.real.authorize(
            recovery.case_id,
            s.StartAuthorization(payment_method_id="pm_fresh_card", terms_accepted=True),
        )
    assert recovery.payments.calls == calls
    assert recovery.payments.captures == recovery.payments.releases == 0
    recovery.assert_no_case()
